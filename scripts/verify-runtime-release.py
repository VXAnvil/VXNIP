#!/usr/bin/env python3
"""Verify all native publisher outputs before creating a public release."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/vxanvil-cluster/scripts'))
import artifacts  # noqa: E402
from cluster import RELEASE  # noqa: E402
spec = importlib.util.spec_from_file_location('publisher', ROOT / 'scripts/package-runtime-assets.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def verify(directory, tag, expected_sources=None):
    expected_sources = RELEASE if expected_sources is None else expected_sources
    result = {'schema': 1, 'sources': expected_sources, 'assets': {}}
    for arch in ('amd64', 'arm64'):
        manifest = json.loads((directory / ('manifest-' + arch + '.json')).read_text())
        if manifest.get('schema') != 1 or manifest.get('sources') != expected_sources:
            raise ValueError('Native publisher source identity differs')
        for role in ('manager', 'node'):
            key = role + '-' + arch
            asset = artifacts.validate_asset(manifest['assets'][key], role, arch)
            name = 'vxanvil-' + key + '.tar.gz'
            path = directory / name
            if asset['url'] != artifacts.ORIGIN + tag + '/' + name:
                raise ValueError('Runtime release tag differs')
            if path.is_symlink() or path.stat().st_size != asset['size'] or publisher.sha256(path) != asset['sha256']:
                raise ValueError('Runtime archive bytes differ')
            if (directory / (name + '.sha256')).read_text() != asset['sha256'] + '  ' + name + '\n':
                raise ValueError('Runtime checksum sidecar differs')
            result['assets'][key] = asset
    output = directory / 'runtime-artifacts.json'
    output.write_text(json.dumps(result, indent=2) + '\n')
    artifacts.catalog(output, expected_sources)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    verify(args.directory, args.tag, dict(RELEASE, provider=revision))
