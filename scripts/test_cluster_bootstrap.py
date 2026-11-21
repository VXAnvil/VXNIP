"""Offline behavioral checks for the portable SSH bootstrap executor."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import stat
import unittest
from unittest.mock import patch
import zipfile

RUNTIME = Path(__file__).resolve().parents[1] / 'skills/vmetal-cluster/scripts'
sys.path.insert(0, str(RUNTIME))
import cluster  # noqa: E402
import reconcile  # noqa: E402
import verify  # noqa: E402
import audit  # noqa: E402
import preserve  # noqa: E402
import apt_guard  # noqa: E402
import settings  # noqa: E402


def facts(ip='192.168.50.10', identity='a', user='ubuntu'):
    return {'machine_id': identity * 64, 'hostname': 'node-' + identity, 'os': 'ubuntu',
            'version': '24.04', 'arch': 'arm64', 'cpus': 8, 'memory': 32 * 2**30,
            'free_disk': 150 * 2**30, 'swap': False, 'allocated': False, 'containers': [],
            'docker_networks': [], 'virtualization': 'kvm', 'kernel_config': True, 'kernel': 'fixture',
            'cloud_init': {'status': 'done', 'errors': [], 'recoverable_errors': {}},
            'host_key': 'ssh-ed25519 ' + identity * 40, 'user': user, 'users': ['ubuntu'],
            'address': ip, 'addresses': [{'addr_info': [{'family': 'inet', 'local': ip, 'prefixlen': 24}]}],
            'routes': [{'dst': '192.168.50.0/24'}, {'dst': 'default', 'gateway': '192.168.50.1'}]}


class RuntimeAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('publisher', RUNTIME.parents[2] / 'scripts/package-runtime-assets.py')
        self.publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.publisher)
        self.input = self.root / 'input'
        self.input.mkdir()
        (self.input / 'node-tool').write_bytes(b'verified tool')
        digest = self.publisher.sha256(self.input / 'node-tool')
        (self.input / 'SHA256SUMS').write_text(digest + '  node-tool\n')
        (self.input / 'unused-build-output').write_text('not needed on user machines')

    def test_native_publisher_archive_round_trips_without_build_intermediates(self):
        asset = self.publisher.pack(self.input, 'node', 'amd64', self.root, 'runtime-v0.1.0-test')
        cluster.runtime_assets.validate_asset(asset, 'node', 'amd64')
        staging = self.root / 'staging'
        staging.mkdir()
        archive = self.root / 'vmetal-node-amd64.tar.gz'
        result = cluster.runtime_assets.unpack(archive, staging, asset, cluster.RELEASE)
        self.assertEqual((result / 'node-tool').read_bytes(), b'verified tool')
        self.assertFalse((result / 'unused-build-output').exists())
        self.assertEqual(self.publisher.sha256(archive), asset['sha256'])

    def test_publication_requires_all_four_archives_with_matching_hashes(self):
        spec = importlib.util.spec_from_file_location('release_verify', RUNTIME.parents[2] / 'scripts/verify-runtime-release.py')
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        tag = 'runtime-v0.1.0-test'
        for arch in ('amd64', 'arm64'):
            assets = {}
            for role in ('manager', 'node'):
                assets[role + '-' + arch] = self.publisher.pack(self.input, role, arch, self.root, tag)
            (self.root / ('manifest-' + arch + '.json')).write_text(json.dumps({'schema':1, 'sources':cluster.RELEASE, 'assets':assets}))
        self.assertEqual(len(verifier.verify(self.root, tag)['assets']), 4)
        with self.assertRaisesRegex(ValueError, 'release tag differs'):
            verifier.verify(self.root, 'runtime-v0.1.0-other')
        with (self.root / 'vmetal-manager-amd64.tar.gz').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            verifier.verify(self.root, tag)

    def test_install_uses_verified_staging_and_matching_receipt(self):
        import shutil
        asset = self.publisher.pack(self.input, 'node', 'amd64', self.root, 'runtime-v0.1.0-test')
        settings = {'root':str(self.root / 'state'), 'node':{'facts':{'arch':'amd64'}},
                    'plan':{'release':cluster.RELEASE, 'artifacts':{'amd64':{'asset':asset}}}}
        Path(settings['root']).mkdir()
        archive = self.root / 'vmetal-node-amd64.tar.gz'
        with patch.object(cluster.runtime_assets, 'download', side_effect=lambda asset, target:shutil.copyfile(archive,target)) as download:
            cluster.runtime_assets.install(settings, 'node')
            cluster.runtime_assets.install(settings, 'node')
            self.assertEqual(download.call_count, 1)
        work = Path(settings['root']) / 'work'
        self.assertEqual((work / 'artifacts/node-tool').read_bytes(), b'verified tool')
        self.assertEqual(json.loads((work / '.installed-node.json').read_text()), asset)
        self.assertFalse(list(work.glob('.runtime-*')))
        (work / '.installed-node.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'matching receipt'):
            cluster.runtime_assets.install(settings, 'node')

    def test_downloaded_bytes_must_match_before_installation(self):
        import io
        asset = self.publisher.pack(self.input, 'node', 'amd64', self.root, 'runtime-v0.1.0-test')
        for data in (b'corrupt', b'X' * (asset['size'] + 1)):
            with self.subTest(size=len(data)):
                response = io.BytesIO(data)
                response.url = asset['url']
                with patch.object(cluster.runtime_assets.urllib.request, 'build_opener') as opener:
                    opener.return_value.open.return_value = response
                    output = self.root / 'download'
                    with self.assertRaises(cluster.runtime_assets.DownloadBlocked):
                        cluster.runtime_assets.download(asset, output)
                    output.unlink(missing_ok=True)

    def test_untrusted_archive_paths_and_links_are_rejected(self):
        import io
        import tarfile
        for name, link in (('../escape', False), ('/escape', False), ('artifacts/link', True)):
            with self.subTest(name=name):
                archive = self.root / 'malicious.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type, member.linkname = tarfile.SYMTYPE, '/etc/passwd'
                    else:
                        member.size = 1
                    output.addfile(member, None if link else io.BytesIO(b'x'))
                with self.assertRaises(cluster.runtime_assets.DownloadBlocked):
                    cluster.runtime_assets.unpack(archive, self.root, {'role':'node', 'unpacked_size':100}, cluster.RELEASE)
                self.assertFalse((self.root.parent / 'escape').exists())

    def test_manager_archive_does_not_publish_site_credentials(self):
        (self.input / 'tenant').mkdir()
        (self.input / 'tenant/workload.kubeconfig').write_text('private fixture')
        with self.assertRaisesRegex(ValueError, 'Private state'):
            self.publisher.pack(self.input, 'manager', 'arm64', self.root, 'runtime-v0.1.0-test')

    def test_prebuilt_manifest_is_required_without_source_fallback(self):
        missing = self.root / 'missing.json'
        with self.assertRaisesRegex(ValueError, 'manifest is missing'):
            cluster.runtime_assets.catalog(missing, cluster.RELEASE)
        config = {'manager':{'ssh':'manager'}, 'workers':[{'ssh':'worker'}]}
        with patch.object(cluster, 'BUNDLE_MANIFEST', missing), patch.object(cluster, 'probe') as probe:
            with self.assertRaisesRegex(ValueError, 'manifest is missing'):
                cluster.make_plan(config)
            probe.assert_not_called()


class ImagePortabilityTests(unittest.TestCase):
    def setUp(self):
        import hashlib,io,tarfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'management-bundle'
        self.source.mkdir()
        self.names = ['kindest/node:test', 'vmetal/controller:test', 'vmetal/profile:test']
        (self.source / 'images').write_text('\n'.join(self.names)+'\n')
        (self.source / 'image-ids').write_text(('sha256:'+'a'*64+'\n')*3)
        self.actual = {}
        manifests = []
        with tarfile.open(self.source / 'images.tar','w') as archive:
            for i,name in enumerate(self.names):
                cfg = {'architecture':'arm64', 'os':'linux', 'rootfs':{'diff_ids':['sha256:'+str(i)*64]},
                       'config':{'Env':['MODE=original'], 'Entrypoint':['/bin/run'], 'Labels':{'empty':''}, 'ArgsEscaped':False}}
                data=json.dumps(cfg).encode(); filename=str(i)+'.json'
                member=tarfile.TarInfo(filename);member.size=len(data);archive.addfile(member,io.BytesIO(data))
                manifests.append({'RepoTags':[name], 'Config':filename})
                self.actual[name]={'Id':'sha256:'+str(i+1)*64,'Architecture':'arm64','Os':'linux',
                                   'RootFS':{'Layers':cfg['rootfs']['diff_ids']}, 'Config':dict(cfg['config'],Cmd=None)}
            data=json.dumps(manifests).encode();member=tarfile.TarInfo('manifest.json');member.size=len(data);archive.addfile(member,io.BytesIO(data))
        sums=[hashlib.sha256(f.read_bytes()).hexdigest()+'  '+f.name for f in self.source.iterdir()]
        (self.source / 'SHA256SUMS').write_text('\n'.join(sums)+'\n')
        import images
        self.images=images

    def test_local_ids_are_derived_without_changing_verified_source(self):
        before={p.name:p.read_bytes() for p in self.source.iterdir()}
        dest=self.root / 'management-runtime'
        self.images.prepare(self.source,dest,'arm64',load=lambda:None,inspect=lambda name:self.actual[name])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.source.iterdir()})
        self.assertNotEqual((dest/'image-ids').read_bytes(),(self.source/'image-ids').read_bytes())
        self.assertEqual((dest/'images.tar').stat().st_ino,(self.source/'images.tar').stat().st_ino)
        with self.assertRaisesRegex(RuntimeError,'already exists'):
            self.images.prepare(self.source,dest,'arm64',load=lambda:None,inspect=lambda name:self.actual[name])

    def test_changed_layers_config_or_platform_are_not_accepted(self):
        for field in ('env','label','layer','arch'):
            with self.subTest(field=field):
                actual=copy.deepcopy(self.actual)
                image=actual[self.names[0]]
                if field=='env':image['Config']['Env']=['MODE=changed']
                elif field=='label':image['Config']['Labels']={'empty':'changed'}
                elif field=='layer':image['RootFS']['Layers']=['sha256:'+'f'*64]
                else:image['Architecture']='amd64'
                dest=self.root / ('bad-'+field)
                with self.assertRaisesRegex(ValueError,'differs'):
                    self.images.prepare(self.source,dest,'arm64',load=lambda:None,inspect=lambda name:actual[name])
                self.assertFalse(dest.exists())


class LANRouteTests(unittest.TestCase):
    def setUp(self):
        import routes
        self.routes=routes
        self.plan={'mode':'create','network_mode':'lan','manager':{'address':'192.168.50.10'},'networks':{'docker':'172.30.91.0/24'}}
        self.node={'address':'192.168.50.20','subnet':'192.168.50.0/24'}
        self.ifaces=[{'ifname':'lan0','addr_info':[{'local':self.node['address']}]}]
        self.current=[{'gateway':'192.168.50.10','dev':'lan0','prefsrc':'192.168.50.20'}]
        self.calls=[]

    def command(self, args, text=True):
        self.calls.append(args)
        if args==['ip','-j','-4','address','show']:return json.dumps(self.ifaces)
        if args==['ip','-j','-4','route','get','172.30.91.20']:return json.dumps(self.current)
        return ''

    def test_route_restored_after_netplan_discards_it_or_replaces_interface(self):
        self.assertFalse(self.routes.ensure(self.plan,self.node,self.command))
        self.current=[{'gateway':'10.0.2.2','dev':'brnat0','prefsrc':'10.0.2.15'}]
        self.ifaces[0]['ifname']='brlan0'
        before=copy.deepcopy(self.current)
        self.assertTrue(self.routes.ensure(self.plan,self.node,self.command))
        self.assertEqual(self.calls[-1],['ip','-4','route','replace','172.30.91.20/32','via','192.168.50.10','dev','brlan0','src','192.168.50.20'])
        self.assertEqual(self.current,before)
        self.assertEqual(sum(c[1:4]==['-4','route','replace'] for c in self.calls),1)

    def test_no_default_route_still_restores_the_explicit_api_route(self):
        def command(args, text=True):
            if args[1:5]==['-j','-4','route','get']:
                raise subprocess.CalledProcessError(2,args)
            return self.command(args,text=text)
        self.assertTrue(self.routes.ensure(self.plan,self.node,command))
        self.assertEqual(self.calls[-1][4],'172.30.91.20/32')

    def test_missing_address_waits_without_changing_any_route(self):
        self.ifaces=[]
        with self.assertRaisesRegex(RuntimeError,'Waiting'):
            self.routes.ensure(self.plan,self.node,self.command)
        self.assertEqual(len(self.calls),1)

    def test_existing_and_tailscale_modes_cannot_use_lan_route_watch(self):
        for field,value in (('mode','existing'),('network_mode','tailscale')):
            plan={**self.plan,field:value}
            with self.assertRaises(ValueError):self.routes.ensure(plan,self.node,self.command)
        self.assertEqual(self.calls,[])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = {'artifact_mode': 'source', 'manager': {'ssh': 'manager'}, 'workers': [{'ssh': 'worker'}]}
        self.machines = {'manager': facts(), 'worker': facts('192.168.50.20', 'b')}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def plan(self):
        return cluster.make_plan(self.config, lambda alias: copy.deepcopy(self.machines[alias]))

    def test_connections_alone_choose_unused_networks(self):
        plan = self.plan()
        self.assertEqual(plan['manager']['address'], '192.168.50.10')
        self.assertEqual(plan['workers'][0]['address'], '192.168.50.20')
        self.assertEqual(len(plan['networks']), 3)
        self.assertEqual(plan['release'], cluster.RELEASE)

    def test_default_lan_release_prepares_every_mixed_worker_without_tailscale(self):
        # No source-build override, Tailscale executable, prefs or interface.
        self.config.pop('artifact_mode')
        self.config['workers'] += [{'ssh': 'worker2'}, {'ssh': 'worker3'}]
        self.machines['worker2'] = facts('192.168.50.30', 'c')
        self.machines['worker3'] = facts('192.168.50.40', 'd')
        self.machines['worker']['arch'] = 'amd64'
        self.machines['worker3']['arch'] = 'amd64'
        for alias, machine in self.machines.items():
            machine['tailscale'] = None
            machine['tailscale_state'] = None
            if alias != 'manager':
                machine.update(cpus=2, memory=4 * 2**30, free_disk=20 * 2**30)
        assets = {role + '-' + arch: {'role': role, 'arch': arch}
                  for role in ('manager', 'node') for arch in ('amd64', 'arm64')}
        with patch.object(cluster.runtime_assets, 'catalog', return_value={'assets': assets}):
            plan = self.plan()
        self.assertEqual(plan['network_mode'], 'lan')
        self.assertEqual(plan['artifact_mode'], 'release')
        self.assertEqual(plan['manager_asset'], assets['manager-arm64'])
        self.assertEqual(plan['artifacts']['amd64']['asset'], assets['node-amd64'])
        self.assertEqual(plan['artifacts']['arm64']['asset'], assets['node-arm64'])
        actions = []
        result = json.dumps({'ready': True, 'plan_digest': cluster.digest(plan)}).encode()
        with patch.object(cluster, 'probe', side_effect=lambda alias: copy.deepcopy(self.machines[alias])), \
                patch.object(cluster, 'upload_runtime'), patch.object(cluster, 'transfer'), \
                patch.object(cluster, 'transfer_artifacts') as transfers, \
                patch.object(cluster, 'phase', side_effect=lambda node, plan, name, log: actions.append((node['ssh'], name))), \
                patch.object(cluster, 'remote', return_value=result):
            cluster.apply(plan, self.state)
        self.assertEqual({alias for alias, phase in actions if phase == 'worker'}, {'worker', 'worker2', 'worker3'})
        self.assertEqual({alias for alias, phase in actions if phase == 'fetch-node'}, {'worker', 'worker2'})
        self.assertIn(('manager', 'fetch-manager'), actions)
        self.assertIn(('manager', 'verify'), actions)
        self.assertEqual(transfers.call_count, 1)
        self.assertFalse(any(name in ('build', 'node-assets', 'tailscale-worker') for _, name in actions))

    def test_lan_does_not_change_installed_but_unauthenticated_tailscale(self):
        for machine in self.machines.values():
            machine['tailscale_state'] = 'NeedsLogin'
            machine['tailscale'] = {'AdvertiseRoutes': ['192.168.100.0/24'], 'RouteAll': False}
        before = copy.deepcopy(self.machines)
        self.assertEqual(self.plan()['network_mode'], 'lan')
        self.assertEqual(self.machines, before)

    def test_lan_rejects_a_different_subnet_before_installation(self):
        self.machines['worker'] = facts('192.168.60.20', 'b')
        with self.assertRaisesRegex(ValueError, 'same directly connected LAN'):
            self.plan()

    def test_management_and_workload_containers_never_share_a_name(self):
        for site in ('vmetal-lab', 'vmetal-' + 'a' * 20):
            self.config['site'] = site
            plan = self.plan()
            shell = settings.values({'plan': plan, 'root': '/fixture', 'node': plan['manager']})
            self.assertNotEqual(shell['CLUSTER'] + '-control-plane', site + '-control-plane')
            self.assertEqual(shell['CLUSTER'], plan['workload_cluster'])
            self.assertRegex(shell['CLUSTER'], r'^vmetal-[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?$')

    def test_reviewed_plan_cannot_reuse_the_management_name_for_workloads(self):
        plan = self.plan()
        plan['workload_cluster'] = plan['site']
        path = self.state / 'plan.json'
        cluster.save(path, plan)
        with self.assertRaisesRegex(ValueError, 'distinct'):
            cluster.checked_plan(path, cluster.digest(plan))

    def test_multiple_workers_and_root_ssh_account_selection(self):
        self.config['workers'][0]['worker_user'] = 'ubuntu'
        self.machines['worker']['user'] = 'root'
        self.config['workers'].append({'ssh': 'worker2'})
        self.machines['worker2'] = facts('192.168.50.30', 'c')
        self.assertEqual([w['user'] for w in self.plan()['workers']], ['ubuntu', 'ubuntu'])

    def test_incompatible_runtime_or_allocated_machines_are_blocked(self):
        for field, value in [('virtualization', ''), ('allocated', True),
                             ('swap', True), ('free_disk', 2**30), ('containers', ['unrelated'])]:
            with self.subTest(field=field):
                original = self.machines['worker'][field]
                self.machines['worker'][field] = value
                with self.assertRaises(ValueError):
                    self.plan()
                self.machines['worker'][field] = original

    def test_duplicate_physical_machine_cannot_be_allocated_twice(self):
        self.machines['worker']['machine_id'] = self.machines['manager']['machine_id']
        with self.assertRaisesRegex(ValueError, 'distinct machine IDs'):
            self.plan()

    def test_network_selection_checks_routes_and_docker_networks(self):
        self.machines['worker']['routes'].append({'dst': '10.220.0.0/16'})
        self.machines['manager']['docker_networks'] = ['172.30.91.0/24']
        plan = self.plan()
        self.assertEqual(plan['networks']['pod'], '10.221.0.0/16')
        self.assertNotEqual(plan['networks']['docker'], '172.30.91.0/24')
        self.config['networks'] = {'docker': '192.168.50.0/24'}
        with self.assertRaises(ValueError):
            self.plan()

    def test_cloud_init_errors_are_not_silently_accepted(self):
        self.machines['worker']['cloud_init']['recoverable_errors'] = {'NETWORK': ['failed']}
        with self.assertRaisesRegex(ValueError, 'cloud-init'):
            self.plan()

    def test_manager_seed_media_and_worker_existing_cni_are_blocked(self):
        self.machines['manager']['seed_media'] = ['sr0']
        with self.assertRaisesRegex(ValueError, 'config-drive'):
            self.plan()
        self.machines['manager']['seed_media'] = []
        self.machines['worker']['existing_cni'] = True
        with self.assertRaisesRegex(ValueError, 'CNI'):
            self.plan()

    def test_rfc1918_and_ovn_bridge_conflicts_are_checked(self):
        self.config['networks'] = {'docker': '169.254.10.0/24'}
        with self.assertRaises(ValueError):
            self.plan()
        self.config.pop('networks')
        self.machines['worker']['routes'].append({'dst': '10.44.0.0/24'})
        with self.assertRaisesRegex(ValueError, 'OVN local bridge'):
            self.plan()

    def test_auth_failure_is_actionable_without_leaking_raw_stderr(self):
        completed = subprocess.CompletedProcess([], 255, b'', b'Permission denied (publickey). secret-marker')
        with patch.object(subprocess, 'run', return_value=completed):
            with self.assertRaisesRegex(RuntimeError, 'IdentityFile') as error:
                cluster.remote('manager', 'true')
        self.assertNotIn('secret-marker', str(error.exception))

    def test_ssh_options_cannot_be_injected(self):
        for target in ['-oProxyCommand=id', 'user@host;id', 'host\ncommand', 'host $(id)']:
            with self.subTest(target=target), self.assertRaises(ValueError):
                cluster.ssh(target, 'true')
        command = cluster.ssh('ubuntu@worker.local', 'true')
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('ForwardAgent=no', command)

    def test_existing_authenticated_socket_is_supported_and_bound_into_plan(self):
        target = {'ssh': 'ubuntu@worker', 'control_path': str(self.state / 'master.sock')}
        info = os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0))
        with patch.object(Path, 'lstat', return_value=info):
            args = cluster.ssh(target, 'true')
        self.assertIn('-S', args)
        self.assertIn(target['control_path'], args)
        self.assertIn('ControlMaster=no', args)
        self.config['workers'][0]['control_path'] = target['control_path']
        plan = cluster.make_plan(self.config, lambda value: copy.deepcopy(
            self.machines[value['ssh'] if isinstance(value, dict) else value]))
        self.assertEqual(plan['workers'][0]['control_path'], target['control_path'])

    def test_regular_file_cannot_be_used_as_control_socket(self):
        fake = self.state / 'not-a-socket'
        fake.write_text('fixture')
        with self.assertRaisesRegex(ValueError, 'existing socket'):
            cluster.ssh({'ssh': 'worker', 'control_path': str(fake)}, 'true')

    def test_plan_digest_and_runtime_changes_block_execution(self):
        plan = self.plan()
        path = self.state / 'plan.json'
        cluster.save(path, plan)
        cluster.checked_plan(path, cluster.digest(plan))
        with self.assertRaisesRegex(ValueError, 'digest'):
            cluster.checked_plan(path, 'wrong')
        plan['runtime']['cluster.py'] = '0' * 64
        cluster.save(path, plan)
        with self.assertRaisesRegex(ValueError, 'Runtime/source'):
            cluster.checked_plan(path)

    def test_private_records_are_atomic_and_not_world_readable(self):
        path = self.state / 'record.json'
        cluster.save(path, {'id': 'test'})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {'id': 'test'})
        self.assertEqual(list(self.state.glob('.next-*')), [])

    def test_lost_mutation_response_never_causes_automatic_replay(self):
        effects = []

        def partial_action():
            effects.append('started')
            raise RuntimeError('lost reply')

        with self.assertRaises(RuntimeError):
            cluster.run_step(self.state, 'management', partial_action)
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            cluster.run_step(self.state, 'management', partial_action)
        self.assertEqual(effects, ['started'])
        self.assertEqual(json.loads((self.state / 'journal.json').read_text())['management']['state'], 'unknown')

    def test_completed_checkpoints_are_skipped(self):
        effects = []
        cluster.run_step(self.state, 'build', lambda: effects.append('build'))
        cluster.run_step(self.state, 'build', lambda: effects.append('build'))
        self.assertEqual(effects, ['build'])

    def test_apply_runs_install_once_but_verifies_each_time(self):
        plan = self.plan()
        actions = []
        result = json.dumps({'ready': True, 'plan_digest': cluster.digest(plan)}).encode()
        with patch.object(cluster, 'probe', side_effect=lambda alias: copy.deepcopy(self.machines[alias])), \
                patch.object(cluster, 'upload_runtime'), patch.object(cluster, 'transfer'), patch.object(cluster, 'transfer_artifacts'), \
                patch.object(cluster, 'phase', side_effect=lambda node, plan, name, log: actions.append(name)), \
                patch.object(cluster, 'remote', return_value=result):
            cluster.apply(plan, self.state)
            cluster.apply(plan, self.state)
        self.assertEqual(actions.count('build'), 1)
        self.assertEqual(actions.count('worker'), 1)
        self.assertEqual(actions.count('verify'), 2)

    def test_failed_verification_cannot_leave_old_ready_result(self):
        plan = self.plan()
        cluster.save(self.state / 'binding.json', {'plan': cluster.digest(plan)})
        cluster.save(self.state / 'result.json', {'ready': True})
        with patch.object(cluster, 'probe', side_effect=lambda alias: self.machines[alias]), \
                patch.object(cluster, 'run_step'), \
                patch.object(cluster, 'phase', side_effect=RuntimeError('not Ready')):
            with self.assertRaises(RuntimeError):
                cluster.apply(plan, self.state)
        self.assertFalse(json.loads((self.state / 'result.json').read_text())['ready'])

    def test_preconditions_are_rechecked_if_only_staging_was_completed(self):
        plan = self.plan()
        cluster.save(self.state / 'binding.json', {'plan': cluster.digest(plan)})
        cluster.save(self.state / 'journal.json', {'upload-manager': {'state': 'completed'}})
        self.machines['worker']['allocated'] = True
        with patch.object(cluster, 'probe', side_effect=lambda alias: self.machines[alias]), \
                patch.object(cluster, 'run_step') as start:
            with self.assertRaisesRegex(ValueError, 'allocation'):
                cluster.apply(plan, self.state)
            start.assert_not_called()

    def test_identity_changes_stop_before_install(self):
        plan = self.plan()
        self.machines['worker']['machine_id'] = 'c' * 64
        with patch.object(cluster, 'probe', side_effect=lambda alias: self.machines[alias]), \
                patch.object(cluster, 'run_step') as start:
            with self.assertRaisesRegex(ValueError, 'identity'):
                cluster.apply(plan, self.state)
            start.assert_not_called()

    def test_reconciliation_requires_success_receipt_and_does_not_replay(self):
        plan = self.plan()
        cluster.save(self.state / 'binding.json', {'plan': cluster.digest(plan)})
        cluster.save(self.state / 'journal.json', {'management': {'state': 'unknown'}})
        with patch.object(cluster, 'probe', return_value=self.machines['manager']), \
                patch.object(cluster, 'remote', side_effect=RuntimeError('missing receipt')):
            with self.assertRaises(RuntimeError):
                reconcile.reconcile(plan, self.state, 'management')
        self.assertEqual(json.loads((self.state / 'journal.json').read_text())['management']['state'], 'unknown')
        with patch.object(cluster, 'probe', return_value=self.machines['manager']), \
                patch.object(cluster, 'remote') as call:
            result = reconcile.reconcile(plan, self.state, 'management')
        self.assertFalse(result['replayed'])
        self.assertIn('completed.management', call.call_args.args[1])
        self.assertIn('flock -n', call.call_args.args[1])

    def test_package_has_no_site_state_or_unrelated_files(self):
        script = Path(__file__).parent / 'package-cluster-skill.py'
        spec = importlib.util.spec_from_file_location('package_skill', script)
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        output = self.state / 'skill.zip'
        package.package(output, development=True)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('vmetal-cluster/SKILL.md', archive.namelist())
            self.assertIn('vmetal-cluster/scripts/public-inputs.sh', archive.namelist())
            self.assertTrue(all(name.startswith('vmetal-cluster/') for name in archive.namelist()))
            self.assertFalse(any('__pycache__' in name or 'settings.json' in name for name in archive.namelist()))

    def test_service_probe_cleans_up_only_its_namespace_on_success_and_failure(self):
        fake = self.state / 'fakebin'
        fake.mkdir()
        records = self.state / 'calls.jsonl'
        kubectl = fake / 'kubectl'
        kubectl.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
args=sys.argv[1:]
if 'create' in args:
    sys.stdin.read()
    if '-o' in args: print('vmetal-service-check-fixture',end='')
elif 'exec' in args:
    if os.environ['PROBE_FAIL']=='1': sys.exit(1)
    print('vmetal-ok')
''')
        kubectl.chmod(0o755)
        sleep = fake / 'sleep'
        sleep.write_text('#!/bin/sh\nexit 0\n')
        sleep.chmod(0o755)
        for fail in ('0', '1'):
            with self.subTest(fail=fail):
                records.unlink(missing_ok=True)
                result = subprocess.run(['bash', str(RUNTIME / 'service-check.sh'), '/fixture/kubeconfig', 'cp', 'worker'],
                                        env=dict(os.environ, PATH=str(fake)+os.pathsep+os.environ['PATH'],
                                                 CALLS=str(records), PROBE_FAIL=fail),
                                        capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, int(fail))
                calls = [json.loads(line) for line in records.read_text().splitlines()]
                deleted = [call for call in calls if 'delete' in call]
                self.assertEqual(len(deleted), 1)
                self.assertIn('vmetal-service-check-fixture', deleted[0])


    def test_pod_probe_waits_for_http_before_testing_both_directions(self):
        fake = self.state / 'pod-fakebin'
        fake.mkdir()
        records, state = self.state / 'pod-calls.jsonl', self.state / 'pod-state.json'
        kubectl = fake / 'kubectl'
        kubectl.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
a=sys.argv[1:]; p=Path(os.environ['STATE'])
s=json.loads(p.read_text()) if p.exists() else {'probes':[], 'serving':False}
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(a)+'\\n')
if 'config' in a: print('https://192.168.50.10:6443',end='')
elif 'create' in a:
    sys.stdin.read(); print('vmetal-worker-check-fixture',end='')
elif 'apply' in a:
    manifest=sys.stdin.read()
    s['probes'].append('readinessProbe:' in manifest and 'httpGet:' in manifest)
elif 'wait' in a and 'pods' in a:
    # A plain Pod Ready can precede HTTP startup. HTTP readiness waits for listening.
    s['serving']=bool(s['probes']) and all(s['probes'])
elif 'get' in a and 'node' in a:
    print(json.dumps({'status':{'addresses':[{'type':'InternalIP','address':'192.168.50.20'}]}}))
elif 'get' in a and 'pod' in a: print('10.7.0.2' if 'worker-1' in a else '10.7.0.1',end='')
elif 'exec' in a:
    if not s['serving'] or os.environ['PROBE_FAIL']=='1': sys.exit(17)
    print('worker-1' if 'http://10.7.0.2:8080/' in a else 'worker-0')
p.write_text(json.dumps(s))
''')
        kubectl.chmod(0o755)
        for name, body in {'timeout':'#!/bin/sh\nshift\nexec "$@"\n',
                           'openssl':'#!/bin/sh\ncat >/dev/null\necho fixture-public-certificate\n'}.items():
            path = fake / name
            path.write_text(body)
            path.chmod(0o755)
        for script in [RUNTIME / 'pod-check.sh', Path(__file__).parent / 'check-nvke-lab-workers.sh']:
            for fail in ('0', '1'):
                with self.subTest(script=script.name, fail=fail):
                    records.unlink(missing_ok=True)
                    state.unlink(missing_ok=True)
                    result = subprocess.run(['bash', str(script), '/fixture/kubeconfig',
                                             'https://192.168.50.10:6443', 'cp', 'worker'],
                                            env=dict(os.environ, PATH=str(fake)+os.pathsep+os.environ['PATH'],
                                                     CALLS=str(records), STATE=str(state), PROBE_FAIL=fail),
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, int(fail), result.stdout + result.stderr)
                    calls = [json.loads(line) for line in records.read_text().splitlines()]
                    self.assertEqual(len([c for c in calls if 'exec' in c]), 2)
                    deleted = [c for c in calls if 'delete' in c]
                    self.assertEqual(len(deleted), 1)
                    self.assertIn('vmetal-worker-check-fixture', deleted[0])

    def test_probe_failure_keeps_private_diagnostics_without_echoing_them(self):
        log = self.state / 'probe.log'
        with self.assertRaises(ValueError) as failure:
            verify.run_probe([sys.executable, '-c',
                              'import sys; print("fixture-private-diagnostic"); sys.exit(1)'], log)
        self.assertIn(str(log), str(failure.exception))
        self.assertNotIn('fixture-private-diagnostic', str(failure.exception))
        self.assertIn('fixture-private-diagnostic', log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)


class MixedEnvironmentTests(unittest.TestCase):
    setUp = BootstrapTests.setUp
    plan = BootstrapTests.plan

    def mixed(self, existing=False):
        self.machines['manager']['arch'] = 'amd64'
        worker = self.machines['worker']
        worker.update(virtualization='', containerd_version='containerd v2.2.0', shared_cri=True,
                      address='100.101.10.20', containers=['existing-application'], allocated=existing)
        worker['addresses'] = [{'ifname': 'tailscale0', 'mtu': 1280, 'addr_info': [
            {'family':'inet','local':'100.101.10.20','prefixlen':32}]}]
        worker['tailscale'] = {'RouteAll': True, 'NoSNAT': False, 'AdvertiseRoutes': []}
        worker['tailscale_state'] = 'Running'
        self.machines['manager']['addresses'][0]['ifname'] = 'eth0'
        self.machines['manager']['addresses'].append({'ifname':'tailscale0','addr_info':[
            {'family':'inet','local':'100.101.10.10','prefixlen':32}]})
        self.machines['manager']['tailscale'] = {'RouteAll':False,'NoSNAT':True,'AdvertiseRoutes':['172.30.91.20/32','192.168.50.10/32']}
        self.machines['manager']['tailscale_state'] = 'Running'
        self.config['network_mode'] = 'tailscale'
        if existing:
            self.config.update(mode='existing',existing={
                'management_kubeconfig':'/private/management.kubeconfig',
                'workload_kubeconfig':'/private/workload.kubeconfig',
                'api':'https://172.30.91.20:6443','cluster':'vmetal-lab','spire_address':'192.168.50.10'})
        return self.plan()

    def test_mixed_tailscale_baremetal_plan_uses_native_artifacts(self):
        plan = self.mixed()
        self.assertEqual(plan['manager']['facts']['arch'], 'amd64')
        self.assertEqual(plan['workers'][0]['kind'], 'baremetal')
        self.assertEqual(plan['artifacts'], {'amd64':{'builder':'manager'}, 'arm64':{'builder':'worker-0'}})
        self.assertEqual(cluster.artifact_builder(plan, plan['workers'][0]), plan['workers'][0])

    def test_foreign_native_builder_capacity_is_required(self):
        self.mixed()
        self.machines['worker']['available_memory'] = 5 * 2**30
        with self.assertRaisesRegex(ValueError, 'native artifact builder'):
            self.plan()

    def test_existing_mode_does_not_require_sudo_or_build_resources(self):
        self.mixed(existing=True)
        self.machines['manager'].update(memory=2**30, available_memory=100*2**20, containers=['existing-management'], unknown=['private-path'])
        plan = self.plan()
        self.assertEqual(plan['execution'], 'observe-existing')
        self.assertEqual(plan['changes'], [])
        self.assertEqual(plan['artifacts'], {})
        with patch.object(audit, 'audit', return_value={'checks_passed':True,'ready':None}), \
                patch.object(cluster,'upload_runtime') as upload, patch.object(cluster,'phase') as phase:
            cluster.apply(plan,self.state)
        upload.assert_not_called()
        phase.assert_not_called()

    def test_existing_options_cannot_authorize_reinstallation(self):
        plan = self.mixed(existing=True)
        self.config['mode'] = 'create'
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(plan['changes'], [])

    def test_unknown_allocations_cannot_be_treated_as_free_baremetal(self):
        self.mixed()
        self.machines['worker']['allocated'] = None
        with self.assertRaisesRegex(ValueError, 'allocation'):
            self.plan()

    def test_baremetal_runtime_incompatibility_is_blocked(self):
        self.mixed()
        self.machines['worker']['containerd_version'] = 'containerd 1.7.0'
        with self.assertRaisesRegex(ValueError, 'containerd 2'):
            self.plan()

    def test_tailscale_address_must_be_on_tailscale_interface(self):
        self.mixed()
        self.machines['worker']['addresses'][0]['ifname'] = 'eth1'
        with self.assertRaisesRegex(ValueError, 'Tailscale'):
            self.plan()

    def test_installed_tailscale_without_device_authentication_blocks_create(self):
        self.mixed()
        for state in ('NeedsLogin', 'Stopped', None):
            with self.subTest(state=state):
                self.machines['manager']['tailscale_state'] = state
                with self.assertRaisesRegex(ValueError, 'authenticated'):
                    self.plan()

    def test_pending_route_approval_is_resumable_but_not_unknown_installer(self):
        def pending(): raise cluster.ExternalGate('route approval pending')
        with self.assertRaises(cluster.ExternalGate):
            cluster.run_step(self.state,'tailscale-worker-0',pending)
        self.assertEqual(json.loads((self.state/'journal.json').read_text())['tailscale-worker-0']['state'],'blocked')
        done=[]
        cluster.run_step(self.state,'tailscale-worker-0',lambda: done.append(True))
        self.assertEqual(done,[True])

    def test_mixed_pipeline_preserves_before_foreign_build_and_never_calls_vm_prep(self):
        plan=self.mixed(); actions=[]
        result=json.dumps({'ready':True,'plan_digest':cluster.digest(plan)}).encode()
        with patch.object(cluster,'probe',side_effect=lambda alias:copy.deepcopy(self.machines[alias])), \
                patch.object(cluster,'upload_runtime'),patch.object(cluster,'transfer'),patch.object(cluster,'transfer_artifacts'), \
                patch.object(cluster,'phase',side_effect=lambda node,plan,name,log:actions.append((node['ssh'],name))), \
                patch.object(cluster,'remote',return_value=result):
            cluster.apply(plan,self.state)
        self.assertLess(actions.index(('worker','protect')),actions.index(('worker','node-assets')))
        self.assertLess(actions.index(('worker','tailscale-worker')),actions.index(('worker','worker')))
        self.assertIn(('worker','check-protected'),actions)
        self.assertNotIn(('manager','node-assets'),actions)

    def test_readonly_audit_reports_unverified_pod_network_and_detects_workload_drift(self):
        plan=self.mixed(existing=True)
        snapshot={'checked_at':1,'api':plan['existing']['api'],'bindings':[], 'public_ca':'public',
                  'coverage':{'new_pod_http':False,'service_dns_http':False},
                  'protected':{'ovn_mtu':'1180','hosts':[1],'nodes':[1,2],'workloads':[{'uid':'original'}],'pods':[1]}}
        manager_path={'agent_tcp':True,'kubelet_tcp':True,'peer_route':[{'dev':'tailscale0'}],'pod_mtu_budget':1180}
        worker_path={'api_tls':True,'spire_tcp':True,'api_route':[{'dev':'tailscale0'}],'pod_mtu_budget':1180}
        for changed in (False,True):
            after=copy.deepcopy(snapshot)
            if changed: after['protected']['workloads'][0]['uid']='replacement'
            with self.subTest(changed=changed), \
                    patch.object(cluster,'probe',side_effect=lambda alias,**kwargs:copy.deepcopy(self.machines[alias])), \
                    patch.object(audit,'collect',side_effect=[snapshot,after]), \
                    patch.object(audit,'network_probe',side_effect=[manager_path,worker_path]):
                result=audit.audit(plan,self.state)
            self.assertEqual(result['checks_passed'],not changed)
            self.assertIsNone(result['ready'])
            self.assertFalse(result['coverage']['new_pod_http'])

    def test_shared_container_and_driver_preservation(self):
        before={'containers':[{'id':'app','pid':42}], 'services':{'docker':'pid=1'},'socket':[1,2],
                'default_route':'default via 10.0.0.1','driver':'original'}
        after=copy.deepcopy(before)
        after['containers'].append({'id':'new-build','pid':50})
        self.assertTrue(preserve.preserved(before,after))
        after['containers'][0]['pid']=99
        self.assertFalse(preserve.preserved(before,after))
        after=copy.deepcopy(before);after['driver']='changed'
        self.assertFalse(preserve.preserved(before,after))

    def test_native_builder_dependency_plan_preserves_installed_packages(self):
        self.assertEqual(apt_guard.check('Inst build-tool (1.0 Ubuntu)\nConf build-tool (1.0 Ubuntu)\n'), ['build-tool'])
        for transaction in ('Remv docker.io [26.1]\n', 'Inst containerd [2.2.0] (2.2.1 Ubuntu)\n',
                            'Conf docker.io (26.1 Ubuntu)\n'):
            with self.subTest(transaction=transaction), self.assertRaises(ValueError):
                apt_guard.check(transaction)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.host = {'metadata': {'name': 'host-a', 'generation': 1},
                     'spec': {'machineID': 'physical', 'nodeIP': '192.168.50.20'},
                     'status': {'discoveryReady': True, 'observedGeneration': 1,
                                'claim': {'name': 'worker', 'namespace': 'nvke-system', 'uid': 'infra'}}}
        self.machine = {'metadata': {'name': 'worker', 'namespace': 'nvke-system', 'uid': 'infra',
                                    'ownerReferences': [{'apiVersion': 'cluster.x-k8s.io/v1beta2',
                                                        'kind': 'Machine', 'name': 'parent', 'uid': 'capi'}]},
                        'spec': {'hostRef': 'host-a', 'providerID': 'vmetal://infra'}}
        self.parent = {'metadata': {'name': 'parent', 'uid': 'capi'},
                       'spec': {'infrastructureRef': {'apiGroup': 'infrastructure.cluster.x-k8s.io',
                                                     'kind': 'VMetalMachine', 'name': 'worker'}},
                       'status': {'nodeRef': {'name': 'worker', 'uid': 'node-uid'}}}
        self.node = {'metadata': {'name': 'worker', 'uid': 'node-uid'},
                     'spec': {'providerID': 'vmetal://infra'},
                     'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                                'addresses': [{'type': 'InternalIP', 'address': '192.168.50.20'}]}}

    def check(self):
        return verify.validate_binding(self.host, self.machine, self.parent, self.node,
                                       '192.168.50.20', 'physical')

    def test_complete_identity_chain_is_accepted(self):
        self.assertEqual(self.check()['provider_id'], 'vmetal://infra')

    def test_ready_node_with_wrong_allocation_is_rejected(self):
        self.node['spec']['providerID'] = 'vmetal://unrelated'
        with self.assertRaisesRegex(ValueError, 'providerID'):
            self.check()

    def test_stale_discovery_is_rejected(self):
        self.host['metadata']['generation'] = 2
        with self.assertRaisesRegex(ValueError, 'generation'):
            self.check()

    def test_same_name_different_uid_is_rejected(self):
        self.node['metadata']['uid'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'nodeRef'):
            self.check()

    def test_name_only_capi_node_reference_keeps_provider_identity_checks(self):
        self.parent['status']['nodeRef'].pop('uid')
        result = self.check()
        self.assertFalse(result['node_ref_uid_verified'])
        self.assertEqual(result['node_uid'], 'node-uid')
        self.node['spec']['providerID'] = 'vmetal://wrong-allocation'
        with self.assertRaisesRegex(ValueError, 'providerID'):
            self.check()

    def test_wrong_host_claim_is_rejected(self):
        self.host['status']['claim']['uid'] = 'other-allocation'
        with self.assertRaisesRegex(ValueError, 'claim'):
            self.check()

    def test_not_ready_node_is_rejected(self):
        self.node['status']['conditions'][0]['status'] = 'False'
        with self.assertRaisesRegex(ValueError, 'not Ready'):
            self.check()

    def test_ready_node_being_released_is_rejected(self):
        self.machine['metadata']['deletionTimestamp'] = '2026-11-20T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'being deleted'):
            self.check()


if __name__ == '__main__':
    unittest.main(verbosity=2)
