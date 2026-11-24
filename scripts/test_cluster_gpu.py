"""Behavioral regression tests for opt-in NVIDIA/CDI bootstrap and site-bound clients."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/vxnip-cluster/scripts'))
sys.path.insert(0, str(ROOT / 'scripts'))
import client
import cluster
import gpu
import prepare
import preserve
import verify
import join
from types import SimpleNamespace
from test_cluster_bootstrap import facts


def gpu_facts(address='192.168.50.20', identity='b'):
    value = facts(address, identity)
    value.update(virtualization='', shared_cri=True, containerd_version='containerd v2.2.5', available_memory=100*2**30)
    value['nvidia'] = {'devices': [{'name': 'NVIDIA GB10', 'uuid': 'GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'driver_version': '580.178.04'}],
                       'toolkit_version': 'NVIDIA Container Toolkit CLI version 1.20.2', 'runtime': True, 'cdi_hook': True, 'cdi_all': True}
    return value


class GPUPlanningTests(unittest.TestCase):
    def setUp(self):
        self.config = {'manager': {'ssh': 'manager'}, 'workers': [{'ssh': 'spark-a', 'gpu': True}, {'ssh': 'spark-b', 'gpu': True}]}
        self.facts = {'manager': facts(), 'spark-a': gpu_facts(), 'spark-b': gpu_facts('192.168.50.30', 'c')}
        self.facts['manager']['free_disk'] = 80*2**30
        self.facts['spark-b']['nvidia']['devices'][0]['uuid'] = 'GPU-11111111-2222-3333-4444-555555555555'

    def plan(self):
        return cluster.make_plan(self.config, lambda alias: copy.deepcopy(self.facts[alias]))

    def test_two_sparks_are_opt_in_and_native_assets_are_shared(self):
        plan = self.plan()
        self.assertEqual(plan['version'], 4)
        self.assertTrue(all(w['gpu'] for w in plan['workers']))
        self.assertEqual(len(plan['artifacts']), 1)
        self.assertEqual(plan['artifacts']['arm64']['delivery'], 'direct-download')
        self.assertEqual(plan['workers'][0]['facts']['nvidia']['devices'][0]['uuid'], self.facts['spark-a']['nvidia']['devices'][0]['uuid'])

    def test_cpu_bootstrap_does_not_require_toolkit_or_enable_gpu(self):
        for worker in self.config['workers']:
            worker.pop('gpu')
            self.facts[worker['ssh']]['nvidia'] = None
        self.assertFalse(any(w['gpu'] for w in self.plan()['workers']))

    def test_gpu_prerequisites_fail_before_installation(self):
        for key in ('runtime', 'cdi_hook', 'cdi_all'):
            with self.subTest(key=key):
                value = copy.deepcopy(self.facts['spark-a']); value['nvidia'][key] = False
                with self.assertRaisesRegex(ValueError, 'Toolkit'):
                    gpu.validate_host(value)
        for version in ('', '1.17.9'):
            value = copy.deepcopy(self.facts['spark-a']); value['nvidia']['toolkit_version'] = version
            with self.assertRaisesRegex(ValueError, 'Toolkit'):
                gpu.validate_host(value)
        value = copy.deepcopy(self.facts['spark-a']); value['nvidia']['devices'] = []
        with self.assertRaisesRegex(ValueError, 'driver'):
            gpu.validate_host(value)

    def test_release_disk_budget_does_not_require_source_build_space(self):
        self.assertEqual(self.plan()['artifact_mode'], 'release')
        self.config['artifact_mode'] = 'source'
        with self.assertRaisesRegex(ValueError, '100 GiB'):
            self.plan()

    def test_all_hosts_are_discovered_and_blockers_are_reported_together(self):
        self.facts['manager']['free_disk'] = 2**30
        self.facts['spark-a']['allocated'] = True
        self.facts['spark-b']['nvidia'] = None
        with self.assertRaises(ValueError) as failure:
            self.plan()
        for alias in ('manager', 'spark-a', 'spark-b'):
            self.assertIn(alias, str(failure.exception))

    def test_named_manager_containers_are_preserved_not_stopped(self):
        self.facts['manager'].update(containers=['exact-id'], container_details=[{'id': 'exact-id', 'name': 'cache', 'image': 'sha256:a'}])
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            self.plan()
        self.config['manager']['preserve_containers'] = ['cache']
        self.assertEqual(self.plan()['manager']['preserve_containers'], ['cache'])
        self.facts['manager']['containers'].append('unexpected')
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            self.plan()

    def test_pipeline_prepares_gpu_before_join_and_verifies_on_every_resume(self):
        plan = self.plan(); actions = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            result = json.dumps({'ready': True, 'plan_digest': cluster.digest(plan)}).encode()
            with patch.object(cluster, 'probe', side_effect=lambda alias: copy.deepcopy(self.facts[alias])), \
                    patch.object(cluster, 'upload_runtime'), patch.object(cluster, 'transfer'), patch.object(cluster, 'transfer_artifacts'), \
                    patch.object(cluster, 'phase', side_effect=lambda node, plan, name, log: actions.append((node['ssh'], name))), \
                    patch.object(cluster, 'remote', return_value=result):
                cluster.apply(plan, state); cluster.apply(plan, state)
            for alias in ('spark-a', 'spark-b'):
                self.assertLess(actions.index((alias, 'worker')), actions.index((alias, 'gpu-worker')))
                self.assertLess(actions.index((alias, 'gpu-worker')), actions.index(('manager', 'join')))
                self.assertEqual(actions.count((alias, 'gpu-worker')), 1)
            self.assertLess(actions.index(('manager', 'join')), actions.index(('manager', 'gpu-plugin')))
            self.assertEqual(actions.count(('manager', 'gpu-plugin')), 1)
            self.assertEqual(actions.count(('manager', 'verify')), 2)
            self.assertEqual(actions.count(('manager', 'client')), 1)


class CDIManifestTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'site': 'vxnip-test', 'workers': []}
        self.root = Path('/var/lib/vxnip-bootstrap/vxnip-test')

    def test_isolated_cdi_does_not_relax_global_envvar_policy(self):
        cfg = tomllib.loads(gpu.runtime_config(self.root))['plugins']['io.containerd.cri.v1.runtime']
        self.assertTrue(cfg['enable_cdi'])
        self.assertIn(str(self.root / 'cdi'), cfg['cdi_spec_dirs'])
        self.assertEqual(cfg['containerd']['runtimes']['nvidia']['pod_annotations'], [gpu.ANNOTATION])
        self.assertNotIn('accept-nvidia-visible-devices-envvar-when-unprivileged', gpu.runtime_config(self.root))

    def test_gpu_import_preserves_the_existing_runc_table_and_newlines(self):
        base = "version = 3\n[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]\n  runtime_type = 'io.containerd.runc.v2'\n"
        result = tomllib.loads(gpu.import_runtime(base, self.root / 'gpu/containerd.toml'))
        self.assertEqual(result['imports'], [str(self.root / 'gpu/containerd.toml')])
        self.assertEqual(result['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']['runc']['runtime_type'], 'io.containerd.runc.v2')
        with self.assertRaises(ValueError):gpu.import_runtime(base.replace('version = 3', 'version = 2'), '/invalid')

    def test_driver_mount_and_private_cdi_output_are_required_for_both_sparks(self):
        ds = gpu.plugin_manifest(self.plan, self.root)
        spec = ds['spec']['template']['spec']; container = spec['containers'][0]
        env = {e['name']: e['value'] for e in container['env']}
        self.assertEqual(env['DEVICE_LIST_STRATEGY'], 'cdi-cri')
        self.assertEqual(env['NVIDIA_VISIBLE_DEVICES'], 'void')
        self.assertEqual(env['NVIDIA_CDI_HOOK_PATH'], '/usr/bin/nvidia-cdi-hook')
        mounts = {m['name']: m for m in container['volumeMounts']}
        self.assertEqual(mounts['driver-root'], {'name': 'driver-root', 'mountPath': '/driver-root', 'readOnly': True})
        volumes = {v['name']: v['hostPath']['path'] for v in spec['volumes']}
        self.assertEqual(volumes['driver-root'], '/')
        self.assertEqual(volumes['cdi-root'], str(self.root / 'cdi'))
        self.assertNotEqual(volumes['cdi-root'], '/var/run/cdi')
        self.assertEqual(spec['nodeSelector'], {gpu.SITE_LABEL: self.plan['site']})
        self.assertFalse(container['securityContext'].get('privileged', False))

    def test_gpu_probe_uses_scheduler_and_requests_a_real_device(self):
        pod = gpu.probe_pod('private-test', 'cuda-0', {'metadata': {'labels': {'kubernetes.io/hostname': 'worker-1'}}}, 'code')
        self.assertNotIn('nodeName', pod['spec'])
        self.assertEqual(pod['spec']['nodeSelector'], {'kubernetes.io/hostname': 'worker-1'})
        self.assertEqual(pod['spec']['containers'][0]['resources']['limits']['nvidia.com/gpu'], 1)
        self.assertFalse(pod['spec']['automountServiceAccountToken'])

    def test_capacity_alone_cannot_accept_another_node_or_allocation(self):
        binding = {'node': 'worker', 'node_uid': 'uid', 'provider_id': 'vxnip://allocation'}
        worker = {'facts': gpu_facts()}
        node = {'metadata': {'name': 'worker', 'uid': 'uid'}, 'spec': {'providerID': 'vxnip://allocation'},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'capacity': {'nvidia.com/gpu': '1'}, 'allocatable': {'nvidia.com/gpu': '1'}}}
        gpu.check_resources([node], [(worker, binding)])
        for path, value in [('uid', 'replaced'), ('providerID', 'vxnip://other'), ('gpu', '0')]:
            bad = copy.deepcopy(node)
            if path == 'uid': bad['metadata']['uid'] = value
            elif path == 'providerID': bad['spec']['providerID'] = value
            else: bad['status']['allocatable']['nvidia.com/gpu'] = value
            with self.assertRaises(ValueError):gpu.check_resources([bad], [(worker, binding)])

    def test_global_nvidia_policy_is_part_of_preservation(self):
        before = {'containers': [], 'shared_configuration': {'/etc/nvidia-container-runtime/config.toml': 'strict'}}
        self.assertTrue(preserve.preserved(before, copy.deepcopy(before)))
        self.assertFalse(preserve.preserved(before, {'containers': [], 'shared_configuration': {'/etc/nvidia-container-runtime/config.toml': 'relaxed'}}))


class PreparationTests(unittest.TestCase):
    def test_swap_and_cloud_init_need_explicit_options_and_known_facts(self):
        value = gpu_facts(); value.update(swap=True, swap_devices=[{'path': '/swap.img', 'type': 'file', 'used_kib': 0}],
                                         cloud_init={'status': 'disabled'}, cloud_init_preparable=True)
        self.assertEqual(prepare.actions({}, value, 'worker', 'baremetal'), [])
        options = {'prepare': {'disable_swap': True, 'cloud_init': True}}
        self.assertEqual(prepare.actions(options, value, 'worker', 'baremetal'), ['disable_swap', 'cloud_init'])
        value['cloud_init_preparable'] = False
        with self.assertRaisesRegex(ValueError, 'unknown seed'):
            prepare.actions(options, value, 'worker', 'baremetal')
        value['swap_devices'][0]['used_kib'] = 1
        with self.assertRaisesRegex(ValueError, 'unused'):
            prepare.actions(options, value, 'worker', 'baremetal')

    def test_swap_file_is_retained_and_unrelated_fstab_lines_are_unchanged(self):
        text = '# existing\nUUID=abc / ext4 defaults 0 1\n/swap.img none swap sw 0 0\n'
        devices = [{'path': '/swap.img', 'type': 'file', 'used_kib': 0}]
        updated = prepare.swap_fstab(text, devices)
        self.assertIn('UUID=abc / ext4 defaults 0 1\n', updated)
        self.assertIn('# /swap.img none swap sw 0 0', updated)
        for bad in (text+'OTHER none swap sw 0 0\n', text.replace('/swap.img', '/different')):
            with self.assertRaises(ValueError): prepare.swap_fstab(bad, devices)
        with self.assertRaises(ValueError):prepare.swap_fstab(text, [dict(devices[0], used_kib=4)])


class BoundClientTests(unittest.TestCase):
    def test_site_wrapper_overrides_wrong_environment_without_source_or_global_changes(self):
        with tempfile.TemporaryDirectory(prefix='vxnip client ') as directory:
            root = Path(directory); binary = root/'fake-nvkectl'
            binary.write_text('#!/bin/sh\nprintf "%s\\n" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$@"\n');binary.chmod(0o700)
            script = root/'client';script.write_text(client.wrapper(binary, root/'config', root/'cache', 'vxnip-test'));script.chmod(0o700)
            result = subprocess.check_output([str(script), 'get', 'nodepools'], env=dict(os.environ, XDG_CONFIG_HOME='/wrong', XDG_CACHE_HOME='/wrong'), text=True).splitlines()
            self.assertEqual(result, [str(root/'config'), str(root/'cache'), '--site', 'vxnip-test', 'get', 'nodepools'])


class FreshGPUVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workers = [{'gpu': True, 'facts': gpu_facts()['nvidia']} for _ in range(2)]
        self.workers = [{'gpu': True, 'facts': {'nvidia': w['facts']}} for w in self.workers]
        self.workers[1]['facts']['nvidia']['devices'][0]['uuid'] = 'GPU-11111111-2222-3333-4444-555555555555'
        self.plan = {'site': 'vxnip-test', 'workers': self.workers}
        self.settings = {'root': str(self.root), 'plan': self.plan}
        self.bindings = [{'node': 'cp'}] + [{'node': 'worker-'+str(i), 'node_uid': 'uid-'+str(i), 'provider_id': 'vxnip://allocation-'+str(i)} for i in range(2)]
        self.nodes = [{'metadata': {'name': b['node'], 'uid': b['node_uid'], 'labels': {'kubernetes.io/hostname': b['node']}},
                       'spec': {'providerID': b['provider_id']}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'capacity': {'nvidia.com/gpu': '1'}, 'allocatable': {'nvidia.com/gpu': '1'}}} for b in self.bindings[1:]]
        self.pods = {}; self.deleted = []; self.wrong_uuid = False

    def command(self, args, data=None):
        if 'nodes' in args:return json.dumps({'items': self.nodes})
        if 'daemonset' in args:return json.dumps(gpu.plugin_manifest(self.plan, self.root))
        if 'create' in args and '-f' in args:
            obj=json.loads(data)
            if obj['kind']=='Namespace':return json.dumps({'metadata': {'name': 'own-probe', 'uid': 'namespace-uid'}})
            self.pods[obj['metadata']['name']]=obj;return 'created'
        if 'configmap' in args:return 'created'
        if 'delete' in args:self.deleted.append(args[-3]);return 'deleted'
        if 'namespace' in args:return json.dumps({'metadata': {'name': 'own-probe', 'uid': 'namespace-uid'}})
        if 'logs' in args:
            pod=self.pods[args[-1]]; node=pod['spec']['nodeSelector']['kubernetes.io/hostname']; index=int(node.rsplit('-',1)[1])
            uuid=self.workers[index]['facts']['nvidia']['devices'][0]['uuid']
            return json.dumps({'cuda_kernel_passed': True, 'result': 42, 'gpu_uuid': 'GPU-wrong' if self.wrong_uuid else uuid})
        if 'pod' in args:
            name=args[args.index('pod')+1];pod=copy.deepcopy(self.pods[name]);pod['metadata']['uid']=name+'-uid'
            pod['spec']['nodeName']=pod['spec']['nodeSelector']['kubernetes.io/hostname']
            pod['status']={'phase': 'Succeeded', 'containerStatuses': [{'imageID': 'sha256:verified-image'}]}
            return json.dumps(pod)
        raise AssertionError(args)

    def test_fresh_cuda_results_are_bound_to_every_physical_uuid_and_cleaned(self):
        with patch.object(gpu,'run',side_effect=self.command):result=gpu.verify_gpu(self.settings,self.bindings)
        self.assertTrue(result['passed']); self.assertTrue(result['namespace_removed'])
        self.assertEqual({r['gpu_uuid'] for r in result['results']}, {w['facts']['nvidia']['devices'][0]['uuid'] for w in self.workers})
        self.assertEqual(len(self.pods),2);self.assertTrue(self.deleted)
        self.assertTrue(all((p.stat().st_mode & 0o777)==0o600 for p in (self.root/'verification-logs').iterdir()))

    def test_wrong_gpu_uuid_fails_and_still_cleans_only_own_namespace(self):
        self.wrong_uuid=True
        with patch.object(gpu,'run',side_effect=self.command):
            with self.assertRaisesRegex(ValueError,'physical GPU'):gpu.verify_gpu(self.settings,self.bindings)
        self.assertTrue(self.deleted)
        self.assertFalse(any(json.loads(p.read_text()).get('passed') for p in (self.root/'verification-logs').iterdir()))

    def test_cpu_only_does_not_create_pods_or_query_gpu(self):
        with patch.object(gpu,'run') as command:
            self.assertIsNone(gpu.verify_gpu({'plan': {'workers': [{}]}, 'root': str(self.root)},[{},{}]))
            command.assert_not_called()


class NeutralPoolNamingTests(unittest.TestCase):
    def test_pool_names_do_not_depend_on_gpu_or_cpu_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'site').mkdir();(root/'tenant').mkdir();(root/'site/known-hosts').write_text('')
            workers=[{'address':'192.168.50.20','user':'ubuntu','facts':gpu_facts(),'gpu':True},
                     {'address':'192.168.50.30','user':'ubuntu','facts':facts('192.168.50.30','c'),'gpu':False}]
            created={};pools=[]
            def command(args,data=None):
                if 'clusters' in args:return json.dumps([{'name':'vxnip-w-test','id':'cluster-id'}])
                if 'get' in args and 'vxniphost' in args:
                    name=args[args.index('vxniphost')+1]
                    return json.dumps(created.get(name)) if name in created else ''
                if 'create' in args and 'nodepool' in args:
                    pools.append(json.loads(Path(args[-1]).read_text()));return ''
                if 'create' in args:
                    obj=json.loads(data);obj['metadata']['generation']=1;obj['status']={'discoveryReady':True,'observedGeneration':1}
                    created[obj['metadata']['name']]=obj
                return ''
            with patch.object(join,'run',side_effect=command),patch.object(join.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')):
                join.main({'root':str(root),'plan':{'site':'vxnip-test','workload_cluster':'vxnip-w-test','workers':workers}})
            self.assertEqual([p['name'] for p in pools],['worker-1','worker-2'])
            self.assertEqual([p['nodeNamePrefix'] for p in pools],['worker1','worker2'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
