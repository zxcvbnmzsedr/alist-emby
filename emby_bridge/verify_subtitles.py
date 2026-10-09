"""在 AList 主机验证现有影片字幕；临时测试会话在结束时撤销。"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from contextlib import closing
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from server import Bridge, LocalAccounts, stable_id


def main():
    database, config = os.environ['ALIST_DATABASE'], os.environ['ALIST_CONFIG']
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        users = [dict(row) for row in db.execute('SELECT id,username,role,disabled FROM x_users')]
        upstream = db.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()[0]
    user = next(user for user in users if 2 in json.loads(user['role']) and not user['disabled'])
    user['role'] = json.loads(user['role'])
    bridge = Bridge(os.environ['CATALOG_PATH'], os.environ['STATE_PATH'], os.environ['PUBLIC_ORIGIN'],
                    os.environ['ALIST_ORIGIN'], LocalAccounts(database, config))
    token, session = bridge.issue_session(user, upstream, client='字幕接口验收')
    origin = bridge.origin
    def request(path, method='GET', credential=token, body=None):
        headers = {'X-Emby-Token': credential} if credential else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(origin + path, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urlopen(req, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()
    try:
        ident = stable_id('movie:' + os.environ['CINEMA_TEST_VIDEO_ID'])
        status, _, raw = request('/emby/Items/' + ident + '/PlaybackInfo', 'POST', body={})
        assert status == 200
        source = json.loads(raw)['MediaSources'][0]
        tracks = [s for s in source['MediaStreams'] if s['Type'] == 'Subtitle']
        assert tracks and source['DefaultSubtitleStreamIndex'] == tracks[0]['Index']
        track = tracks[0]
        assert track['IsExternal'] and track['DeliveryMethod'] == 'External'
        path = track['DeliveryUrl'].replace('/Stream.srt', '/Stream.vtt')
        status, headers, raw = request(path, credential='')
        assert status == 200 and headers['Content-Type'].startswith('text/vtt') and raw.startswith(b'WEBVTT')
        bare = path.split('?', 1)[0]
        assert request(bare, credential='')[0] == 401
        assert request(bare, 'HEAD')[0] == 200
        status, _, srt = request(bare.replace('.vtt', '.srt'))
        assert status == 200 and srt.count(b'-->') == raw.count(b'-->') > 0
        video = bridge.video(ident)
        subtitle = bridge.subtitle_tracks(video)[0]
        obj = bridge.alist_api('fs/get', upstream, {'path': subtitle['path'], 'password': ''})
        from urllib.parse import quote
        subtitle_url = origin + '/d' + quote(subtitle['path'], safe='/') + '?sign=' + quote(obj['sign'], safe='')
        with urlopen(subtitle_url, timeout=45) as response:
            web_raw = response.read()
        local = bridge.media_root / subtitle['path'][len('/m3u8/'):]
        assert web_raw == local.read_bytes()
        print(json.dumps({'status': 'verified', 'externalTracks': len(tracks), 'defaultLanguage': track['Language'],
                          'subtitleIndex': track['Index'], 'cues': raw.count(b'-->'), 'embyVtt': True,
                          'embySrt': True, 'head': True, 'anonymousDenied': True,
                          'webSignedReadback': True, 'subtitleSha256': hashlib.sha256(web_raw).hexdigest()}, ensure_ascii=False))
    finally:
        with bridge.db() as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (session['hash'],))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}))
        sys.exit(1)
