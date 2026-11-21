"""Adapt daemon-specific image IDs after verifying portable image content.

The downloaded management bundle remains immutable. A hard-linked runtime view
holds local image IDs and its own checksum manifest; no images or binaries build.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


def config(value):
    # Docker versions omit default/empty fields differently. Labels and Env values
    # remain exact; only absent/default top-level fields and health defaults agree.
    result = {k:v for k,v in value.items() if v not in (None, '', False, 0, [], {})}
    if 'Healthcheck' in result:
        result['Healthcheck'] = {k:v for k,v in result['Healthcheck'].items() if v not in (None, '', False, 0, [], {})}
        if not result['Healthcheck']:
            result.pop('Healthcheck')
    return result


def fingerprint(value, archived=False):
    return {'os':value['os'] if archived else value['Os'],
            'arch':value['architecture'] if archived else value['Architecture'],
            'layers':value['rootfs']['diff_ids'] if archived else value['RootFS']['Layers'],
            'config':config(value.get('config', {}) if archived else value.get('Config', {}))}


def archive_images(bundle):
    names = (bundle / 'images').read_text().splitlines()
    if not 3 <= len(names) <= 4 or len(set(names)) != len(names) or any(
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./_-]*:[A-Za-z0-9_][A-Za-z0-9._-]*', name) for name in names):
        raise ValueError('Unexpected management image list')
    expected = {}
    with tarfile.open(bundle / 'images.tar', 'r:') as archive:
        def document(name):
            member = archive.getmember(name)
            if not member.isfile() or member.size > 2**21:
                raise ValueError('Invalid image config member')
            with archive.extractfile(member) as stream:
                return json.load(stream)
        manifests = document('manifest.json')
        for name in names:
            matches = [m for m in manifests if name in (m.get('RepoTags') or [])]
            if len(matches) != 1:
                raise ValueError('Management image missing or ambiguous in archive: ' + name)
            expected[name] = fingerprint(document(matches[0]['Config']), archived=True)
    return expected


def inspect_image(name):
    return json.loads(subprocess.check_output(['docker', 'image', 'inspect', name], text=True))[0]


def prepare(source, destination, arch, load=None, inspect=None):
    subprocess.run(['sha256sum', '--quiet', '-c', 'SHA256SUMS'], cwd=source, check=True)
    expected = archive_images(source)
    if destination.exists():
        raise RuntimeError('Derived management runtime already exists; inspect phase receipt, do not replay')
    if load is None:
        load = lambda:subprocess.run(['docker', 'load', '-i', str(source / 'images.tar')], check=True)
    inspect = inspect or inspect_image
    load()
    ids = []
    for name, identity in expected.items():
        actual = inspect(name)
        if identity['arch'] != arch or fingerprint(actual) != identity:
            raise ValueError('Loaded image content/config differs from verified archive: ' + name)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', actual['Id']):
            raise ValueError('Invalid local Docker image ID')
        ids.append(actual['Id'])
    with tempfile.TemporaryDirectory(prefix='.manager-runtime-', dir=source.parent) as temporary:
        staging = Path(temporary) / 'runtime'
        shutil.copytree(source, staging, copy_function=os.link)
        # Never write through hard links to the immutable downloaded source.
        for name in ('image-ids', 'SHA256SUMS'):
            (staging / name).unlink()
        (staging / 'image-ids').write_text('\n'.join(ids)+'\n')
        digest = hashlib.sha256((staging / 'image-ids').read_bytes()).hexdigest()
        lines = (source / 'SHA256SUMS').read_text().splitlines()
        matches = [line for line in lines if line.split(maxsplit=1)[1].lstrip('*') == 'image-ids']
        if len(matches) != 1:
            raise ValueError('Original image ID checksum is missing or ambiguous')
        (staging / 'SHA256SUMS').write_text('\n'.join(digest+'  image-ids' if line == matches[0] else line for line in lines)+'\n')
        subprocess.run(['sha256sum', '--quiet', '-c', 'SHA256SUMS'], cwd=staging, check=True)
        staging.rename(destination)
    print('Loaded images match verified archive layers/config; recorded local Docker IDs without building.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('settings')
    args = parser.parse_args()
    settings = json.loads(Path(args.settings).read_text())
    work = Path(settings['root']) / 'work'
    prepare(work / 'management-bundle', work / 'management-runtime', settings['node']['facts']['arch'])
