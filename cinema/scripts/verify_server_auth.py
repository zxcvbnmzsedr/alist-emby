"""Run over SSH on the AList host; credentials never leave that host."""
import json
import os
from pathlib import Path
import sqlite3
import ssl
import urllib.error
import urllib.request


def request(path, headers=None, body=None):
    combined = {}
    combined.update(headers or {})
    req = urllib.request.Request(os.environ['PUBLIC_ORIGIN'].rstrip('/') + path, headers=combined, data=body)
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def main():
    token = os.environ.get('ALIST_TOKEN')
    if not token:
        with sqlite3.connect(Path(os.environ['ALIST_DATABASE']).resolve().as_uri() + '?mode=ro', uri=True) as db:
            token = db.execute("select value from x_setting_items where key='token'").fetchone()[0]
    results = {}
    for path in ['/cinema/catalog.json', '/cinema/session', '/cinema/api/fs/get']:
        status, _, _ = request(path)
        assert status == 401, (path, status)
    results['anonymousDataBlocked'] = True
    for headers in [{'Authorization': 'invalid-token'}, {'Cookie': 'cinema_session=invalid-token'}]:
        status, _, _ = request('/cinema/catalog.json', headers)
        assert status == 401, status
    results['invalidTokenBlocked'] = True
    status, headers, body = request('/cinema/session', {'Authorization': token})
    assert status == 200, status
    assert json.loads(body) == {'authenticated': True}
    set_cookie = headers['Set-Cookie']
    assert all(flag in set_cookie for flag in ['HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/cinema/'])
    cookie = set_cookie.split(';', 1)[0]
    results['validIdentitySetsSecureCookie'] = True
    status, headers, body = request('/cinema/catalog.json', {'Cookie': cookie})
    assert status == 200, status
    assert 'no-store' in headers['Cache-Control']
    catalog = json.loads(body)
    results['cookieCatalogAccess'] = True
    cover = next((video['cover'] for video in catalog['videos'] if video.get('cover')), None)
    if cover:
        for headers, expected in [({}, 401), ({'Cookie': cookie}, 200)]:
            status, _, _ = request('/cinema/' + cover, headers)
            assert status == expected, (status, expected)
        results['coverRequiresValidSession'] = True
    if not catalog['videos']:
        raise ValueError('A populated catalogue is required for playback checks')
    status, _, body = request('/cinema/api/fs/get', {'Cookie': cookie, 'Content-Type': 'application/json'},
                              json.dumps({'path': catalog['videos'][0]['path'], 'password': ''}).encode())
    assert status == 200, status
    assert json.loads(body)['code'] == 200
    results['cookiePlaybackFileAccess'] = True
    status, _, _ = request('/cinema/catalog.json', {'Cookie': cookie, 'Authorization': 'invalid-token'})
    assert status == 401
    results['invalidHeaderCannotBypassWithCookie'] = True
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
