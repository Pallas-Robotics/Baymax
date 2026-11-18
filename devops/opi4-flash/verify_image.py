"""Check a built OPI4 Pro image before flashing. Never writes a disk or image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from image_settings import REPOSITORY, TARGET_BOARD


def sha256(path):
    """1 MiB at a time: hashlib.file_digest needs Python 3.11, and the python3 that
    comes with macOS is 3.9."""
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def verify(manifest):
    manifest = Path(manifest)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or type(data.get('format')) is not int
            or data['format'] != 1 or data.get('board') != TARGET_BOARD):
        raise ValueError('Expected an Orange Pi 4 Pro manifest from this builder; rebuild older images to verify them here.')
    if data.get('repository') != REPOSITORY:
        raise ValueError('Manifest repository does not match the configured Ember repository.')
    name = data.get('image')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.img', name):
        raise ValueError('Image must be an .img filename beside the manifest, not a path.')
    image = (manifest.parent / name).resolve()
    if image.parent != manifest.parent.resolve() or not image.is_file():
        raise ValueError('Image is missing or resolves outside the manifest directory.')
    size = data.get('length')
    if type(size) is not int or size <= 0 or image.stat().st_size != size:
        raise ValueError('Image length does not match its manifest.')
    expected = data.get('sha256')
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Manifest SHA256 must be 64 lowercase hexadecimal characters.')
    actual = sha256(image)
    if actual != expected:
        raise ValueError('Image SHA256 does not match; do not flash this file.')
    return {'board': TARGET_BOARD, 'image': name, 'length': size, 'sha256': actual}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).parent / 'output/ready-image.json')
    args = parser.parse_args(argv)
    try:
        result = verify(args.manifest)
    except (OSError, ValueError, TypeError) as error:
        print('Verification failed: ' + str(error), file=sys.stderr)
        return 2
    print(f"Verified {result['image']}: {result['length']} bytes, board {result['board']}")
    print('SHA256: ' + result['sha256'])
    print('No disk was written. Flash this file with your image writer and enable its verification step.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
