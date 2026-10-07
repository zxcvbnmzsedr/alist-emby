"""Exercise the published entrypoint without real AList or account data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def environment(self):
        # Keep externally configured account paths out of the test process.
        env = dict(os.environ)
        env.pop('ALIST_DATABASE', None)
        env.pop('ALIST_CONFIG', None)
        return env

    def test_partial_local_account_configuration_is_rejected(self):
        for variable in ('ALIST_DATABASE', 'ALIST_CONFIG'):
            with self.subTest(variable=variable):
                env = self.environment()
                env[variable] = '/nonexistent/test-only'
                process = subprocess.run([sys.executable, str(ROOT / 'server.py')],
                                         env=env, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('must be configured together', process.stderr)

    def test_entrypoint_supports_public_root_and_prefixed_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = self.environment()
            env.update(BIND_HOST='127.0.0.1', PORT=str(port),
                       CATALOG_PATH=str(ROOT / 'examples/catalog.json'),
                       STATE_PATH=temporary, PUBLIC_ORIGIN='http://127.0.0.1:' + str(port))
            process = subprocess.Popen([sys.executable, str(ROOT / 'server.py')], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                origin = 'http://127.0.0.1:' + str(port)
                # Loopback integration tests must not use the host's HTTP proxy.
                request = build_opener(ProxyHandler({})).open
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with request(origin + '/System/Info/Public', timeout=1) as response:
                            data = json.load(response)
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline or process.poll() is not None:
                            self.fail('Published server entrypoint did not start')
                        time.sleep(0.05)
                self.assertEqual(data['ProductName'], 'alist-emby')
                with request(origin + '/emby/System/Info/Public', timeout=1) as response:
                    self.assertEqual(json.load(response)['Id'], data['Id'])
                for prefix in ('', '/emby'):
                    with self.assertRaises(HTTPError) as denied:
                        request(origin + prefix + '/Items?Recursive=true', timeout=1)
                    self.assertEqual(denied.exception.code, 401)
                    denied.exception.close()
            finally:
                process.terminate()
                process.wait(timeout=5)
