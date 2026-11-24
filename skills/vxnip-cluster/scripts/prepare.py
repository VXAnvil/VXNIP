"""Explicit pre-allocation swap/NoCloud preparation; never reset an allocation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def actions(config, facts, role, kind):
    requested = config.get('prepare', {})
    if not isinstance(requested, dict) or set(requested) - {'disable_swap', 'cloud_init'} or any(type(v) is not bool for v in requested.values()):
        raise ValueError('prepare accepts boolean disable_swap and cloud_init only')
    if requested and (role != 'worker' or kind != 'baremetal'):
        raise ValueError('Host preparation is supported only for bare-metal workers')
    result = []
    if facts.get('swap') and requested.get('disable_swap'):
        devices = facts.get('swap_devices', [])
        if not devices or any(d['type'] != 'file' or d['used_kib'] != 0 for d in devices):
            raise ValueError('Swap preparation requires known, unused swap files; inspect active/in-use swap')
        result.append('disable_swap')
    if facts.get('cloud_init', {}).get('status') != 'done' and requested.get('cloud_init'):
        if facts.get('cloud_init_preparable') is not True:
            raise ValueError('NoCloud preparation would replace unknown seed/configuration; inspect it first')
        result.append('cloud_init')
    return result


def swap_fstab(text, devices):
    expected = {d['path'] for d in devices}
    if (not expected or any(d['type'] != 'file' or d['used_kib'] for d in devices)
            or any(not p.startswith('/') or p.startswith('/dev/') or any(c.isspace() for c in p) for p in expected)):
        raise ValueError('Only known, unused absolute swap files may be disabled')
    found, output = set(), []
    for line in text.splitlines(keepends=True):
        fields = line.split()
        if fields and not line.lstrip().startswith('#') and len(fields) > 2 and fields[2] == 'swap':
            if fields[0] not in expected or fields[0] in found:
                raise ValueError('Unexpected/duplicate persistent swap entry')
            found.add(fields[0])
            output.append('# VXNIP: swap disabled; original file retained\n# ' + line)
        else:
            output.append(line)
    if found != expected:
        raise ValueError('Swap files and fstab do not match')
    return ''.join(output)


def main(settings):
    import probe
    import preserve
    node, root = settings['node'], Path(settings['root'])
    current = probe.facts()
    if current['machine_id'] != node['facts']['machine_id'] or current['allocated'] is not False:
        raise ValueError('Host identity/allocation changed before preparation')
    planned = node.get('preparation', [])
    if not planned:
        return
    before = preserve.snapshot()
    state = root / 'host-preparation'
    state.mkdir(mode=0o700)
    if 'disable_swap' in planned:
        devices = current['swap_devices']
        fstab = Path('/etc/fstab')
        if fstab.is_symlink() or any(Path(d['path']).is_symlink() or not Path(d['path']).is_file() for d in devices):
            raise ValueError('Refusing symlink/non-file swap configuration')
        updated = swap_fstab(fstab.read_text(), devices)
        shutil.copy2(fstab, state / 'fstab.before')
        (state / 'fstab.before').chmod(0o600)
        for device in devices:
            subprocess.run(['swapoff', device['path']], check=True, timeout=60)
        temporary = fstab.with_name('.fstab.vxnip-next')
        with temporary.open('x') as stream:
            stream.write(updated); stream.flush(); os.fsync(stream.fileno())
        shutil.copymode(fstab, temporary)
        os.replace(temporary, fstab)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
    if 'cloud_init' in planned:
        if not current['cloud_init_preparable']:
            raise ValueError('NoCloud prerequisites changed')
        subprocess.run(['bash', str(root / 'input/provider/scripts/prepare-nvke-baremetal-cloud-init.sh')], check=True, timeout=120)
    after = probe.facts()
    if (after['swap'] or after['cloud_init'].get('status') != 'done' or after['cloud_init'].get('errors')
            or any(k != 'DEPRECATED' for k in after['cloud_init'].get('recoverable_errors', {}))
            or not preserve.preserved(before, preserve.snapshot())):
        raise ValueError('Host preparation failed readiness or changed protected shared resources')
    (state / 'completed.json').write_text(json.dumps({'actions': planned, 'machine_id': after['machine_id']}))


if __name__ == '__main__':
    main(json.loads(Path(sys.argv[1]).read_text()))
