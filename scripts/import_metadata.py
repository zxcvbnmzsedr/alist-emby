#!/usr/bin/env python3
"""Compatibility entrypoint for cinema/scripts/import_metadata.py."""
import runpy
import sys
from pathlib import Path

folder = Path(__file__).resolve().parents[1] / 'cinema/scripts'
sys.path.insert(0, str(folder))
if __name__ == '__main__':
    runpy.run_path(str(folder / 'import_metadata.py'), run_name='__main__')
else:
    globals().update(runpy.run_path(str(folder / 'import_metadata.py')))
