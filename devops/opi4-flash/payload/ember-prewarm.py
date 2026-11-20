"""Read the files Ember loaded on its last run into memory, early in boot.

Ember writes them (pc_hardware.note_startup_files) to
~/ember-deployment/data/startup-files/<process>.txt once it is connected. On the
Orange Pi 4 Pro, Ember took 24 s from start to first words with a cold disk cache
and 13.6 s with a warm one: ~390 MB of libraries and ~290 MB of Python files come
off the SD card. Read here while the desktop is still starting, they are already
in the page cache when Ember imports them. Runs as ember; only reads.
"""
import os
from pathlib import Path
import sys
import time

LISTS = Path('/home/ember/ember-deployment/data/startup-files')
ALLOWED = ('/home/ember/', '/opt/ember-python/')
CHUNK = 1 << 20


def startup_files(lists=LISTS, allowed=ALLOWED):
    """Each listed path once, in list order, if it is a regular file under `allowed`."""
    seen, paths = set(), []
    try:
        names = sorted(p for p in lists.iterdir() if p.suffix == '.txt')
    except OSError:
        return []
    for name in names:
        try:
            lines = name.read_text(errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            path = os.path.realpath(line.strip())
            if path in seen or not path.startswith(allowed) or not os.path.isfile(path):
                continue
            seen.add(path)
            paths.append(path)
    return paths


def warm(path):
    """Read the whole file (into the page cache); returns bytes read."""
    total = 0
    try:
        with open(path, 'rb', buffering=0) as handle:
            while chunk := handle.read(CHUNK):
                total += len(chunk)
    except OSError:
        pass
    return total


def main():
    started = time.monotonic()
    paths = startup_files()
    total = sum(warm(path) for path in paths)
    print(f'Prewarmed {len(paths)} files, {total / 1e6:.0f} MB in {time.monotonic() - started:.1f} s',
          flush=True)


if __name__ == '__main__':
    sys.exit(main())
