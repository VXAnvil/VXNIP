"""Export a private client whose commands bind their own site configuration."""
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys


def wrapper(binary, config, cache, site):
    values = {'XDG_CONFIG_HOME': str(config), 'XDG_CACHE_HOME': str(cache)}
    lines = ['#!/bin/sh', 'set -eu']
    lines += ['export ' + key + '=' + shlex.quote(value) for key, value in values.items()]
    lines.append('exec ' + shlex.quote(str(binary)) + ' --site ' + shlex.quote(site) + ' "$@"')
    return '\n'.join(lines) + '\n'


def main(settings):
    root, plan = Path(settings['root']), settings['plan']
    account = pwd.getpwnam(plan['manager']['user'])
    output = Path(account.pw_dir) / '.config/vxnip/sites' / plan['site']
    bundle = root / 'work' / ('management-runtime' if plan['artifact_mode'] == 'release' else 'management-bundle')
    subprocess.run(['bash', str(bundle / 'provider/scripts/configure-vxnip-client.sh'), str(root / 'site'),
                    str(output), str(root / 'tenant/workload.kubeconfig')], check=True)
    (output / 'libexec').mkdir(mode=0o700, exist_ok=True)
    binary = output / 'libexec/nvkectl'
    if binary.exists():
        raise ValueError('Client binary staging already exists; inspect before repeating')
    (output / 'bin/nvkectl').rename(binary)
    entry = output / 'bin/nvkectl'
    entry.write_text(wrapper(binary, output / 'config', output / 'cache', plan['site']))
    entry.chmod(0o700)
    workload = output / 'bin/workload-kubectl'
    workload.write_text('#!/bin/sh\nset -eu\nexec ' + shlex.quote(str(output / 'bin/kubectl')) + ' --kubeconfig '
                        + shlex.quote(str(output / 'workload.kubeconfig')) + ' "$@"\n')
    workload.chmod(0o700)
    for directory, dirs, files in os.walk(output):
        os.chown(directory, account.pw_uid, account.pw_gid)
        for name in files:
            os.chown(Path(directory) / name, account.pw_uid, account.pw_gid)
    report = {'directory': str(output), 'nvkectl': str(entry), 'workload_kubectl': str(workload),
              'workload_kubeconfig': str(output / 'workload.kubeconfig')}
    (root / 'client.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main(json.loads(Path(sys.argv[1]).read_text()))
