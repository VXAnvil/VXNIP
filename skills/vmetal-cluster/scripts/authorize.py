"""Append one restricted public Provider key; preserve existing SSH authorization."""
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


def authorize(user, key_path):
    account = pwd.getpwnam(user)
    home = Path(account.pw_dir)
    if not str(home).startswith('/home/') or home.is_symlink():
        raise ValueError('Worker account must have a real /home directory')
    key = Path(key_path).read_text().strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\n]*)?', key):
        raise ValueError('Expected one Ed25519 public Provider key')
    subprocess.run(['ssh-keygen', '-l', '-f', str(key_path)], check=True, capture_output=True)
    directory = home / '.ssh'
    if directory.is_symlink():
        raise ValueError('Refusing a symlink SSH directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != account.pw_uid and directory.stat().st_uid != 0:
        raise ValueError('SSH directory has another owner')
    file = directory / 'authorized_keys'
    if file.is_symlink():
        raise ValueError('Refusing a symlink authorized_keys')
    before = file.read_bytes() if file.exists() else b''
    line = ('restrict ' + ' '.join(key.split()[:2])).encode()
    if line not in before.splitlines():
        with file.open('ab') as stream:
            if before and not before.endswith(b'\n'):
                stream.write(b'\n')
            stream.write(line + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o700)
    os.chown(file, account.pw_uid, account.pw_gid)
    os.chmod(file, 0o600)


if __name__ == '__main__':
    authorize(sys.argv[1], sys.argv[2])
