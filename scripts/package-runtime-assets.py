#!/usr/bin/env python3
"""Publisher-only packaging of clean, verified NVKE runtime outputs."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import os
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/vxnip-cluster/scripts'))
from cluster import RELEASE  # noqa: E402


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def pack(directory, role, arch, output, tag, sources=None):
    sources = RELEASE if sources is None else sources
    root_name = 'management-bundle' if role == 'manager' else 'artifacts'
    selected = []
    if role == 'manager':
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('Symlinks are not shipped in runtime assets')
            if path.is_file():
                selected.append((path, path.relative_to(directory)))
    else:
        selected.append((directory / 'SHA256SUMS', Path('SHA256SUMS')))
        for line in (directory / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split(maxsplit=1)
            name = name.removeprefix('*')
            if not re.fullmatch(r'[0-9a-f]{64}', digest) or '/' in name or name in ('.', '..'):
                raise ValueError('Invalid node runtime checksum entry')
            path = directory / name
            if path.is_symlink() or not path.is_file() or sha256(path) != digest:
                raise ValueError('Unverified node artifact: ' + name)
            selected.append((path, Path(name)))
    metadata = json.dumps({'schema': 1, 'role': role, 'arch': arch, 'sources': sources}, sort_keys=True).encode()
    total = len(metadata)
    archive_path = output / f'vxnip-{role}-{arch}.tar.gz'
    with tarfile.open(archive_path, 'x:gz', compresslevel=6) as tar:
        info = tarfile.TarInfo('asset.json')
        info.size, info.mode = len(metadata), 0o644
        tar.addfile(info, io.BytesIO(metadata))
        for path, relative in selected:
            if any(part in ('site', 'tenant', '.git', '__pycache__') for part in relative.parts) or path.name.endswith(('.kubeconfig', '.key')):
                raise ValueError('Private state is not a runtime asset: ' + str(relative))
            info = tar.gettarinfo(str(path), str(Path(root_name) / relative))
            if not info.isfile():
                raise ValueError('Only regular runtime files are published')
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            info.mode = 0o755 if info.mode & 0o111 else 0o644
            total += info.size
            with path.open('rb') as stream:
                tar.addfile(info, stream)
    if archive_path.stat().st_size >= 2**31:
        raise ValueError('Runtime asset exceeds the GitHub release per-file limit')
    digest = sha256(archive_path)
    archive_path.with_suffix('.gz.sha256').write_text(digest + '  ' + archive_path.name + '\n')
    return {'role': role, 'arch': arch, 'size': archive_path.stat().st_size,
            'unpacked_size': total, 'sha256': digest,
            'url': 'https://github.com/VXAnvil/cluster-api-provider-vxnip/releases/download/' + tag + '/' + archive_path.name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--arch', required=True, choices=('amd64', 'arm64'))
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    native = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if native != args.arch or not re.fullmatch(r'runtime-v[0-9A-Za-z.-]+', args.tag):
        raise ValueError('Publish on the native architecture with a versioned runtime tag')
    provider_revision = os.environ.get('VXNIP_PROVIDER_REVISION', RELEASE['provider'])
    if not re.fullmatch(r'[a-f0-9]{40}', provider_revision):
        raise ValueError('Publisher requires an exact Provider source commit')
    sources = dict(RELEASE, provider=provider_revision)
    for checkout, revision in (('provider', sources['provider']), ('nvke', sources['nvke'])):
        actual = subprocess.check_output(['git', '-C', str(args.workspace / checkout), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != revision:
            raise ValueError('Workspace source revision differs: ' + checkout)
    manager = args.workspace / 'management-bundle'
    subprocess.run(['sha256sum', '--quiet', '-c', 'SHA256SUMS'], cwd=manager, check=True)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets = {}
    for role, directory in (('manager', manager), ('node', args.workspace / 'artifacts')):
        assets[role + '-' + args.arch] = pack(directory, role, args.arch, args.output, args.tag, sources=sources)
    (args.output / ('manifest-' + args.arch + '.json')).write_text(json.dumps({'schema': 1, 'sources': sources, 'assets': assets}, indent=2))


if __name__ == '__main__':
    main()
