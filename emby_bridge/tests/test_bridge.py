import json
import base64
import hashlib
import hmac
import sqlite3
import time
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Bridge, Handler, ThreadingHTTPServer, ApiError, LocalAccounts, stable_id, TICKS


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'covers').mkdir()
        (root / 'covers' / 'abc.jpg').write_bytes(b'jpeg-test')
        self.video = {'id': 'example', 'title': 'Example', 'path': '/m3u8/example/index.m3u8',
                      'duration': 100, 'cover': 'covers/abc.jpg', 'actors': ['Alice'], 'tags': [],
                      'director': 'Director', 'studio': 'Studio',
                      'addedAt': '2000-01-29T00:00:00Z'}
        (root / 'catalog.json').write_text(json.dumps({'videos': [self.video]}))
        self.bridge = Bridge(root / 'catalog.json', root / 'state', 'https://media.test:7334')
        self.user = {'id': 1, 'username': 'owner', 'role': [2]}
        self.calls = []

        def api(path, token='', body=None):
            self.calls.append((path, token, body))
            if path == 'me': return self.user
            if path == 'auth/login':
                if body == {'username': 'owner', 'password': 'valid-password'}: return {'token': 'upstream-secret'}
                raise ApiError(401, 'Rejected')
            if path == 'fs/get': return {'sign': 'test-signature'}
            raise AssertionError(path)

        self.bridge.alist_api = api
        self.token, self.session = self.bridge.issue_session(self.user, 'upstream-secret')
        self.id = stable_id('movie:example')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.bridge = self.bridge
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def req(self, path, method='GET', body=None, token=True, headers=None):
        h = dict(headers or {})
        if token: h['X-Emby-Token'] = self.token
        if body is not None: h['Content-Type'] = 'application/json'
        try:
            response = urlopen(Request(self.base + '/emby' + path, method=method, headers=h,
                               data=json.dumps(body).encode() if body is not None else None), timeout=3)
        except HTTPError as error: response = error
        with response:
            raw = response.read()
            data = json.loads(raw) if raw and 'application/json' in response.headers.get('Content-Type', '') else raw
            return response.status, data

    def test_anonymous_and_invalid_tokens_cannot_access_catalog_or_images(self):
        for path in ['/Items?Recursive=true', '/Items/' + self.id + '/Images/Primary',
                     '/Items/' + self.id + '/PlaybackInfo', '/Videos/' + self.id + '/master.m3u8']:
            self.assertEqual(self.req(path, token=False)[0], 401)
            self.assertEqual(self.req(path, token=False, headers={'X-Emby-Token': 'invalid'})[0], 401)
        self.assertEqual(self.req('/System/Info/Public', token=False)[0], 200)

    def test_login_delegates_password_and_never_returns_upstream_token(self):
        status, data = self.req('/Users/AuthenticateByName', 'POST', {'Username': 'owner', 'Pw': 'valid-password'}, False)
        self.assertEqual(status, 200)
        self.assertIn('AccessToken', data)
        self.assertNotIn('upstream-secret', json.dumps(data))
        self.assertEqual(self.req('/Users/AuthenticateByName', 'POST', {'Username': 'owner', 'Pw': 'wrong'}, False)[0], 401)
        self.user['role'] = [0]
        self.assertEqual(self.req('/Users/AuthenticateByName', 'POST', {'Username': 'owner', 'Pw': 'valid-password'}, False)[0], 403)

    def test_emby_headers_query_token_and_user_isolation(self):
        self.assertEqual(self.req('/emby/System/Info/Public', token=False)[0], 200)
        self.assertEqual(self.req('/Items?Recursive=true&api_key=' + self.token, token=False)[0], 200)
        self.assertEqual(self.req('/Items?Recursive=true', token=False,
                        headers={'X-Emby-Authorization': 'Emby Client="Fileball", Token="' + self.token + '"'})[0], 200)
        self.assertEqual(self.req('/Users/another-user/Items')[0], 403)

    def test_browse_search_image_and_playback_contract(self):
        uid = self.session['uid']
        self.assertEqual(self.req('/Users/' + uid + '/Views')[1]['Items'][0]['CollectionType'], 'movies')
        movies = self.req('/Users/' + uid + '/Items?ParentId=1')[1]
        self.assertEqual(movies['TotalRecordCount'], 1)
        self.assertEqual(movies['Items'][0]['RunTimeTicks'], 100 * TICKS)
        for entity in movies['Items'][0]['People'] + movies['Items'][0]['Studios']:
            self.assertTrue(entity['Id'])
        self.assertEqual(self.req('/Items?Recursive=true&SearchTerm=Alice')[1]['TotalRecordCount'], 1)
        self.assertEqual(self.req('/Items?Recursive=true&SearchTerm=missing')[1]['TotalRecordCount'], 0)
        self.assertEqual(self.req('/Items/' + self.id + '/Images/Primary')[1], b'jpeg-test')
        source = self.req('/Items/' + self.id + '/PlaybackInfo', 'POST', {})[1]['MediaSources'][0]
        self.assertFalse(source['SupportsTranscoding'])
        self.assertEqual(source['Container'], 'm3u8')
        self.assertIn('/emby/Videos/' + self.id + '/master.m3u8', source['Path'])
        self.assertNotIn('upstream-secret', json.dumps(source))

    def test_native_image_tag_is_scoped_to_one_image(self):
        item = self.req('/Items?Recursive=true')[1]['Items'][0]
        tag = item['ImageTags']['Primary']
        path = '/Items/' + self.id + '/Images/Primary'
        self.assertEqual(self.req(path + '?tag=' + tag, token=False)[1], b'jpeg-test')
        self.assertEqual(self.req(path + '?tag=abc', token=False)[0], 401)
        self.assertEqual(self.req('/Items/' + self.id + '/Images/Backdrop?tag=' + tag, token=False)[0], 401)
        self.assertEqual(self.req('/Items?Recursive=true&api_key=' + tag, token=False)[0], 401)
        self.assertEqual(self.req('/Items/another/Images/Primary?tag=' + tag, token=False)[0], 401)
        self.assertEqual(self.req('/Users/' + self.session['uid'] + '/Items/' + self.id + '/SpecialFeatures')[1], [])

    def test_progress_survives_restart_favorite_and_completed(self):
        uid = self.session['uid']
        self.assertEqual(self.req('/Sessions/Playing/Progress', 'POST', {'ItemId': self.id, 'PositionTicks': 42 * TICKS})[0], 204)
        other = Bridge(self.bridge.catalog_path, Path(self.bridge.db_path).parent, self.bridge.origin)
        self.assertEqual(other.userdata(uid, self.id)['PlaybackPositionTicks'], 42 * TICKS)
        self.assertEqual(self.req('/Users/' + uid + '/Items/Resume')[1]['TotalRecordCount'], 1)
        self.assertTrue(self.req('/Users/' + uid + '/FavoriteItems/' + self.id, 'POST')[1]['IsFavorite'])
        self.assertEqual(self.req('/Sessions/Playing/Stopped', 'POST', {'ItemId': self.id, 'PositionTicks': 99 * TICKS})[0], 204)
        self.assertEqual(self.req('/Users/' + uid + '/Items/Resume')[1]['TotalRecordCount'], 0)
        self.assertTrue(other.userdata(uid, self.id)['Played'])

    def test_existing_progress_percentage_is_returned_consistently(self):
        uid = self.session['uid']
        # Existing databases contain position ticks but no percentage field.
        self.bridge.update_userdata(uid, self.id, {'PlaybackPositionTicks': 42 * TICKS})
        other = Bridge(self.bridge.catalog_path, Path(self.bridge.db_path).parent, self.bridge.origin)
        self.assertEqual(other.userdata(uid, self.id)['PlayedPercentage'], 42.0)
        for path in ['/Items?Recursive=true', '/Users/' + uid + '/Items/Resume']:
            self.assertEqual(self.req(path)[1]['Items'][0]['UserData']['PlayedPercentage'], 42.0)
        detail = self.req('/Users/' + uid + '/Items/' + self.id)[1]
        self.assertEqual(detail['UserData']['PlayedPercentage'], 42.0)
        self.assertEqual(self.req('/Users/' + uid + '/Items/' + self.id + '/UserData')[1]['PlayedPercentage'], 42.0)
        self.assertEqual(self.req('/Users/' + uid + '/FavoriteItems/' + self.id, 'POST')[1]['PlayedPercentage'], 42.0)
        completed = self.req('/Users/' + uid + '/PlayedItems/' + self.id, 'POST')[1]
        self.assertEqual(completed['PlaybackPositionTicks'], 0)
        self.assertEqual(completed['PlayedPercentage'], 100.0)
        reset = self.req('/Users/' + uid + '/PlayedItems/' + self.id, 'DELETE')[1]
        self.assertEqual(reset['PlayedPercentage'], 0.0)

    def test_resume_sorts_by_last_played_before_pagination(self):
        videos = [dict(self.video, id='movie-' + str(n),
                       addedAt=f'2000-01-{n + 1:02d}T00:00:00Z') for n in range(15)]
        self.bridge.catalog_path.write_text(json.dumps({'videos': videos}))
        uid = self.session['uid']
        ids = [stable_id('movie:' + v['id']) for v in videos]
        for n, ident in enumerate(ids):
            self.bridge.update_userdata(uid, ident, {
                'PlaybackPositionTicks': 42 * TICKS,
                'LastPlayedDate': f'2000-02-{15 - n:02d}T00:00:00Z'})
        for path in ['/Users/' + uid + '/Items/Resume', '/Items/Resume']:
            data = self.req(path + '?Limit=12&Recursive=true')[1]
            self.assertEqual(data['TotalRecordCount'], 15)
            self.assertEqual([v['Id'] for v in data['Items']], ids[:12])
            page = self.req(path + '?StartIndex=12&Limit=12')[1]
            self.assertEqual([v['Id'] for v in page['Items']], ids[12:])
        latest = self.req('/Users/' + uid + '/Items/Latest?Limit=12')[1]
        self.assertEqual([v['Id'] for v in latest], list(reversed(ids))[:12])

    def test_manifest_rewrites_key_and_segment_uris_and_preserves_ranges(self):
        raw = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="/d/m3u8/example/key?sign=k",IV=0x01\n#EXTINF:6,\n#EXT-X-BYTERANGE:16@0\n/p/cloud/raw/example/pack.ts\n#EXT-X-ENDLIST\n'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return raw
        with patch('server.urlopen', return_value=Response()):
            status, manifest = self.req('/Videos/' + self.id + '/master.m3u8')
        self.assertEqual(status, 200)
        self.assertIn(b'URI="https://media.test:7334/d/m3u8/example/key?sign=k"', manifest)
        self.assertIn(b'\nhttps://media.test:7334/p/cloud/raw/example/pack.ts', manifest)
        self.assertIn(b'#EXT-X-BYTERANGE:16@0', manifest)

    def test_revocation_logout_and_unsupported_mutations(self):
        self.assertEqual(self.req('/Items/' + self.id, 'DELETE')[0], 404)
        self.assertEqual(self.req('/Sessions/Logout', 'POST')[0], 204)
        self.assertEqual(self.req('/Items?Recursive=true')[0], 401)
        self.token, _ = self.bridge.issue_session(self.user, 'upstream-secret')
        self.user['disabled'] = True
        self.assertEqual(self.req('/Items?Recursive=true')[0], 403)

    def local_accounts(self):
        root = Path(self.temp.name)
        database = root / 'alist.sqlite'
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript('''
                CREATE TABLE x_users (id,username,role,disabled,pwd_hash,pwd_ts,salt,otp_secret);
                INSERT INTO x_users VALUES (1,'owner','[2]',0,'hashed-password',123,'salt','');
                CREATE TABLE x_setting_items (key,value);
                INSERT INTO x_setting_items VALUES ('token','service-secret');
            ''')
        config = root / 'alist.json'
        config.write_text(json.dumps({'jwt_secret': 'signing-secret'}))
        self.bridge.accounts = LocalAccounts(database, config)
        return database

    def legacy_jwt(self, **changes):
        payload = dict(username='owner', pwd_ts=123, exp=int(time.time())-1,
                       nbf=int(time.time())-172800, iat=int(time.time())-172800)
        payload.update(changes)
        encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip('=')
        data = encode(b'{"alg":"HS256"}') + '.' + encode(json.dumps(payload).encode())
        return data + '.' + encode(hmac.new(b'signing-secret', data.encode(), hashlib.sha256).digest())

    def test_local_session_survives_upstream_expiry_and_restart_without_password(self):
        self.local_accounts()
        self.token, session = self.bridge.issue_session(self.user, 'short-lived-token')
        self.assertEqual(session['alist_token'], '')
        original = self.bridge.alist_api
        def api(path, token='', body=None):
            if token == 'short-lived-token': raise ApiError(401, 'denied', 'token is expired')
            return original(path, token, body)
        restarted = Bridge(self.bridge.catalog_path, Path(self.bridge.db_path).parent,
                           self.bridge.origin, accounts=self.bridge.accounts)
        restarted.alist_api = api
        self.server.bridge = restarted
        self.assertEqual(self.req('/Items?Recursive=true')[0], 200)
        self.assertEqual(restarted.authenticate(self.token)['alist_token'], 'service-secret')
        self.assertEqual(self.req('/Sessions/Logout', 'POST')[0], 204)
        self.assertEqual(self.req('/Items?Recursive=true')[0], 401)

    def test_local_session_revocation_password_2fa_token_role_disabled_and_expiry(self):
        database = self.local_accounts()
        for sql in ["UPDATE x_users SET pwd_ts=pwd_ts+1", "UPDATE x_users SET otp_secret='new-2fa'",
                    "UPDATE x_setting_items SET value='rotated-service-secret'",
                    "UPDATE x_users SET role='[0]'", "UPDATE x_users SET disabled=1"]:
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("UPDATE x_users SET role='[2]',disabled=0,otp_secret=''")
                    db.execute("UPDATE x_setting_items SET value='service-secret'")
                self.token, _ = self.bridge.issue_session(self.user, 'upstream-secret')
                self.assertEqual(self.req('/Items?Recursive=true')[0], 200)
                with closing(sqlite3.connect(database)) as db, db: db.execute(sql)
                self.assertIn(self.req('/Items?Recursive=true')[0], (401,403))
        with self.bridge.db() as db: db.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.req('/Items?Recursive=true')[0], 401)

    def test_legacy_expired_token_migration_preserves_client_token_and_expiration(self):
        jwt = self.legacy_jwt()
        self.token, session = self.bridge.issue_session(self.user, jwt)
        self.local_accounts()
        original = self.bridge.alist_api
        def api(path, token='', body=None):
            if token == jwt: raise ApiError(401, 'denied', 'token is expired')
            return original(path, token, body)
        self.bridge.alist_api = api
        self.assertEqual(self.req('/Items?Recursive=true')[0], 200)
        with self.bridge.db() as db:
            saved = db.execute('SELECT * FROM sessions WHERE hash=?',(session['hash'],)).fetchone()
        self.assertEqual(saved['expires'], session['expires'])
        self.assertEqual(saved['alist_token'], '')
        self.assertTrue(saved['credential_stamp'])

    def test_legacy_invalid_revoked_or_password_changed_tokens_are_not_migrated(self):
        self.local_accounts()
        for token, reason in [(self.legacy_jwt(pwd_ts=122),'token is expired'),
                              (self.legacy_jwt()+'x','token is expired'),
                              (self.legacy_jwt(),'token is invalidated')]:
            with self.subTest(reason=reason, changed_password='122' in token):
                with self.bridge.db() as db:
                    db.execute('UPDATE sessions SET alist_token=?,credential_stamp=NULL WHERE hash=?',
                               (token, self.session['hash']))
                def reject(path, token='', body=None): raise ApiError(401,'denied',reason)
                self.bridge.alist_api = reject
                self.assertEqual(self.req('/Items?Recursive=true')[0], 401)


if __name__ == '__main__':
    unittest.main()
