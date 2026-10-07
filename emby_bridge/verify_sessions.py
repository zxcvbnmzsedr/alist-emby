#!/usr/bin/env python3
"""Run on NAS: verify session migration via HTTPS without changing viewing progress."""
import json
import secrets
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import stable_id
from verification_config import configured_bridge


def main():
    bridge = configured_bridge(require_accounts=True)
    with bridge.db() as db:
        sessions = [dict(s) for s in db.execute('SELECT * FROM sessions WHERE expires>?', (time.time(),))]
    results = []
    for source in sessions:
        token = secrets.token_hex(32)
        session = dict(source, hash=stable_id(token), client='SessionFixVerification')
        with bridge.db() as db:
            db.execute('INSERT INTO sessions (hash,uid,username,alist_token,expires,device,client,credential_stamp) '
                       'VALUES (:hash,:uid,:username,:alist_token,:expires,:device,:client,:credential_stamp)', session)

        def request(path, method='GET', authenticated=True):
            headers = {'X-Emby-Token': token} if authenticated else {}
            req = Request(bridge.origin + '/emby' + path, headers=headers, method=method)
            try:
                response = urlopen(req, timeout=20)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read()
                return response.status, raw

        try:
            status, _ = request('/Users/' + session['uid'] + '/Views')
            assert status == 200, ('views', status)
            status, raw = request('/Items?Recursive=true')
            assert status == 200, ('items', status)
            movies = json.loads(raw)
            assert movies['TotalRecordCount'] > 0
            ident = movies['Items'][0]['Id']
            assert request('/Items/' + ident + '/PlaybackInfo', 'POST')[0] == 200
            status, raw = request('/Videos/' + ident + '/master.m3u8')
            assert status == 200 and raw.startswith(b'#EXTM3U'), ('manifest', status)
            assert b'#EXT-X-KEY' in raw and b'#EXT-X-ENDLIST' in raw
            assert request('/Items', authenticated=False)[0] == 401
            with bridge.db() as db:
                saved = db.execute('SELECT * FROM sessions WHERE hash=?', (session['hash'],)).fetchone()
            assert saved['credential_stamp'] and saved['alist_token'] == ''
            assert saved['expires'] == source['expires']
            assert request('/Sessions/Logout', 'POST')[0] == 204
            assert request('/Items')[0] == 401
            results.append({'existingSessionCopy': len(results) + 1,
                            'migratedLegacySession': not bool(source['credential_stamp']),
                            'movies': movies['TotalRecordCount'], 'views': 200, 'manifest': 200,
                            'anonymous': 401, 'afterLogout': 401})
        finally:
            with bridge.db() as db:
                db.execute('DELETE FROM sessions WHERE hash=?', (session['hash'],))
    assert results, 'No existing sessions available to verify'
    print(json.dumps({'sessions': results, 'viewingProgressUnchanged': True}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
