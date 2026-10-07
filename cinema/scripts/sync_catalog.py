#!/usr/bin/env python3
"""Refresh the local static catalogue over SSH without transferring keys."""
import argparse
import os
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path
from build_catalog import atomic_write

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', default=os.environ.get('ALIST_EMBY_SSH_HOST'), required=not bool(os.environ.get('ALIST_EMBY_SSH_HOST')))
parser.add_argument('--root', default='/m3u8')
parser.add_argument('--output', default=str(Path(__file__).parents[1] / 'public'))
args = parser.parse_args()
source = Path(__file__).with_name('build_catalog.py').read_text()
remote = "__name__ = 'cinema_catalog_remote'\n" + source + '\n' + '''
import tarfile
with tempfile.TemporaryDirectory(prefix='cinema-catalog-') as temporary:
    out = Path(temporary)
    build_catalog(SOURCE_ROOT, out)
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for file in sorted(out.rglob('*')):
            if file.is_file():
                archive.add(file, arcname=str(file.relative_to(out)))
'''.replace('SOURCE_ROOT', repr(args.root))
result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', args.host, 'python3', '-'], input=remote.encode(), stdout=subprocess.PIPE, check=True)
output = Path(args.output)
pending = []
with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:') as archive:
    for member in archive:
        if not member.isfile() or not (member.name == 'catalog.json' or re.fullmatch(r'covers/[a-f0-9]{20}\.(jpg|jpeg|png|webp|avif)', member.name)):
            raise ValueError('Unexpected file in catalogue export')
        if member.size > 20_000_000:
            raise ValueError('Catalogue export file too large')
        pending.append((member.name, archive.extractfile(member).read()))
catalog = next(data for name, data in pending if name == 'catalog.json')
count = len(json.loads(catalog)['videos'])
for name, data in pending:
    if name != 'catalog.json':
        atomic_write(output / name, data)
atomic_write(output / 'catalog.json', catalog)
print(f'Updated {count} videos. No playlists, keys or tokens transferred.')
