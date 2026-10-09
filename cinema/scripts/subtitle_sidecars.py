"""发现清单同目录的 UTF-8 字幕，并提供 SRT / WebVTT 转换。"""
import re
from pathlib import Path

MAX_SUBTITLE_BYTES = 8_000_000
LANGUAGES = {'zh': ('zho', '中文'), 'zh-cn': ('zho', '简体中文'), 'zh-tw': ('zho', '繁体中文'),
             'ja': ('jpn', '日语'), 'en': ('eng', '英语')}


def discover_subtitles(folder, logical_folder):
    folder = Path(folder)
    groups = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in ('.srt', '.vtt') or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > MAX_SUBTITLE_BYTES or not path.resolve().is_relative_to(folder.resolve()):
            continue
        # 同名 SRT / VTT 是同一轨道，优先使用 VTT。
        old = groups.get(path.stem)
        if old is None or path.suffix.lower() == '.vtt':
            groups[path.stem] = path
    tracks = []
    for stem, path in groups.items():
        match = re.search(r'(?:^|[._-])(zh-cn|zh-tw|zh|ja|en)(?:$|[._-])', stem, re.I)
        language, title = LANGUAGES.get(match[1].lower() if match else '', ('und', stem))
        tracks.append({'path': logical_folder + '/' + path.name, 'format': path.suffix.lower()[1:],
                       'language': language, 'title': title, 'default': False})
    tracks.sort(key=lambda t: (t['language'] != 'zho', t['path']))
    if tracks:
        tracks[0]['default'] = True
    return tracks


def subtitle_file(track, logical_folder, folder):
    """只允许读取当前影片目录中的普通字幕文件。"""
    path = track.get('path', '')
    if not isinstance(path, str) or not path.startswith(logical_folder + '/'):
        raise ValueError('字幕不在影片目录内')
    name = path[len(logical_folder) + 1:]
    if not name or '/' in name or '\\' in name or Path(name).suffix.lower() not in ('.srt', '.vtt'):
        raise ValueError('无效的字幕路径')
    root = Path(folder).resolve()
    target = Path(folder) / name
    if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
        raise ValueError('字幕文件不存在')
    if target.stat().st_size > MAX_SUBTITLE_BYTES:
        raise ValueError('字幕文件过大')
    return target


def convert_subtitle(raw, output_format, start_ticks=0, copy_timestamps=True):
    if output_format not in ('srt', 'vtt'):
        raise ValueError('仅支持 SRT 和 WebVTT 字幕')
    text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    stamp = r'(?:\d+:)?\d{2}:\d{2}[.,]\d{3}'
    timing = re.compile(rf'^({stamp})\s+-->\s+({stamp})(?:\s+.*)?$')
    def milliseconds(value):
        parts = value.replace(',', '.').split(':')
        seconds = float(parts[-1]) + int(parts[-2]) * 60
        if len(parts) == 3:
            seconds += int(parts[0]) * 3600
        return round(seconds * 1000)
    def timestamp(value):
        hours, value = divmod(value, 3_600_000)
        minutes, value = divmod(value, 60_000)
        seconds, ms = divmod(value, 1000)
        separator = ',' if output_format == 'srt' else '.'
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{ms:03d}'
    start_ms = max(0, int(start_ticks) // 10_000)
    cues = []
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        if not lines or lines[0].startswith(('NOTE', 'STYLE', 'REGION')):
            continue
        for n, line in enumerate(lines):
            match = timing.fullmatch(line.strip())
            if not match:
                continue
            begin, end = map(milliseconds, match.groups())
            if end <= start_ms or end <= begin:
                break
            if not copy_timestamps:
                begin, end = max(0, begin - start_ms), end - start_ms
            cues.append((begin, end, '\n'.join(lines[n + 1:])))
            break
    output = ['WEBVTT\n'] if output_format == 'vtt' else []
    for n, (begin, end, body) in enumerate(cues, 1):
        output.append(f'{n}\n{timestamp(begin)} --> {timestamp(end)}\n{body}\n')
    return ('\n'.join(output) + '\n').encode('utf-8')
