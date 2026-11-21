"""Read-only acknowledgement of completed remote phases after lost SSH replies."""
import argparse
import fcntl
import json
from pathlib import Path
import shlex

import cluster


def reconcile(plan, state, step):
    if json.loads((state / 'binding.json').read_text()) != {'plan': cluster.digest(plan)}:
        raise ValueError('State is not bound to this plan')
    journal = json.loads((state / 'journal.json').read_text())
    if journal.get(step, {}).get('state') not in ('unknown', 'running'):
        raise ValueError('Only an uncertain step can be reconciled')
    prefixes = {'prepare-worker-': 'worker', 'protect-worker-': 'protect', 'tailscale-worker-': 'tailscale-worker'}
    prefix = next((prefix for prefix in prefixes if step.startswith(prefix)), None)
    if prefix:
        index = int(step.removeprefix(prefix))
        node, phase = plan['workers'][index], prefixes[prefix]
    elif step.startswith('build-arch-'):
        arch = step.removeprefix('build-arch-')
        builder = plan['artifacts'][arch]['builder']
        if builder == 'manager':
            raise ValueError('Manager build uses the build receipt')
        node, phase = plan['workers'][int(builder.split('-')[1])], 'node-assets'
    elif step in ('build', 'management', 'control-plane', 'handoff', 'join'):
        node, phase = plan['manager'], step
    else:
        raise ValueError('Staging did not return success; inspect the private state before manual recovery')
    current = cluster.probe(cluster.connection(node))
    if any(current[k] != node['facts'][k] for k in ('machine_id', 'host_key', 'arch', 'kernel')):
        raise ValueError('Machine identity changed')
    root = cluster.root_path(plan)
    # The exclusive, non-blocking lock rejects an action still running remotely.
    script = ('exec 9>' + shlex.quote(root + '/runtime.lock') + '; flock -n 9 || exit 1; '
              'test "$(cat ' + shlex.quote(root + '/plan.digest') + ')" = '
              + shlex.quote(cluster.digest(plan)) + ' && cmp -s '
              + shlex.quote(root + '/plan.digest') + ' ' + shlex.quote(root + '/completed.' + phase))
    cluster.remote(cluster.connection(node), 'sudo -n bash -c ' + shlex.quote(script))
    journal[step].update(state='completed', reconciled=True)
    cluster.save(state / 'journal.json', journal)
    return {'step': step, 'state': 'completed', 'replayed': False,
            'health': 'not_checked; apply runs fresh final verification'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--state-directory', required=True)
    parser.add_argument('--step', required=True)
    args = parser.parse_args()
    plan = cluster.checked_plan(args.plan)
    state = cluster.private_dir(args.state_directory)
    with open(state / 'lock', 'a', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(reconcile(plan, state, args.step)))


if __name__ == '__main__':
    main()
