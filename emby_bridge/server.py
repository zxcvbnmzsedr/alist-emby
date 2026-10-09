#!/usr/bin/env python3
"""Emby-compatible API adapter for an AList HLS library.

No Emby code or licence endpoints. Login delegates to AList; the initial release
admits administrators only, since catalog.json represents the complete library.
Tokens, preferences and progress are stored in a private SQLite state directory.
"""
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cinema/scripts'))
from subtitle_sidecars import discover_subtitles, subtitle_file, convert_subtitle

LOG = logging.getLogger('alist-emby')
LIBRARY = '1'
TICKS = 10_000_000


class ApiError(Exception):
    def __init__(self, status, message, upstream_reason=''):
        self.status, self.message = status, message
        self.upstream_reason = upstream_reason


def stable_id(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def admin(user):
    roles = user.get('role', [])
    return not user.get('disabled') and (roles == 2 or isinstance(roles, list) and 2 in roles)


class LocalAccounts:
    """Read-only local authority for administrator bridge sessions; no passwords saved."""
    def __init__(self, database, config):
        self.database, self.config = database, config

    def identity(self, session):
        try:
            with closing(sqlite3.connect(Path(self.database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('SELECT * FROM x_users WHERE username=?', (session['username'],)).fetchone()
                token_row = db.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()
            if not row or not token_row or not token_row[0]:
                raise ApiError(401, 'Account no longer permitted')
            user = dict(row)
            user['role'] = json.loads(user['role'])
            if not admin(user) or stable_id('user:' + str(user['id'])) != session['uid']:
                raise ApiError(403, 'Account no longer permitted')
            config = json.loads(Path(self.config).read_text())
            secret = config['jwt_secret']
            # Password/2FA changes and either signing-key or API-token rotation revoke sessions.
            values = [user.get(k) for k in ('id', 'username', 'pwd_hash', 'pwd_ts', 'salt', 'otp_secret')]
            values += [secret, token_row[0]]
            stamp = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
            return user, token_row[0], stamp, secret
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            raise ApiError(503, 'Local account authority unavailable') from None

    def verify_legacy(self, session, user, secret):
        # Only stored, still-live bridge sessions qualify. Never accept a JWT from a client here.
        try:
            head, body, signature = session['alist_token'].split('.')
            decode = lambda value: base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
            expected = hmac.new(secret.encode(), (head + '.' + body).encode(), hashlib.sha256).digest()
            if json.loads(decode(head)).get('alg') != 'HS256' or not hmac.compare_digest(decode(signature), expected):
                raise ValueError()
            claims = json.loads(decode(body))
            if (claims['username'] != user['username'] or claims['pwd_ts'] != user['pwd_ts']
                    or claims['nbf'] > time.time() or claims['iat'] > time.time()):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ApiError(401, 'Legacy session cannot be renewed') from None


class Bridge:
    def __init__(self, catalog, state, origin, alist='http://127.0.0.1:5244', accounts=None, media_root=None):
        self.accounts = accounts
        self.catalog_path = Path(catalog)
        self.media_root = Path(media_root or os.environ.get('MEDIA_ROOT', '/m3u8'))
        self.origin, self.alist = origin.rstrip('/'), alist.rstrip('/')
        Path(state).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = str(Path(state) / 'state.sqlite')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, uid TEXT, username TEXT, alist_token TEXT,
                    expires REAL, device TEXT, client TEXT);
                CREATE TABLE IF NOT EXISTS userdata (
                    uid TEXT, item TEXT, data TEXT, PRIMARY KEY(uid,item));
                CREATE TABLE IF NOT EXISTS preferences (
                    uid TEXT, name TEXT, data TEXT, PRIMARY KEY(uid,name));
            ''')
            if 'credential_stamp' not in {row[1] for row in db.execute('PRAGMA table_info(sessions)')}:
                db.execute('ALTER TABLE sessions ADD COLUMN credential_stamp TEXT')
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('server_id', secrets.token_hex(16)))
            self.server_id = db.execute('SELECT value FROM settings WHERE key=?', ('server_id',)).fetchone()[0]
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('image_secret', secrets.token_hex(32)))
            self.image_secret = db.execute('SELECT value FROM settings WHERE key=?', ('image_secret',)).fetchone()[0]
        os.chmod(self.db_path, 0o600)
        self.identity_cache = {}
        self.login_attempts = {}
        self.lock = threading.Lock()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def alist_api(self, path, token='', body=None):
        req = Request(self.alist + '/api/' + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': token, 'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=15) as response:
                data = json.load(response)
        except (HTTPError, URLError, TimeoutError):
            raise ApiError(502, 'AList unavailable') from None
        if data.get('code') != 200:
            raise ApiError(401 if path.startswith('auth/') or path == 'me' else 502,
                           'AList request denied', data.get('message', ''))
        return data.get('data')

    def issue_session(self, user, alist_token, device='', client=''):
        if not admin(user):
            raise ApiError(403, 'This bridge currently requires an AList administrator account')
        token = secrets.token_hex(32)
        session = {'hash': stable_id(token), 'uid': stable_id('user:' + str(user['id'])),
                   'username': user['username'], 'alist_token': alist_token,
                   'expires': time.time() + 30 * 86400, 'device': device, 'client': client}
        session['credential_stamp'] = None
        if self.accounts:
            _, upstream, stamp, _ = self.accounts.identity(session)
            self.check_identity(self.alist_api('me', upstream), session)
            session.update(credential_stamp=stamp, alist_token='')
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO sessions (hash,uid,username,alist_token,expires,device,client,credential_stamp) '
                       'VALUES (:hash,:uid,:username,:alist_token,:expires,:device,:client,:credential_stamp)', session)
        return token, session

    @staticmethod
    def check_identity(user, session):
        if not admin(user) or stable_id('user:' + str(user['id'])) != session['uid']:
            raise ApiError(403, 'Account no longer permitted')

    def authenticate(self, token):
        if not token:
            raise ApiError(401, 'Authentication required')
        key = stable_id(token)
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE hash=? AND expires>?', (key, time.time())).fetchone()
        if not row:
            raise ApiError(401, 'Invalid or expired token')
        session = dict(row)
        if self.accounts:
            user, upstream, stamp, secret = self.accounts.identity(session)
            if session['credential_stamp']:
                if not hmac.compare_digest(session['credential_stamp'], stamp):
                    raise ApiError(401, 'Account credentials changed; sign in again')
            else:
                try:
                    self.check_identity(self.alist_api('me', session['alist_token']), session)
                except ApiError as error:
                    # Do not revive explicitly revoked tokens or failed/disabled accounts.
                    if error.status != 401 or error.upstream_reason != 'token is expired':
                        raise
                    self.accounts.verify_legacy(session, user, secret)
                self.check_identity(self.alist_api('me', upstream), session)
                with self.db() as db:
                    db.execute('UPDATE sessions SET credential_stamp=?, alist_token=? WHERE hash=? AND credential_stamp IS NULL',
                               (stamp, '', key))
                session['credential_stamp'] = stamp
            session['alist_token'] = upstream
        elif session['credential_stamp']:
            raise ApiError(503, 'Local account authority is required')
        with self.lock:
            checked = self.identity_cache.get(key, 0)
        if time.time() - checked > 15:
            user = self.alist_api('me', session['alist_token'])
            self.check_identity(user, session)
            with self.lock:
                if len(self.identity_cache) > 1000:
                    self.identity_cache.clear()
                self.identity_cache[key] = time.time()
        return session

    def login(self, body, ip, device, client):
        with self.lock:
            attempts = [t for t in self.login_attempts.get(ip, []) if time.time() - t < 60]
            if len(attempts) >= 10:
                raise ApiError(429, 'Too many login attempts; retry in one minute')
            self.login_attempts[ip] = attempts + [time.time()]
        if not isinstance(body.get('Username'), str) or not isinstance(body.get('Pw'), str):
            raise ApiError(400, 'Username and Pw are required')
        result = self.alist_api('auth/login', body={'username': body['Username'], 'password': body['Pw']})
        user = self.alist_api('me', result['token'])
        token, session = self.issue_session(user, result['token'], device, client)
        return {'AccessToken': token, 'ServerId': self.server_id,
                'User': self.user(session), 'SessionInfo': self.session_info(session)}

    def user(self, session):
        return {'Id': session['uid'], 'Name': session['username'], 'ServerId': self.server_id,
                'HasPassword': True, 'HasConfiguredPassword': True,
                'LastLoginDate': now(), 'LastActivityDate': now(),
                'Configuration': self.preference(session['uid'], 'configuration', {
                    'PlayDefaultAudioTrack': True, 'SubtitleMode': 'Default',
                    'OrderedViews': [LIBRARY], 'LatestItemsExcludes': [], 'MyMediaExcludes': [],
                    'EnableNextEpisodeAutoPlay': False}),
                'Policy': {'IsAdministrator': False, 'IsHidden': True, 'IsDisabled': False,
                    'EnableMediaPlayback': True, 'EnableRemoteAccess': True,
                    'EnableAllFolders': True, 'EnabledFolders': [LIBRARY], 'EnableAllDevices': True,
                    'EnableUserPreferenceAccess': True, 'EnableContentDeletion': False,
                    'EnableContentDownloading': False, 'EnableVideoPlaybackTranscoding': False,
                    'EnableAudioPlaybackTranscoding': False, 'EnablePlaybackRemuxing': False}}

    def session_info(self, session):
        return {'Id': session['hash'], 'UserId': session['uid'], 'UserName': session['username'],
                'ServerId': self.server_id, 'Client': session['client'], 'DeviceId': session['device'],
                'DeviceName': session['device'], 'LastActivityDate': now(),
                'PlayableMediaTypes': ['Video'], 'SupportedCommands': [], 'SupportsRemoteControl': False}

    def preference(self, uid, name, default):
        with self.db() as db:
            row = db.execute('SELECT data FROM preferences WHERE uid=? AND name=?', (uid, name)).fetchone()
        return json.loads(row[0]) if row else default

    def save_preference(self, uid, name, data):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?,?)', (uid, name, json.dumps(data)))

    def catalog(self):
        data = json.loads(self.catalog_path.read_text())
        result = {}
        for video in data['videos']:
            path = video.get('path', '')
            if not path.startswith('/m3u8/') or not path.endswith('/index.m3u8') or any(p in ('.', '..') for p in path.split('/')):
                continue
            result[stable_id('movie:' + video['id'])] = video
        return result

    def video(self, item):
        video = self.catalog().get(item)
        if video is None:
            raise ApiError(404, 'Item not found')
        return video

    def progress_dto(self, data, video):
        # Derive this on reads so pre-existing progress needs no database migration.
        if video is not None:
            duration = round(video['duration'] * TICKS)
            percentage = data['PlaybackPositionTicks'] / duration * 100 if duration > 0 else 0.0
            data['PlayedPercentage'] = 100.0 if data['Played'] else min(100.0, max(0.0, percentage))
        return data

    def userdata(self, uid, item, video=None):
        with self.db() as db:
            row = db.execute('SELECT data FROM userdata WHERE uid=? AND item=?', (uid, item)).fetchone()
        data = json.loads(row[0]) if row else {'Key': item, 'ItemId': item, 'PlaybackPositionTicks': 0,
                                             'PlayCount': 0, 'IsFavorite': False, 'Played': False}
        return self.progress_dto(data, video if video is not None else self.catalog().get(item))

    def update_userdata(self, uid, item, values):
        video = self.video(item)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM userdata WHERE uid=? AND item=?', (uid, item)).fetchone()
            data = json.loads(row[0]) if row else self.userdata(uid, item, video)
            data.update(values)
            # The percentage is derived from the current duration, never persisted.
            data.pop('PlayedPercentage', None)
            db.execute('INSERT OR REPLACE INTO userdata VALUES (?,?,?)', (uid, item, json.dumps(data)))
        return self.progress_dto(data, video)

    def folder(self, session, root=False):
        return {'Id': 'root' if root else LIBRARY, 'Name': 'alist-emby', 'ServerId': self.server_id,
                'Type': 'UserRootFolder' if root else 'CollectionFolder', 'CollectionType': 'movies',
                'IsFolder': True, 'ChildCount': len(self.catalog()), 'ImageTags': {},
                'DisplayPreferencesId': LIBRARY, 'UserData': self.userdata(session['uid'], LIBRARY)}

    def item(self, ident, video, session):
        images = {'Primary': self.image_tag(ident, 'primary', video)} if video.get('cover') else {}
        data = {'Id': ident, 'ServerId': self.server_id, 'ParentId': LIBRARY, 'Name': video['title'],
                'SortName': video['title'], 'OriginalTitle': video.get('originalTitle', ''),
                'Type': 'Movie', 'MediaType': 'Video', 'VideoType': 'VideoFile', 'IsFolder': False,
                'LocationType': 'Remote', 'Container': 'm3u8', 'Overview': video.get('description', ''),
                'RunTimeTicks': round(video['duration'] * TICKS), 'DateCreated': video.get('addedAt'),
                'Genres': video.get('tags', []), 'Tags': video.get('tags', []), 'ProviderIds': {},
                'People': [{'Name': name, 'Type': 'Actor', 'Id': stable_id('person:' + name)} for name in video.get('actors', [])],
                'Studios': [{'Name': video['studio'], 'Id': stable_id('studio:' + video['studio'])}] if video.get('studio') else [],
                'ImageTags': images, 'BackdropImageTags': [self.image_tag(ident, 'backdrop', video)] if video.get('backdrop') else [],
                'PrimaryImageAspectRatio': 0.67, 'UserData': self.userdata(session['uid'], ident, video),
                'CanDownload': False, 'CanDelete': False, 'SupportsSync': False,
                'PlayAccess': 'Full', 'IsPlaceHolder': False, 'HasSubtitles': bool(self.subtitle_tracks(video))}
        if str(video.get('year', '')).isdigit():
            data['ProductionYear'] = int(video['year'])
        if video.get('premiered'):
            data['PremiereDate'] = video['premiered'] + 'T00:00:00Z'
        if video.get('director'):
            data['People'].append({'Name': video['director'], 'Type': 'Director', 'Id': stable_id('person:' + video['director'])})
        return data

    def image_tag(self, ident, kind, video):
        asset = video.get('cover' if kind == 'primary' else 'backdrop', '')
        return hmac.new(bytes.fromhex(self.image_secret), (ident + ':' + kind + ':' + asset).encode(), hashlib.sha256).hexdigest()

    def image(self, ident, kind):
        v = self.video(ident)
        relative = v.get('cover' if kind == 'primary' else 'backdrop', '')
        if not re.fullmatch(r'covers/[\w.-]+\.(?:jpg|jpeg|png|webp|avif)', relative):
            raise ApiError(404, 'No image')
        root = self.catalog_path.parent.resolve()
        image = (root / relative).resolve()
        if not image.is_relative_to(root) or not image.is_file():
            raise ApiError(404, 'No image')
        return image.read_bytes(), mimetypes.guess_type(image.name)[0] or 'image/jpeg'

    def items(self, session, query, mode=''):
        rows = [self.item(i, v, session) for i, v in self.catalog().items()]
        term = query.get('searchterm', '').casefold()
        if term:
            rows = [v for v in rows if term in (v['Name'] + ' ' + ' '.join(p['Name'] for p in v['People'])).casefold()]
        if query.get('ids'):
            rows = [v for v in rows if v['Id'] in query['ids'].split(',')]
        if query.get('parentid') not in (None, '', LIBRARY, 'root'):
            rows = []
        if query.get('includeitemtypes') and 'movie' not in query['includeitemtypes'].lower().split(','):
            rows = []
        filters = query.get('filters', '').lower().split(',')
        if mode == 'resume' or 'isresumable' in filters:
            rows = [v for v in rows if v['UserData']['PlaybackPositionTicks'] > 0 and not v['UserData']['Played']]
        if 'isfavorite' in filters or query.get('isfavorite') == 'true':
            rows = [v for v in rows if v['UserData']['IsFavorite']]
        if 'isunplayed' in filters or query.get('isplayed') == 'false':
            rows = [v for v in rows if not v['UserData']['Played']]
        if 'isplayed' in filters or query.get('isplayed') == 'true':
            rows = [v for v in rows if v['UserData']['Played']]
        field = query.get('sortby', 'DatePlayed' if mode == 'resume' else 'DateCreated').split(',')[0].lower()
        rows.sort(key=lambda v: (v['UserData'].get('LastPlayedDate', '') if field == 'dateplayed' else
                                v['Name'] if field in ('sortname', 'name') else v.get('DateCreated') or ''))
        if mode in ('latest', 'resume') or query.get('sortorder', 'Descending').lower() == 'descending':
            rows.reverse()
        total = len(rows)
        start, limit = max(0, int(query.get('startindex', 0))), min(1000, max(0, int(query.get('limit', 100))))
        return {'Items': rows[start:start + limit], 'TotalRecordCount': total, 'StartIndex': start}

    def source_url(self, video, session):
        data = self.alist_api('fs/get', session['alist_token'], {'path': video['path'], 'password': ''})
        if not data.get('sign'):
            raise ApiError(502, 'Signed playback URL required')
        return self.origin + '/d' + quote(video['path'], safe='/') + '?sign=' + quote(data['sign'], safe='')

    def subtitle_tracks(self, video):
        logical = video['path'].rsplit('/', 1)[0]
        relative = logical[len('/m3u8/'):]
        folder = self.media_root / relative
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.media_root.resolve()) or not folder.is_dir():
            return []
        return discover_subtitles(folder, logical)

    def media_streams(self, ident, video, token):
        probe_path = Path(self.db_path).parent / 'media.json'
        probe = json.loads(probe_path.read_text()).get(video['id'], {}) if probe_path.is_file() else {}
        streams = [dict(s) for s in probe.get('MediaStreams', []) if s.get('Type') != 'Subtitle']
        first = max([1] + [s['Index'] for s in streams]) + 1
        for n, track in enumerate(self.subtitle_tracks(video), first):
            delivery = f'/emby/Videos/{ident}/{ident}/Subtitles/{n}/Stream.{track["format"]}'
            if token:
                delivery += '?api_key=' + quote(token, safe='')
            streams.append({'Index': n, 'Type': 'Subtitle', 'Codec': 'webvtt' if track['format'] == 'vtt' else 'srt',
                            'Language': track['language'], 'Title': track['title'], 'DisplayTitle': track['title'],
                            'IsDefault': track['default'], 'IsForced': False, 'IsExternal': True,
                            'IsTextSubtitleStream': True, 'SupportsExternalStream': True,
                            'DeliveryMethod': 'External', 'DeliveryUrl': delivery})
        return streams

    def subtitle(self, ident, source, index, output_format, session, start_ticks=0, copy_timestamps=True):
        if source != ident:
            raise ApiError(404, 'Media source not found')
        video = self.video(ident)
        # 与播放相同的影片访问权限检查。
        self.source_url(video, session)
        streams = self.media_streams(ident, video, '')
        subtitles = [s for s in streams if s['Type'] == 'Subtitle']
        selected = next((n for n, s in enumerate(subtitles) if s['Index'] == index), None)
        if selected is None:
            raise ApiError(404, 'Subtitle stream not found')
        logical = video['path'].rsplit('/', 1)[0]
        folder = self.media_root / logical[len('/m3u8/'):]
        track = self.subtitle_tracks(video)[selected]
        try:
            path = subtitle_file(track, logical, folder)
            raw = convert_subtitle(path.read_bytes(), output_format, start_ticks, copy_timestamps)
        except (ValueError, UnicodeError, OSError):
            raise ApiError(400, 'Subtitle unavailable or unsupported format') from None
        return raw, 'text/vtt; charset=utf-8' if output_format == 'vtt' else 'application/x-subrip; charset=utf-8'

    def playback(self, ident, session, token):
        video = self.video(ident)
        # Check file permission now, rather than waiting until the first stream request.
        self.source_url(video, session)
        stream = '/emby/Videos/' + ident + '/master.m3u8?api_key=' + quote(token)
        source = {'Id': ident, 'Name': '原画 HLS', 'Protocol': 'Http', 'Path': self.origin + stream,
                  'Type': 'Default', 'Container': 'm3u8', 'IsRemote': True,
                  'RunTimeTicks': round(video['duration'] * TICKS), 'SupportsDirectPlay': True,
                  'SupportsDirectStream': True, 'SupportsTranscoding': False,
                  'IsInfiniteStream': False, 'RequiresOpening': False, 'RequiresClosing': False,
                  'DirectStreamUrl': stream, 'MediaStreams': [], 'Formats': ['hls'],
                  'RequiredHttpHeaders': {}, 'DefaultSubtitleStreamIndex': -1}
        source['MediaStreams'] = self.media_streams(ident, video, token)
        if any(s['Type'] == 'Audio' for s in source['MediaStreams']):
            source['DefaultAudioStreamIndex'] = next(s['Index'] for s in source['MediaStreams'] if s['Type'] == 'Audio')
        source['DefaultSubtitleStreamIndex'] = next((s['Index'] for s in source['MediaStreams']
            if s['Type'] == 'Subtitle' and s['IsDefault']), -1)
        return {'MediaSources': [source], 'PlaySessionId': secrets.token_hex(16)}

    def manifest(self, ident, session):
        url = self.source_url(self.video(ident), session)
        # Fetch through loopback AList; clients get absolute public key/segment URLs.
        local = self.alist + url[len(self.origin):]
        try:
            with urlopen(local, timeout=20) as response:
                raw = response.read(8_000_001)
        except (HTTPError, URLError, TimeoutError):
            raise ApiError(502, 'Playlist unavailable') from None
        if len(raw) > 8_000_000 or not raw.startswith(b'#EXTM3U'):
            raise ApiError(502, 'Invalid playlist')
        lines = []
        for line in raw.decode('utf-8-sig').splitlines():
            if line and not line.startswith('#'):
                line = urljoin(url, line)
            else:
                line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + urljoin(url, m[1]) + '"', line)
            lines.append(line)
        return ('\n'.join(lines) + '\n').encode()


class Handler(BaseHTTPRequestHandler):
    server_version = 'alist-emby/0.1'

    def log_message(self, fmt, *args):
        pass  # Request URLs may contain tokens; never log them.

    def respond(self, value=None, status=200, content_type='application/json'):
        self.response_status = status
        self.response_count = len(value) if isinstance(value, list) else len(value['Items']) if isinstance(value, dict) and isinstance(value.get('Items'), list) else None
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        if status == 204:
            raw = b''
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def do_GET(self): self.handle_api()
    def do_HEAD(self): self.handle_api()
    def do_POST(self): self.handle_api()
    def do_DELETE(self): self.handle_api()

    def handle_api(self):
        path = ''
        query = {}
        status = 200
        try:
            split = urlsplit(self.path)
            path = unquote(split.path).rstrip('/') or '/'
            path = re.sub(r'^(?:/emby)+(?=/|$)', '', path, flags=re.I) or '/'
            query = {k.lower(): v[-1] for k, v in parse_qs(split.query).items()}
            length = int(self.headers.get('Content-Length', 0))
            if length < 0 or length > 65536:
                raise ApiError(413, 'Request too large')
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ApiError(400, 'Expected JSON object')
            self.route(path, query, body)
        except ApiError as e:
            status = e.status
            self.respond({'ErrorCode': str(e.status), 'Message': e.message}, e.status)
        except (ValueError, TypeError, KeyError):
            status = 400
            self.respond({'Message': 'Invalid request or catalog data'}, 400)
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception as error:
            status = 500
            LOG.error('Internal error class=%s', type(error).__name__)
            self.respond({'Message': 'Internal server error'}, 500)
        finally:
            # Only whitelisted library filters; never tokens, credentials or search text.
            safe = {k: v[:250] for k, v in query.items() if k in {
                'parentid', 'includeitemtypes', 'excludeitemtypes', 'recursive', 'limit',
                'startindex', 'fields', 'filters', 'sortby', 'sortorder', 'isfolder', 'isplayed'}}
            LOG.info('%s %s status=%s count=%s filters=%s client=%s', self.command, path[:180], getattr(self, 'response_status', status),
                     getattr(self, 'response_count', None), json.dumps(safe),
                     re.sub(r'[^a-zA-Z0-9 ._/-]', '', self.headers.get('User-Agent', ''))[:100])

    def route(self, path, q, body):
        b = self.server.bridge
        p = path.lower()
        read = self.command in ('GET', 'HEAD')
        auth = self.headers.get('X-Emby-Authorization') or self.headers.get('Authorization', '')
        fields = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', auth)}
        token = self.headers.get('X-Emby-Token') or q.get('api_key') or q.get('x-emby-token') or fields.get('token', '')
        if p == '/system/info/public' and read:
            return self.respond({'Id': b.server_id, 'ServerName': 'alist-emby', 'Version': '4.8.11.0',
                                 'ProductName': 'alist-emby', 'OperatingSystem': 'Linux',
                                 'LocalAddress': b.origin, 'WanAddress': b.origin, 'StartupWizardCompleted': True})
        if p == '/users/public' and read:
            return self.respond([])
        if p == '/web/manifest.json' and read:
            return self.respond({'name': 'alist-emby', 'short_name': 'alist-emby', 'display': 'standalone', 'icons': [], 'start_url': '/emby/'})
        if p == '/users/authenticatebyname' and self.command == 'POST':
            return self.respond(b.login(body, self.headers.get('X-Real-IP', self.client_address[0]),
                                        fields.get('deviceid', ''), fields.get('client', '')))
        image_match = re.fullmatch(r'/items/([^/]+)/images/(primary|backdrop)(?:/\d+)?', p)
        if image_match and read and not token and q.get('tag'):
            # Native image loaders may send only ImageTags, without session headers.
            # The HMAC is a capability for precisely one image, not an API token.
            ident, kind = image_match.groups()
            v = b.catalog().get(ident)
            if v and hmac.compare_digest(q['tag'], b.image_tag(ident, kind, v)):
                raw, mime = b.image(ident, kind)
                return self.respond(raw, content_type=mime)
        s = b.authenticate(token)
        uid = s['uid']
        match = re.match(r'/users/([^/]+)', p)
        if match and match[1] not in (uid, 'me'):
            raise ApiError(403, 'User does not match session')
        if p in ('/system/info', '/system/info/private') and read:
            return self.respond({'Id': b.server_id, 'ServerName': 'alist-emby', 'Version': '4.8.11.0',
                                 'ProductName': 'alist-emby', 'OperatingSystem': 'Linux',
                                 'LocalAddress': b.origin, 'WanAddress': b.origin,
                                 'SupportsLibraryMonitor': False, 'HasUpdateAvailable': False})
        if p in ('/users/me', '/users/' + uid) and read:
            return self.respond(b.user(s))
        if p == '/users' and read:
            return self.respond([b.user(s)])
        if p == '/sessions/logout' and self.command == 'POST':
            with b.db() as db: db.execute('DELETE FROM sessions WHERE hash=?', (s['hash'],))
            return self.respond(status=204)
        if p in ('/sessions/capabilities', '/sessions/capabilities/full') and self.command == 'POST':
            return self.respond(status=204)
        if p == '/sessions' and read:
            return self.respond([b.session_info(s)])
        if p == '/users/' + uid + '/configuration' and self.command == 'POST':
            b.save_preference(uid, 'configuration', body)
            return self.respond(status=204)
        if p.startswith('/displaypreferences/'):
            name = p + ':' + q.get('client', '')
            if self.command == 'POST':
                b.save_preference(uid, name, body)
                return self.respond(status=204)
            if read:
                return self.respond(b.preference(uid, name, {'Id': path.split('/')[-1], 'SortBy': 'SortName',
                    'SortOrder': 'Ascending', 'ViewType': 'Poster', 'CustomPrefs': {}, 'ScrollDirection': 'Vertical'}))
        if p in ('/users/' + uid + '/views', '/library/mediafolders') and read:
            return self.respond({'Items': [b.folder(s)], 'TotalRecordCount': 1, 'StartIndex': 0})
        if p == '/users/' + uid + '/groupingoptions' and read:
            return self.respond([])
        if p == '/users/' + uid + '/items/root' and read:
            return self.respond(b.folder(s, True))
        if p in ('/items', '/users/' + uid + '/items') and read:
            if q.get('parentid') == 'root' or (not q.get('parentid') and q.get('recursive') != 'true' and not q.get('includeitemtypes') and not q.get('searchterm') and not q.get('ids')):
                return self.respond({'Items': [b.folder(s)], 'TotalRecordCount': 1, 'StartIndex': 0})
            return self.respond(b.items(s, q))
        if p in ('/users/' + uid + '/items/latest', '/items/latest') and read:
            return self.respond(b.items(s, q, 'latest')['Items'])
        if p in ('/users/' + uid + '/items/resume', '/items/resume') and read:
            return self.respond(b.items(s, q, 'resume'))
        if p in ('/shows/nextup', '/shows/upcoming', '/livetv/channels', '/channels') and read:
            return self.respond({'Items': [], 'TotalRecordCount': 0, 'StartIndex': 0})
        if p == '/movies/recommendations' and read:
            return self.respond([])
        if p == '/items/counts' and read:
            return self.respond({'MovieCount': len(b.catalog()), 'SeriesCount': 0, 'EpisodeCount': 0,
                                 'SongCount': 0, 'AlbumCount': 0, 'ArtistCount': 0, 'BoxSetCount': 0})
        match = re.fullmatch(r'/(?:users/[^/]+/)?items/([^/]+)', p)
        if match and read:
            ident = match[1]
            if ident == LIBRARY:
                return self.respond(b.folder(s))
            v = b.video(ident)
            data = b.item(ident, v, s)
            data['MediaSources'] = b.playback(ident, s, token)['MediaSources']
            data['MediaStreams'] = data['MediaSources'][0]['MediaStreams']
            return self.respond(data)
        match = re.fullmatch(r'/items/([^/]+)/images/(primary|backdrop)(?:/\d+)?', p)
        if match and read:
            raw, mime = b.image(match[1], match[2])
            return self.respond(raw, content_type=mime)
        match = re.fullmatch(r'/(?:users/[^/]+/)?items/([^/]+)/(specialfeatures|localtrailers)', p)
        if match and read:
            b.video(match[1])
            return self.respond([])
        match = re.fullmatch(r'/items/([^/]+)/playbackinfo', p)
        if match and (read or self.command == 'POST'):
            return self.respond(b.playback(match[1], s, token))
        match = re.fullmatch(r'/videos/([^/]+)/(?:master\.m3u8|main\.m3u8|stream(?:\.m3u8)?)', p)
        if match and read:
            return self.respond(b.manifest(match[1], s), content_type='application/vnd.apple.mpegurl')
        match = re.fullmatch(r'/videos/([^/]+)/([^/]+)/subtitles/(\d+)(?:/(\d+))?/stream\.(vtt|srt)', p)
        if match and read:
            ident, source, index, position, output_format = match.groups()
            raw, mime = b.subtitle(ident, source, int(index), output_format, s,
                int(position or q.get('startpositionticks', 0)), q.get('copytimestamps', 'true').lower() == 'true')
            return self.respond(raw, content_type=mime)
        if p in ('/sessions/playing', '/sessions/playing/progress', '/sessions/playing/stopped') and self.command == 'POST':
            ident = str(body.get('ItemId', ''))
            video = b.video(ident)
            current = b.userdata(uid, ident)
            ticks = min(round(video['duration'] * TICKS), max(0, int(body.get('PositionTicks', current['PlaybackPositionTicks']))))
            values = {'PlaybackPositionTicks': ticks, 'LastPlayedDate': now()}
            if p.endswith('/stopped') and ticks >= video['duration'] * TICKS * .95:
                values.update(Played=True, PlaybackPositionTicks=0, PlayCount=current['PlayCount'] + 1)
            b.update_userdata(uid, ident, values)
            return self.respond(status=204)
        match = re.fullmatch(r'/users/[^/]+/(favoriteitems|playeditems)/([^/]+)', p)
        if match and self.command in ('POST', 'DELETE'):
            values = {('IsFavorite' if match[1] == 'favoriteitems' else 'Played'): self.command == 'POST'}
            if match[1] == 'playeditems': values['PlaybackPositionTicks'] = 0
            return self.respond(b.update_userdata(uid, match[2], values))
        match = re.fullmatch(r'/users/[^/]+/items/([^/]+)/userdata', p)
        if match and read:
            b.video(match[1])
            return self.respond(b.userdata(uid, match[1]))
        if p == '/branding/configuration' and read:
            return self.respond({'LoginDisclaimer': 'alist-emby · 使用 AList 管理员账号登录', 'CustomCss': ''})
        if p in ('/plugins', '/scheduledtasks') and read:
            return self.respond([])
        raise ApiError(404, 'Endpoint not implemented')


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    database, config = os.environ.get('ALIST_DATABASE'), os.environ.get('ALIST_CONFIG')
    if bool(database) != bool(config):
        raise SystemExit('ALIST_DATABASE and ALIST_CONFIG must be configured together')
    accounts = LocalAccounts(database, config) if database else None
    bridge = Bridge(os.environ.get('CATALOG_PATH', 'catalog/catalog.json'),
                    os.environ.get('STATE_PATH', 'state'),
                    os.environ.get('PUBLIC_ORIGIN', 'http://127.0.0.1:8097'),
                    os.environ.get('ALIST_ORIGIN', 'http://127.0.0.1:5244'), accounts)
    server = ThreadingHTTPServer((os.environ.get('BIND_HOST', '127.0.0.1'), int(os.environ.get('PORT', '8097'))), Handler)
    server.bridge = bridge
    LOG.info('alist-emby listening on %s:%s', *server.server_address)
    server.serve_forever()


if __name__ == '__main__':
    main()
