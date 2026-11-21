#!/usr/bin/env python3
"""Package only the portable VMetal skill runtime; never include site state."""
import argparse
import hashlib
from pathlib import Path
import zipfile


def package(output):
    source = Path(__file__).resolve().parents[1] / 'skills/vmetal-cluster'
    files = [source / 'SKILL.md', source / 'references/executor.md']
    files.extend(sorted(p for p in (source / 'scripts').iterdir() if p.suffix in ('.py', '.sh')))
    output = Path(output)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Invalid runtime file: ' + str(path))
            archive.write(path, str(Path('vmetal-cluster') / path.relative_to(source)))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(checksum + '  ' + output.name + '\n')
    return checksum


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    print(package(parser.parse_args().output))
