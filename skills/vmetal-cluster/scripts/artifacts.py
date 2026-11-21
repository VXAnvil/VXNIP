"""Pinned public runtime assets. No source builds or model bundles."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = 'https://github.com/vmetal-lab/cluster-api-provider-vmetal/releases/download/'


class DownloadBlocked(RuntimeError):
    """Only private staging files were written; installation has not started."""


def validate_asset(asset, role, arch):
    if role not in ('manager', 'node') or arch not in ('amd64', 'arm64'):
        raise ValueError('Unsupported runtime role/architecture')
    if asset.get('role') != role or asset.get('arch') != arch:
        raise ValueError('Runtime asset role/architecture differs')
    if not re.fullmatch(r'[0-9a-f]{64}', asset.get('sha256', '')):
        raise ValueError('Runtime asset requires a SHA-256 digest')
    if not isinstance(asset.get('size'), int) or not 0 < asset['size'] < 2**31:
        raise ValueError('Invalid public runtime asset size')
    if not isinstance(asset.get('unpacked_size'), int) or not 0 < asset['unpacked_size'] <= 16 * 2**30:
        raise ValueError('Invalid unpacked runtime size')
    url = asset.get('url', '')
    if not url.startswith(ORIGIN) or urllib.parse.urlsplit(url).query or urllib.parse.urlsplit(url).fragment:
        raise ValueError('Runtime assets must use the official public GitHub release')
    suffix = url.removeprefix(ORIGIN)
    if not re.fullmatch(r'runtime-v[0-9A-Za-z.-]+/vmetal-(manager|node)-(amd64|arm64)\.tar\.gz', suffix):
        raise ValueError('Invalid versioned runtime asset URL')
    if suffix.split('/')[-1] != f'vmetal-{role}-{arch}.tar.gz':
        raise ValueError('Runtime URL role/architecture differs')
    return asset


def catalog(path, release):
    if not path.is_file():
        raise ValueError('Prebuilt runtime manifest is missing. Install a published skill release; source builds require artifact_mode=source explicitly.')
    data = json.loads(path.read_text())
    if data.get('schema') != 1 or data.get('sources') != release:
        raise ValueError('Runtime manifest source pins differ from this skill')
    for role in ('manager', 'node'):
        for arch in ('amd64', 'arm64'):
            validate_asset(data['assets'][role + '-' + arch], role, arch)
    return data


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise DownloadBlocked('Runtime download redirected away from HTTPS')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(asset, output):
    digest = hashlib.sha256()
    size = 0
    reported = time.monotonic()
    opener = urllib.request.build_opener(HTTPSRedirect())
    try:
        request = urllib.request.Request(asset['url'], headers={'User-Agent': 'vmetal-cluster-skill'})
        with opener.open(request, timeout=60) as response, output.open('xb') as target:
            if urllib.parse.urlsplit(response.url).scheme != 'https':
                raise DownloadBlocked('Runtime download requires HTTPS')
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > asset['size']:
                    raise DownloadBlocked('Runtime asset exceeds its pinned size')
                digest.update(block)
                target.write(block)
                if time.monotonic() - reported >= 15:
                    print(f"Downloaded {size // 2**20}/{asset['size'] // 2**20} MiB", flush=True)
                    reported = time.monotonic()
    except (OSError, urllib.error.URLError) as error:
        raise DownloadBlocked('Runtime download incomplete; retry the same plan after connectivity recovers') from error
    if size != asset['size'] or digest.hexdigest() != asset['sha256']:
        raise DownloadBlocked('Runtime asset size/SHA-256 verification failed; nothing was installed')


def unpack(archive, staging, asset, release):
    """Only regular files/directories, no traversal, links, devices or duplicates."""
    seen = set()
    total = 0
    root_name = 'management-bundle' if asset['role'] == 'manager' else 'artifacts'
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.as_posix() in seen or not (member.isfile() or member.isdir())):
                raise DownloadBlocked('Unsafe runtime archive member')
            if path.parts[0] != root_name and not (path.as_posix() == 'asset.json' and member.isfile()):
                raise DownloadBlocked('Unexpected runtime archive path')
            seen.add(path.as_posix())
            total += member.size
            if total > asset['unpacked_size']:
                raise DownloadBlocked('Runtime archive exceeds its pinned unpacked size')
            target = staging.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with source.extractfile(member) as src, target.open('xb') as dest:
                    shutil.copyfileobj(src, dest)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    metadata = json.loads((staging / 'asset.json').read_text())
    if metadata != {'schema': 1, 'role': asset['role'], 'arch': asset['arch'], 'sources': release}:
        raise DownloadBlocked('Runtime archive identity/source pins differ')
    if total != asset['unpacked_size'] or not (staging / root_name / 'SHA256SUMS').is_file():
        raise DownloadBlocked('Incomplete runtime archive')
    return staging / root_name


def install(settings, role):
    plan, root = settings['plan'], Path(settings['root'])
    arch = settings['node']['facts']['arch']
    asset = (plan['manager_asset'] if role == 'manager' else plan['artifacts'][arch]['asset'])
    validate_asset(asset, role, arch)
    work = root / 'work'
    work.mkdir(mode=0o700, exist_ok=True)
    destination = work / ('management-bundle' if role == 'manager' else 'artifacts')
    marker = work / ('.installed-' + role + '.json')
    if destination.exists():
        if marker.is_file() and json.loads(marker.read_text()) == asset:
            return
        raise RuntimeError('Runtime destination already exists without a matching receipt; inspect private state')
    if shutil.disk_usage(work).free < asset['size'] + asset['unpacked_size'] + 2**30:
        raise DownloadBlocked('Insufficient free disk for verified runtime staging')
    with tempfile.TemporaryDirectory(prefix='.runtime-', dir=work) as name:
        temporary = Path(name)
        archive = temporary / 'runtime.tar.gz'
        print('Downloading verified ' + role + ' runtime for ' + arch, flush=True)
        download(asset, archive)
        staging = temporary / 'unpack'
        staging.mkdir(mode=0o700)
        content = unpack(archive, staging, asset, plan['release'])
        os.rename(content, destination)
        marker.write_text(json.dumps(asset, sort_keys=True))
        marker.chmod(0o600)
    print('Verified runtime ready; no compiler or image build was invoked', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('settings')
    parser.add_argument('role', choices=('manager', 'node'))
    args = parser.parse_args()
    try:
        install(json.loads(Path(args.settings).read_text()), args.role)
    except DownloadBlocked as error:
        print(str(error), flush=True)
        raise SystemExit(75) from error
