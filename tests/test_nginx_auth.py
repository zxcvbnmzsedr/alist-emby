"""Exercise the shipped Nginx/Lua gate against a synthetic AList server.

Linux CI installs Nginx's Lua and cjson modules. No real accounts are used.
"""
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
NGINX = shutil.which('nginx') or ('/usr/sbin/nginx' if Path('/usr/sbin/nginx').exists() else None)


class AlistFixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        token = self.headers.get('Authorization')
        roles = {'valid-token': [2], 'guest-token': [1], 'disabled-token': [2]}
        if self.path == '/api/me':
            payload = {'code': 200, 'data': {'role': roles[token], 'disabled': token == 'disabled-token'}} if token in roles else {'code': 401}
        else:
            payload = {'code': 404}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        self.rfile.read(length)
        payload = {'code': 200, 'data': {'sign': 'synthetic-sign'}} if self.headers.get('Authorization') == 'valid-token' else {'code': 401}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


@unittest.skipUnless(NGINX, 'Nginx/Lua is installed in Linux CI, not on this workstation')
class NginxAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), AlistFixture)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.origin = f'http://127.0.0.1:{port}'
        public = cls.root / 'www/cinema'
        (public / 'covers').mkdir(parents=True)
        (public / 'assets').mkdir()
        (public / 'index.html').write_text('synthetic login shell')
        (public / 'assets/app.js').write_text('// synthetic application shell')
        (public / 'catalog.json').write_text('{"videos":[]}')
        (public / 'covers/example.jpg').write_bytes(b'synthetic private image')
        template = (ROOT / 'cinema/nginx-pan.conf').read_text()
        template = '\n'.join(line for line in template.splitlines() if not any(s in line for s in ('listen 443 ssl', 'ssl_certificate')))
        template = template.replace('listen 80;', f'listen {port};')
        template = template.replace('127.0.0.1:5244', f'127.0.0.1:{cls.upstream.server_port}')
        template = template.replace('/srv/alist-emby/www', str(cls.root / 'www'))
        template = template.replace('/etc/nginx/conf.d/cinema-auth.lua', str(ROOT / 'cinema/nginx-cinema-auth.lua'))
        modules = Path('/usr/lib/nginx/modules')
        load = ''.join(f'load_module {modules / name};\n' for name in ('ndk_http_module.so', 'ngx_http_lua_module.so') if (modules / name).exists())
        config = load + f'pid {cls.root}/nginx.pid;\nerror_log {cls.root}/error.log;\nevents {{}}\nhttp {{ access_log off; client_body_temp_path {cls.root}/body; proxy_temp_path {cls.root}/proxy;\n' + template + '\n}\n'
        path = cls.root / 'nginx.conf'
        path.write_text(config)
        subprocess.run([NGINX, '-p', str(cls.root), '-c', str(path), '-t'], check=True, capture_output=True)
        cls.process = subprocess.Popen([NGINX, '-p', str(cls.root), '-c', str(path), '-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.cleanup)
        for _ in range(100):
            try:
                if cls.request('/cinema/')[0] == 200:
                    return
            except (URLError, TimeoutError):
                pass
            if cls.process.poll() is not None:
                raise RuntimeError('Nginx exited: ' + (cls.root / 'error.log').read_text())
            time.sleep(.05)
        raise RuntimeError('Nginx did not become ready')

    @classmethod
    def cleanup(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.temporary.cleanup()

    @classmethod
    def request(cls, path, headers=None, body=None):
        request = Request(cls.origin + path, headers=headers or {}, data=body)
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_public_shell_and_internal_route(self):
        self.assertEqual(self.request('/cinema/')[0], 200)
        self.assertEqual(self.request('/cinema/assets/app.js')[0], 200)
        self.assertEqual(self.request('/_cinema_identity')[0], 404)

    def test_private_catalog_cover_and_session_require_identity(self):
        for path in ('/cinema/catalog.json', '/cinema/covers/example.jpg', '/cinema/session', '/cinema/api/fs/get'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)

    def test_invalid_guest_and_disabled_accounts_are_rejected(self):
        for token in ('invalid-token', 'guest-token', 'disabled-token'):
            self.assertEqual(self.request('/cinema/catalog.json', {'Authorization': token})[0], 401)

    def test_valid_cookie_reads_private_data_and_forwards_file_request(self):
        status, headers, _ = self.request('/cinema/session', {'Authorization': 'valid-token'})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        for flag in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/cinema/'):
            self.assertIn(flag, cookie)
        headers = {'Cookie': cookie.split(';', 1)[0]}
        status, response_headers, _ = self.request('/cinema/catalog.json', headers)
        self.assertEqual(status, 200)
        self.assertIn('no-store', response_headers['Cache-Control'])
        self.assertEqual(self.request('/cinema/covers/example.jpg', headers)[0], 200)
        _, _, data = self.request('/cinema/api/fs/get', dict(headers, **{'Content-Type': 'application/json'}), b'{"path":"/m3u8/example/index.m3u8"}')
        self.assertEqual(json.loads(data)['code'], 200)

    def test_invalid_header_cannot_fall_back_to_valid_cookie(self):
        self.assertEqual(self.request('/cinema/catalog.json', {'Authorization': 'invalid-token', 'Cookie': 'cinema_session=valid-token'})[0], 401)
