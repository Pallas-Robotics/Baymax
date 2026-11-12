"""Build a private Orange Pi 4 Pro image on Linux; never write a physical disk."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from image_settings import TARGET_BOARD, load, source

HERE = Path(__file__).resolve().parent


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def asset(name, spec, cache):
    if Path(name).name != name or not spec['url'].startswith('https://'):
        raise ValueError('Invalid asset path or URL')
    target = cache / name
    if target.exists() and digest(target) == spec['sha256']:
        return target
    temporary = cache / (name + '.download')
    try:
        # curl, as the original asset downloads used; DietPi and AnyDesk return 403 to urllib.
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error', '--retry', '3',
                        '--connect-timeout', '60', '--output', str(temporary), spec['url']], check=True)
        if digest(temporary) != spec['sha256']:
            raise ValueError(f'{name}: checksum mismatch; review upstream before changing assets.json')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def generate_credentials(access_password=None):
    # An optional fixed access_password (private settings only) replaces both random passwords.
    return {'hostname': 'ember-' + secrets.token_hex(4), 'username': 'ember',
            'linux_password': access_password or secrets.token_urlsafe(24),
            'anydesk_password': access_password or secrets.token_urlsafe(24)}


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=HERE / 'settings.private.json')
    parser.add_argument('--cache', type=Path, default=HERE / '.cache')
    parser.add_argument('--output', type=Path, default=HERE / 'output')
    args = parser.parse_args()
    settings = load(args.settings)
    if os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        parser.error('Use Linux with sudo; this operation mounts an image file, not a card.')
    for tool in ('curl', 'git', 'xz', 'losetup', 'mount', 'umount', 'mountpoint', 'e2fsck', 'resize2fs', 'openssl', 'bash', 'chroot'):
        if not shutil.which(tool):
            parser.error('Missing prerequisite: ' + tool)
    os.umask(0o077)
    output = args.output.resolve()
    if output.exists():
        parser.error('Output already exists. Choose a new --output directory to preserve previous builds.')
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, mode=0o700)
    work = output / '.build'
    work.mkdir(mode=0o700)
    try:
        payload = work / 'payload'
        shutil.copytree(HERE / 'payload', payload)
        assets = json.loads((HERE / 'assets.json').read_text())
        for name, spec in assets.items():
            print('Checking/download:', name, flush=True)
            downloaded = asset(name, spec, cache)
            shutil.copyfile(downloaded, work / name if name == 'base.img.xz' else payload / name)
        for path in HERE.glob('*.py'):
            shutil.copyfile(path, work / path.name)
        credentials = generate_credentials(settings.get('access_password'))
        private_json(payload / 'credentials.json', credentials)
        private_json(payload / 'image-settings.private.json', settings)
        checkout = work / 'source.git'
        subprocess.run(['git', 'init', '--bare', str(checkout)], check=True, stdout=subprocess.DEVNULL)
        selected = source(settings)
        subprocess.run(['git', '-C', str(checkout), 'fetch', selected['repository'],
                        selected['branch'] + ':refs/heads/image-release'], check=True)
        commit = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'refs/heads/image-release'], text=True).strip()
        subprocess.run(['git', '-C', str(checkout), 'bundle', 'create', str(payload / 'ember.bundle'), 'refs/heads/image-release'], check=True)
        subprocess.run(['git', '-C', str(checkout), 'bundle', 'verify', str(payload / 'ember.bundle')], check=True)
        private_json(payload / 'bundle-source.json', selected | {'commit': commit, 'bundle_branch': 'image-release'})
        # The Wi-Fi setup hotspot (open, only while offline): the selected branch's copy, or
        # this builder's own for a branch from before it existed.
        for name in ('ember_wifi_setup.py', 'ember-wifi-setup.service'):
            shown = subprocess.run(['git', '-C', str(checkout), 'show', 'refs/heads/image-release:Baymax-main/deploy/host/' + name],
                                   capture_output=True)
            if shown.returncode == 0:
                (payload / name).write_bytes(shown.stdout)
            else:
                shutil.copyfile(HERE.parents[1] / 'Baymax-main/deploy/host' / name, payload / name)
        print('Building and checking image file...', flush=True)
        subprocess.run(['bash', str(HERE / 'build_linux.sh'), str(work), str(cache)], check=True)
        image = output / 'ember-opi4.img'
        (work / 'prepared.img').replace(image)
        image.chmod(0o600)
        private_json(output / 'device-access.private.json', credentials)
        private_json(output / 'ready-image.json', {
            'format': 1, 'board': TARGET_BOARD,
            'image': image.name, 'length': image.stat().st_size, 'sha256': digest(image),
            'repository': selected['repository'], 'ref': selected['branch'], 'commit': commit,
            'hostname': credentials['hostname'], 'wifi_ssid': settings['wifi']['ssid'],
            'gemini_key_configured': bool(settings['gemini_api_key'])})
        print('Ready:', image, flush=True)
        print('Private access details:', output / 'device-access.private.json', flush=True)
        print('No physical disk was written. Flash the image using your preferred image writer.', flush=True)
    finally:
        # Loop devices/mounts are released by build_linux.sh before this returns.
        # Keep a failed workspace for inspection rather than delete a mounted tree.
        if not (work / 'mount').is_mount() and (output / 'ready-image.json').is_file():
            shutil.rmtree(work)


if __name__ == '__main__':
    main()
