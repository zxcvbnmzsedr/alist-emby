#!/usr/bin/env python3
"""Run on NAS. Test credentials stay on NAS and are revoked in finally.

This verifies a real authorised session, NOT the user's password login or an
iOS player. No credentials, signed URLs or upstream error payloads are printed.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from server import TICKS
from verification_config import configured_bridge, upstream_token
import argparse

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-progress', action='store_true', help='Temporarily write and restore one progress record; run while this account is idle')
    parser.add_argument('--write-media', action='store_true', help='Save probed streams to the configured bridge state directory')
    args = parser.parse_args()
    os.umask(0o077)
    b = configured_bridge()
    upstream = upstream_token()
    user = b.alist_api('me', upstream)
    token, session = b.issue_session(user, upstream, 'bridge-verification', 'BridgeVerification')

    def request(path, body=None, authenticated=True, method=None):
        headers = {'X-Emby-Token': token} if authenticated else {}
        if body is not None: headers['Content-Type'] = 'application/json'
        req = Request(b.origin + '/emby' + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method)
        try: response = urlopen(req, timeout=30)
        except HTTPError as error: response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') and raw else raw


    def ffprobe(url):
        result = subprocess.run(['ffprobe', '-v', 'error', '-allowed_extensions', 'ALL',
                '-protocol_whitelist', 'file,http,https,tcp,tls,crypto', '-analyzeduration', '2000000',
                '-probesize', '2000000', '-show_entries',
                'stream=index,codec_name,codec_type,width,height,channels,sample_rate,bit_rate,profile,pix_fmt',
                '-of', 'json', url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
        if result.returncode: return None
        return json.loads(result.stdout).get('streams', [])


    report = {}
    try:
        assert request('/System/Info/Public', authenticated=False)[0] == 200
        assert request('/Items?Recursive=true', authenticated=False)[0] == 401
        assert request('/Users/AuthenticateByName', {'Username': 'bridge-nonexistent', 'Pw': 'invalid'}, False)[0] == 401
        report['publicDiscoveryAndAuthGuards'] = True
        status, views = request('/Users/' + session['uid'] + '/Views')
        assert status == 200 and views['Items'][0]['CollectionType'] == 'movies'
        status, items = request('/Items?Recursive=true&IncludeItemTypes=Movie')
        assert status == 200 and items['TotalRecordCount'] > 0
        report['libraryCount'] = items['TotalRecordCount']
        media = {}
        for ident, video in b.catalog().items():
            status, playback = request('/Items/' + ident + '/PlaybackInfo', {})
            assert status == 200
            url = playback['MediaSources'][0]['Path']
            streams = ffprobe(url)
            if not streams:
                report.setdefault('probeFailed', []).append(ident)
                continue
            entries = []
            for stream in streams:
                if stream.get('codec_type') not in ('audio', 'video'): continue
                entry = {'Index': stream['index'], 'Type': stream['codec_type'].title(), 'Codec': stream['codec_name'],
                         'IsDefault': True, 'IsExternal': False}
                for source, target in [('width', 'Width'), ('height', 'Height'), ('channels', 'Channels'),
                                       ('sample_rate', 'SampleRate'), ('bit_rate', 'BitRate')]:
                    if stream.get(source): entry[target] = int(stream[source])
                if stream.get('profile'): entry['Profile'] = stream['profile']
                if stream.get('pix_fmt'): entry['PixelFormat'] = stream['pix_fmt']
                entries.append(entry)
            media[video['id']] = {'MediaStreams': entries}
            print(json.dumps({'probe': ident, 'codecs': [s['Codec'] for s in entries]}), flush=True)
        if args.write_media:
            Path(os.environ['STATE_PATH'], 'media.json').write_text(json.dumps(media, ensure_ascii=False, indent=2))
        report['probedMovies'] = len(media)
        selected = next((item for item in items['Items'] if item['ImageTags']), items['Items'][0])
        ident = selected['Id']
        if selected['ImageTags']:
            assert request('/Items/' + ident + '/Images/Primary')[0] == 200
            assert request('/Items/' + ident + '/Images/Primary', authenticated=False)[0] == 401
            report['authenticatedCover'] = True
        status, playback = request('/Items/' + ident + '/PlaybackInfo', {})
        assert status == 200
        url = playback['MediaSources'][0]['Path']
        status, raw = request('/Videos/' + ident + '/master.m3u8')
        assert status == 200 and b'#EXT-X-KEY:METHOD=AES-128' in raw and b'#EXT-X-BYTERANGE:' in raw
        assert ('URI="' + b.origin + '/').encode() in raw
        elapsed, previous, boundary, duration = 0., None, None, 0.
        for line in raw.decode().splitlines():
            if line.startswith('#EXTINF:'): duration = float(line.split(':')[1].split(',')[0])
            elif line and not line.startswith('#'):
                if previous is not None and line != previous:
                    boundary = elapsed
                    break
                previous = line
                elapsed += duration
        for label, seconds in [('start', 0), ('crossPack', max(0, boundary - 2) if boundary else 60)]:
            result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-allowed_extensions', 'ALL',
                       '-protocol_whitelist', 'file,http,https,tcp,tls,crypto', '-ss', str(seconds), '-i', url,
                       '-t', '8', '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=90)
            report[label + 'Decode'] = result.returncode == 0
            if result.returncode: raise RuntimeError(label + ' decode failed')
            print(json.dumps({'decode': label, 'success': True}), flush=True)
        if args.check_progress:
            # Preserve any real user's progress and restore after verifying the route.
            with b.db() as db:
                previous = db.execute('SELECT data FROM userdata WHERE uid=? AND item=?', (session['uid'], ident)).fetchone()
            try:
                assert request('/Sessions/Playing/Progress', {'ItemId': ident, 'PositionTicks': 60 * TICKS})[0] == 204
                status, data = request('/Users/' + session['uid'] + '/Items/' + ident + '/UserData')
                assert status == 200 and data['PlaybackPositionTicks'] == 60 * TICKS
                report['progressRoundTrip'] = True
            finally:
                with b.db() as db:
                    if previous:
                        db.execute('INSERT OR REPLACE INTO userdata VALUES (?,?,?)', (session['uid'], ident, previous[0]))
                    else: db.execute('DELETE FROM userdata WHERE uid=? AND item=?', (session['uid'], ident))
        assert request('/Sessions/Logout', {}, method='POST')[0] == 204
        assert request('/Items?Recursive=true')[0] == 401
        report['logoutRevokesSession'] = True
    finally:
        with b.db() as db: db.execute('DELETE FROM sessions WHERE hash=?', (session['hash'],))
        Path(os.environ['STATE_PATH'], 'verification.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
