"""Opt-in NVIDIA workers: isolated CDI runtime, device plugin and fresh CUDA probes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

PLUGIN_IMAGE = 'nvcr.io/nvidia/k8s-device-plugin:v0.20.2'
PROBE_IMAGE = 'docker.io/library/python:3.12-slim-bookworm'
SITE_LABEL = 'vxnip.nvke.dev/gpu-site'
ANNOTATION = 'cdi.k8s.io/vxnip-device-plugin'


def identity(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_host(facts):
    value = facts.get('nvidia') or {}
    devices = value.get('devices', [])
    if not devices or any(not re.fullmatch(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', d.get('uuid', '')) for d in devices):
        raise ValueError('GPU worker: a working NVIDIA driver and GPU UUID discovery are required')
    version = re.search(r'(\d+)\.(\d+)\.(\d+)', value.get('toolkit_version', ''))
    if (not version or tuple(map(int, version.groups())) < (1, 18, 0)
            or not value.get('runtime') or not value.get('cdi_hook') or not value.get('cdi_all')):
        raise ValueError('GPU worker: preinstall NVIDIA Container Toolkit >=1.18, runtime, CDI hook and nvidia.com/gpu=all spec; no driver/global-runtime changes are automatic')
    if len({d['uuid'] for d in devices}) != len(devices):
        raise ValueError('GPU discovery returned duplicate UUIDs')
    return value


def runtime_config(root):
    return f'''version = 3
[plugins.'io.containerd.cri.v1.runtime']
  enable_cdi = true
  cdi_spec_dirs = ['/etc/cdi', '/var/run/cdi', '{root}/cdi']
[plugins.'io.containerd.cri.v1.runtime'.containerd]
  default_runtime_name = 'nvidia'
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia]
  runtime_type = 'io.containerd.runc.v2'
  pod_annotations = ['{ANNOTATION}']
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]
  BinaryName = '/usr/bin/nvidia-container-runtime'
  SystemdCgroup = true
'''


def run(args, data=None, timeout=240):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        raise RuntimeError('GPU command failed: ' + args[0] + '; inspect the private phase log')
    return result.stdout.strip()


def import_runtime(before, filename):
    updated, count = re.subn(r'(?m)^version[ \t]*=[ \t]*3[ \t]*$',
                            'version = 3\nimports = ' + json.dumps([str(filename)]), before, count=1)
    if count != 1:
        raise ValueError('Cannot bind the GPU runtime import')
    return updated


def configure_worker(settings):
    import tomllib
    import preserve
    if os.geteuid() != 0 or not settings['node'].get('gpu') or settings['node']['kind'] != 'baremetal':
        raise ValueError('GPU runtime preparation requires a planned bare-metal GPU worker as root')
    root = Path(settings['root'])
    if (root / 'plan.digest').read_text() != identity(settings['plan']):
        raise ValueError('GPU runtime plan identity differs')
    validate_host(settings['node']['facts'])
    main = Path('/etc/vxnip-nvke/containerd.toml')
    before = main.read_text()
    parsed = tomllib.loads(before)
    if (main.is_symlink() or main.stat().st_uid != 0 or parsed.get('version') != 3 or parsed.get('imports')
            or set(parsed.get('plugins', {})) != {'io.containerd.cri.v1.runtime'}):
        raise ValueError('Unexpected isolated runtime configuration; do not adopt or overwrite it')
    runtime = parsed['plugins']['io.containerd.cri.v1.runtime']['containerd']
    if set(runtime.get('runtimes', {})) != {'runc'}:
        raise ValueError('Unexpected existing runtime handlers')
    directory = root / 'gpu'
    directory.mkdir(mode=0o700)  # Existing staging requires inspection, never blind replay.
    (directory / 'containerd.before.toml').write_text(before)
    (directory / 'containerd.before.toml').chmod(0o600)
    protected = preserve.snapshot()
    (root / 'cdi').mkdir(mode=0o700)
    extra = directory / 'containerd.toml'
    extra.write_text(runtime_config(root))
    extra.chmod(0o600)
    main.write_text(import_runtime(before, extra))
    effective = tomllib.loads(run(['containerd', '--config', str(main), 'config', 'dump']))
    config = effective['plugins']['io.containerd.cri.v1.runtime']
    if not config['enable_cdi'] or config['containerd']['runtimes']['nvidia']['pod_annotations'] != [ANNOTATION]:
        raise ValueError('Effective isolated CDI configuration differs')
    run(['systemctl', 'restart', 'vxnip-nvke-containerd.service'], timeout=90)
    run(['systemctl', 'is-active', '--quiet', 'vxnip-nvke-containerd.service'])
    # Agent/kubelet are started by the Provider only after allocation.
    if not preserve.preserved(protected, preserve.snapshot()):
        raise ValueError('Shared Docker/containerd, NVIDIA configuration or default route changed')
    (directory / 'runtime-ready.json').write_text(json.dumps({'plan_digest': identity(settings['plan']), 'cdi_directory': str(root / 'cdi')}))


def metadata(plan, name):
    return {'name': name, 'labels': {'vxnip.nvke.dev/bootstrap': plan['site']},
            'annotations': {'vxnip.nvke.dev/plan': identity(plan)}}


def plugin_manifest(plan, root):
    labels = {'app.kubernetes.io/name': 'nvidia-device-plugin'}
    return {'apiVersion': 'apps/v1', 'kind': 'DaemonSet',
            'metadata': dict(metadata(plan, 'nvidia-device-plugin-daemonset'), namespace='kube-system'),
            'spec': {'selector': {'matchLabels': labels}, 'template': {
                'metadata': {'labels': labels, 'annotations': {ANNOTATION: 'nvidia.com/gpu=all'}},
                'spec': {'automountServiceAccountToken': False, 'priorityClassName': 'system-node-critical',
                    'runtimeClassName': 'nvidia', 'nodeSelector': {SITE_LABEL: plan['site']},
                    'tolerations': [{'key': 'vxnip.nvke.dev/experiment', 'operator': 'Equal', 'value': 'true', 'effect': 'NoSchedule'}],
                    'containers': [{'name': 'nvidia-device-plugin', 'image': PLUGIN_IMAGE,
                        'env': [{'name': k, 'value': v} for k, v in {
                            'FAIL_ON_INIT_ERROR': 'true', 'MIG_STRATEGY': 'none', 'DEVICE_LIST_STRATEGY': 'cdi-cri',
                            'DEVICE_ID_STRATEGY': 'uuid', 'NVIDIA_VISIBLE_DEVICES': 'void',
                            'NVIDIA_DRIVER_CAPABILITIES': 'compute,utility', 'NVIDIA_DRIVER_ROOT': '/',
                            'NVIDIA_CDI_HOOK_PATH': '/usr/bin/nvidia-cdi-hook'}.items()],
                        'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                        'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'memory': '256Mi'}},
                        'volumeMounts': [{'name': 'plugins', 'mountPath': '/var/lib/kubelet/device-plugins'},
                            {'name': 'driver-root', 'mountPath': '/driver-root', 'readOnly': True},
                            {'name': 'cdi-root', 'mountPath': '/var/run/cdi'}]}],
                    'volumes': [{'name': 'plugins', 'hostPath': {'path': '/var/lib/kubelet/device-plugins', 'type': 'Directory'}},
                        {'name': 'driver-root', 'hostPath': {'path': '/', 'type': 'Directory'}},
                        {'name': 'cdi-root', 'hostPath': {'path': str(root) + '/cdi', 'type': 'Directory'}}]}}}}


def kube(settings):
    return ['kubectl', '--kubeconfig', str(Path(settings['root']) / 'tenant/workload.kubeconfig'), '--request-timeout=20s']


def gpu_targets(plan, bindings):
    return [(worker, binding) for worker, binding in zip(plan['workers'], bindings[1:]) if worker.get('gpu')]


def check_resources(nodes, targets):
    byname = {node['metadata']['name']: node for node in nodes}
    for worker, binding in targets:
        node = byname.get(binding['node'])
        count = str(len(worker['facts']['nvidia']['devices']))
        if (not node or node['metadata']['uid'] != binding['node_uid'] or node['spec'].get('providerID') != binding['provider_id']
                or node.get('metadata', {}).get('deletionTimestamp')
                or node['status'].get('capacity', {}).get('nvidia.com/gpu') != count
                or node['status'].get('allocatable', {}).get('nvidia.com/gpu') != count
                or not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status'].get('conditions', []))):
            raise ValueError('GPU capacity, Node identity or readiness differs from the planned physical worker')


def install_plugin(settings):
    import verify
    plan, root = settings['plan'], Path(settings['root'])
    bindings = verify.wait_bindings(settings)
    targets = gpu_targets(plan, bindings)
    if not targets:
        return
    k = kube(settings)
    for _, binding in targets:
        node = json.loads(run(k + ['get', 'node', binding['node'], '-o', 'json']))
        if node['metadata'].get('labels', {}).get(SITE_LABEL) not in (None, plan['site']):
            raise ValueError('GPU node label belongs to another site')
        run(k + ['label', 'node', binding['node'], SITE_LABEL + '=' + plan['site'], 'nvidia.com/gpu.present=true'])
    objects = [{'apiVersion': 'node.k8s.io/v1', 'kind': 'RuntimeClass', 'metadata': metadata(plan, 'nvidia'), 'handler': 'nvidia'},
               plugin_manifest(plan, root)]
    for obj in objects:
        ns = ['-n', obj['metadata']['namespace']] if obj['metadata'].get('namespace') else []
        if run(k + ns + ['get', obj['kind'], obj['metadata']['name'], '--ignore-not-found', '-o', 'name']):
            raise ValueError('GPU resource already exists; inspect ownership rather than adopting it')
        run(k + ['create', '-f', '-'], json.dumps(obj))
    run(k + ['-n', 'kube-system', 'rollout', 'status', 'daemonset/nvidia-device-plugin-daemonset', '--timeout=180s'])
    deadline = time.monotonic() + 180
    while True:
        try:
            check_resources(json.loads(run(k + ['get', 'nodes', '-o', 'json']))['items'], targets)
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def probe_pod(namespace, name, node, code_name):
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': namespace}, 'spec': {
        'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'runtimeClassName': 'nvidia',
        'nodeSelector': {'kubernetes.io/hostname': node['metadata']['labels']['kubernetes.io/hostname']},
        'tolerations': [{'key': 'vxnip.nvke.dev/experiment', 'operator': 'Equal', 'value': 'true', 'effect': 'NoSchedule'}],
        'containers': [{'name': 'cuda', 'image': PROBE_IMAGE, 'command': ['python', '-B', '/validation/check.py'],
            'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi', 'nvidia.com/gpu': 1}},
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
            'volumeMounts': [{'name': 'code', 'mountPath': '/validation', 'readOnly': True}]}],
        'volumes': [{'name': 'code', 'configMap': {'name': code_name}}]}}


def verify_gpu(settings, bindings):
    plan, root = settings['plan'], Path(settings['root'])
    targets = gpu_targets(plan, bindings)
    if not targets:
        return None
    k = kube(settings)
    nodes = json.loads(run(k + ['get', 'nodes', '-o', 'json']))['items']
    check_resources(nodes, targets)
    plugin = json.loads(run(k + ['-n', 'kube-system', 'get', 'daemonset', 'nvidia-device-plugin-daemonset', '-o', 'json']))
    if (plugin['metadata'].get('annotations', {}).get('vxnip.nvke.dev/plan') != identity(plan)
            or plugin['spec']['template'] != plugin_manifest(plan, root)['spec']['template']):
        # Kubernetes defaults extra fields; compare the managed subset below.
        expected = plugin_manifest(plan, root)['spec']['template']
        if plugin['metadata'].get('annotations', {}).get('vxnip.nvke.dev/plan') != identity(plan) or not subset(expected, plugin['spec']['template']):
            raise ValueError('GPU device plugin specification changed')
    namespace = json.loads(run(k + ['create', '-f', '-', '-o', 'json'], json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'generateName': 'vxnip-gpu-check-',
            'labels': {'vxnip.nvke.dev/bootstrap': plan['site']}}})))
    ns = namespace['metadata']['name']
    logs = root / 'verification-logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    record = {'namespace': ns, 'namespace_uid': namespace['metadata']['uid'], 'checked_at': int(time.time()), 'results': []}
    log = logs / ('gpu-' + str(time.time_ns()) + '.json')
    def save():
        log.write_text(json.dumps(record, indent=2)); log.chmod(0o600)
    save()
    try:
        run(k + ['-n', ns, 'create', 'configmap', 'cuda-check', '--from-file=check.py=' + str(root / 'runtime/gpu-cuda-check.py')])
        probes = []
        byname = {n['metadata']['name']: n for n in nodes}
        for worker, binding in targets:
            for _ in worker['facts']['nvidia']['devices']:
                name = 'cuda-' + str(len(probes))
                run(k + ['create', '-f', '-'], json.dumps(probe_pod(ns, name, byname[binding['node']], 'cuda-check')))
                probes.append((name, worker, binding))
        deadline = time.monotonic() + 300
        for name, worker, binding in probes:
            while True:
                pod = json.loads(run(k + ['-n', ns, 'get', 'pod', name, '-o', 'json']))
                phase = pod['status'].get('phase')
                if phase == 'Succeeded':
                    break
                if phase == 'Failed' or time.monotonic() >= deadline:
                    record['failed_pod'] = pod; save()
                    raise ValueError('CUDA Pod failed or timed out; inspect private verification log ' + str(log))
                time.sleep(2)
            output = json.loads(run(k + ['-n', ns, 'logs', name]))
            expected = {d['uuid'] for d in worker['facts']['nvidia']['devices']}
            if (output.get('cuda_kernel_passed') is not True or output.get('result') != 42
                    or output.get('gpu_uuid') not in expected or pod['spec']['nodeName'] != binding['node']):
                raise ValueError('CUDA output or allocated physical GPU differs')
            record['results'].append(dict(output, node=binding['node'], pod_uid=pod['metadata']['uid'],
                image_id=pod['status']['containerStatuses'][0]['imageID'])); save()
        for worker, binding in targets:
            actual = [r['gpu_uuid'] for r in record['results'] if r['node'] == binding['node']]
            if len(actual) != len(set(actual)) or set(actual) != {d['uuid'] for d in worker['facts']['nvidia']['devices']}:
                raise ValueError('CUDA probes did not cover every physical GPU exactly once')
        record['passed'] = True
    finally:
        # Delete only the exact namespace created by this invocation.
        current = json.loads(run(k + ['get', 'namespace', ns, '-o', 'json']))
        if current['metadata']['uid'] != record['namespace_uid']:
            raise ValueError('GPU probe namespace identity changed; refusing cleanup')
        run(k + ['delete', 'namespace', ns, '--wait=true', '--timeout=60s'])
        record['namespace_removed'] = True; save()
    return record


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(subset(a, b) for a, b in zip(expected, actual))
    return expected == actual


if __name__ == '__main__':
    action, filename = sys.argv[1:]
    value = json.loads(Path(filename).read_text())
    if action == 'worker':
        configure_worker(value)
    elif action == 'install':
        install_plugin(value)
    else:
        raise SystemExit('Unknown GPU action')
