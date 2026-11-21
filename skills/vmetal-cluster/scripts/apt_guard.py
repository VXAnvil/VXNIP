"""Reject dependency transactions that could replace/reconfigure installed software."""
from pathlib import Path
import re
import sys


def check(text):
    new = set()
    configure = set()
    for line in text.splitlines():
        if re.match(r'^Remv\s', line) or re.match(r'^Inst\s+\S+\s+\[', line):
            raise ValueError('Native build would remove or upgrade an installed package')
        install = re.match(r'^Inst\s+(\S+)\s+\(', line)
        if install:
            new.add(install[1])
        conf = re.match(r'^Conf\s+(\S+)\s', line)
        if conf:
            configure.add(conf[1])
    if configure - new:
        raise ValueError('Native build would configure an existing pending package')
    return sorted(new)


if __name__ == '__main__':
    check(Path(sys.argv[1]).read_text())
