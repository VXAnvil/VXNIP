#!/usr/bin/env python3
"""Package only the portable VXAnvil skill runtime; never include site state."""
import argparse
import hashlib
from pathlib import Path
import zipfile
import sys


def package(output, development=False):
    source = Path(__file__).resolve().parents[1] / 'skills/vxanvil-cluster'
    files = [source / 'SKILL.md', source / 'references/executor.md']
    manifest = source / 'references/runtime-artifacts.json'
    if manifest.is_file():
        sys.path.insert(0, str(source / 'scripts'))
        from cluster import RELEASE
        from artifacts import catalog
        catalog(manifest, RELEASE)
        files.append(manifest)
    elif not development:
        raise ValueError('Publish verified native runtime assets and include their manifest before releasing the skill')
    files.extend(sorted(p for p in (source / 'scripts').iterdir() if p.suffix in ('.py', '.sh')))
    output = Path(output)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Invalid runtime file: ' + str(path))
            archive.write(path, str(Path('vxanvil-cluster') / path.relative_to(source)))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(checksum + '  ' + output.name + '\n')
    return checksum


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--development', action='store_true', help='Package an explicitly source-only development skill')
    args = parser.parse_args()
    print(package(args.output, development=args.development))
