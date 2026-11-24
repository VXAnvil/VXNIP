"""Offline behavior tests; never connect to a cluster or physical worker."""
import copy
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/vxnip-cluster/scripts'))
import egress
import egress_check
import egress_probe
import verify


class Firewall:
    def __init__(self):
        self.subnet = '10.230.5.2'
        self.uplink = 'uplink0'
        self.rules = []
        self.calls = []
        self.deny = False
        self.missing = False

    def which(self, name):
        return name if name == 'iptables' else None

    def call(self, args):
        self.calls.append(args)
        if args[:5] == ['ip', '-j', '-4', 'address', 'show']:
            return json.dumps([{'addr_info': [{'family': 'inet', 'local': self.subnet, 'prefixlen': 24}]}])
        if args[:5] == ['ip', '-j', '-4', 'route', 'get']:
            return json.dumps([{'dev': self.uplink}])
        if args[-1] == '--version':
            return 'iptables v1.8.10 (nf_tables)'
        if '-S' in args:
            if args[-1] == 'FORWARD':
                return '-P FORWARD DROP\n-A FORWARD -j DOCKER-USER'
            return '-A DOCKER-USER -j DROP' if self.deny else '\n'.join('-A DOCKER-USER ' + ' '.join(r) for r in self.rules)
        op = next(a for a in args if a in ('-C', '-I', '-D'))
        rule = args[args.index('DOCKER-USER') + 1:]
        if op == '-I':
            assert rule.pop(0) == '1'
            self.rules.insert(0, rule)
        elif op == '-D':
            self.rules.remove(rule)
        elif rule not in self.rules or self.missing:
            raise subprocess.CalledProcessError(1, args)
        return ''


class ScopedForwardingTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'site': 'vxnip-test', 'networks': {'pod': '10.230.0.0/16'}}
        self.firewall = Firewall()

    def ensure(self, **kwargs):
        return egress.ensure(self.plan, call=self.firewall.call, which=self.firewall.which, **kwargs)

    def test_forward_drop_is_fixed_without_policy_nat_or_route_changes(self):
        result = self.ensure()
        self.assertEqual(len(self.firewall.rules), 2)
        self.assertEqual(result['pod_subnet'], '10.230.5.0/24')
        out, reply = result['rules']
        self.assertEqual(out[out.index('-s') + 1], '10.230.5.0/24')
        self.assertEqual(out[out.index('-i') + 1], 'ovn-k8s-mp0')
        self.assertEqual(out[out.index('-o') + 1], 'uplink0')
        self.assertEqual(reply[reply.index('--ctstate') + 1], 'ESTABLISHED,RELATED')
        self.assertFalse(any(a in ('-P', '-F', 'nat', 'replace') for args in self.firewall.calls for a in args))
        self.assertTrue(all(args[args.index('-I') + 1] == 'DOCKER-USER' for args in self.firewall.calls if '-I' in args))

    def test_repeated_recovery_is_idempotent_and_restores_removed_rules(self):
        previous = self.ensure()
        self.ensure(previous=previous)
        self.assertEqual(len(self.firewall.rules), 2)
        self.firewall.rules.clear()
        with self.assertRaises(subprocess.CalledProcessError):
            self.ensure(check=True)
        self.assertEqual(self.firewall.rules, [])
        self.ensure(previous=previous)
        self.assertEqual(len(self.firewall.rules), 2)

    def test_uplink_and_subnet_changes_remove_only_exact_owned_old_rules(self):
        first = self.ensure()
        unrelated = ['-s', '192.0.2.0/24', '-j', 'ACCEPT']
        self.firewall.rules.append(unrelated)
        self.firewall.uplink = 'uplink1'
        self.firewall.subnet = '10.230.6.2'
        second = self.ensure(previous=first)
        self.assertIn(unrelated, self.firewall.rules)
        self.assertEqual(len(self.firewall.rules), 3)
        self.assertTrue(all(rule not in self.firewall.rules for rule in first['rules']))
        self.assertEqual(second['pod_subnet'], '10.230.6.0/24')

    def test_unknown_admin_deny_policy_is_not_bypassed(self):
        self.firewall.deny = True
        with self.assertRaisesRegex(ValueError, 'deny/custom'):
            self.ensure()
        self.assertEqual(self.firewall.rules, [])

    def test_unknown_subnet_or_looping_uplink_does_not_change_rules(self):
        self.firewall.subnet = '192.0.2.2'
        with self.assertRaises(RuntimeError):
            self.ensure()
        self.firewall.subnet = '10.230.5.2'
        for name in ('ovn-k8s-mp0', 'lo', 'br-ex', 'iface;command'):
            self.firewall.uplink = name
            with self.assertRaises(RuntimeError):
                self.ensure()
        self.assertEqual(self.firewall.rules, [])


class PodProbeTests(unittest.TestCase):
    def test_dns_tcp_and_https_are_independent_and_https_validates_certificates(self):
        context = ssl.create_default_context()
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(egress_probe.socket, 'getaddrinfo', return_value=[('resolved',)]), \
                patch.object(egress_probe.socket, 'create_connection') as tcp, \
                patch.object(egress_probe.ssl, 'create_default_context', return_value=context), \
                patch.object(egress_probe, 'urlopen', return_value=response) as https:
            result = egress_probe.probe({})
        self.assertTrue(result['passed'])
        self.assertEqual(tcp.call_args.args[0], ('1.1.1.1', 443))
        self.assertEqual(https.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https.call_args.kwargs['context'].check_hostname)

    def test_dns_failure_cannot_be_hidden_by_successful_tcp(self):
        with patch.object(egress_probe.socket, 'getaddrinfo', side_effect=socket.gaierror('no DNS')), \
                patch.object(egress_probe, 'tcp', return_value=True), patch.object(egress_probe, 'https', return_value=True):
            result = egress_probe.probe({})
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['external_dns']['passed'])
        self.assertTrue(result['checks']['numeric_tcp']['passed'])

    def test_bad_tls_certificate_is_not_a_successful_https_probe(self):
        with patch.object(egress_probe.socket, 'getaddrinfo', return_value=[1]), \
                patch.object(egress_probe, 'tcp', return_value=True), \
                patch.object(egress_probe, 'https', side_effect=ssl.SSLCertVerificationError('untrusted')):
            result = egress_probe.probe({})
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['https_verified']['passed'])

    def test_custom_targets_are_validated(self):
        value = {'dns_name': 'egress.example.test', 'tcp_ip': '192.0.2.2', 'tcp_port': 8443,
                 'https_url': 'https://egress.example.test:8443/health'}
        self.assertEqual(egress_probe.targets(value), value)
        for bad in ({'https_url': 'http://example.com'}, {'https_url': 'https://user:secret@example.com/'},
                    {'dns_name': '1.1.1.1'}, {'tcp_port': True}, {'unknown': True}, [], False):
            with self.assertRaises(ValueError):
                egress_probe.targets(bad)


class FreshAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bindings = [{'node': n, 'node_uid': n + '-uid', 'provider_id': 'vxnip://' + n} for n in ('cp', 'worker-a', 'worker-b')]
        self.nodes = [{'metadata': {'name': b['node'], 'uid': b['node_uid'], 'labels': {'kubernetes.io/hostname': b['node']}},
                       'spec': {'providerID': b['provider_id']}} for b in self.bindings]
        self.pods = {}
        self.failed = False
        self.deleted = False
        self.namespace_uid = 'own-uid'

    def command(self, args, data=None):
        if 'nodes' in args:
            return json.dumps({'items': self.nodes})
        if 'node' in args:
            return json.dumps(next(n for n in self.nodes if n['metadata']['name'] == args[args.index('node') + 1]))
        if 'create' in args and '-f' in args:
            obj = json.loads(data)
            if obj['kind'] == 'Namespace':
                return json.dumps({'metadata': {'name': 'own-namespace', 'uid': 'own-uid'}})
            self.pods[obj['metadata']['name']] = obj
            return 'created'
        if 'configmap' in args:
            return 'created'
        if 'delete' in args:
            self.deleted = True; return ''
        if 'namespace' in args:
            return json.dumps({'metadata': {'uid': self.namespace_uid}})
        if 'logs' in args:
            return json.dumps({'passed': not self.failed, 'checks': {
                name: {'passed': not (self.failed and name == 'external_dns')}
                for name in ('external_dns', 'numeric_tcp', 'https_verified')}})
        if 'pod' in args:
            name = args[args.index('pod') + 1]
            p = copy.deepcopy(self.pods[name])
            p['metadata']['uid'] = name + '-uid'
            p['spec']['nodeName'] = p['spec']['nodeSelector']['kubernetes.io/hostname']
            p['status'] = {'phase': 'Failed' if self.failed else 'Succeeded', 'containerStatuses': [{'imageID': 'sha256:test'}]}
            return json.dumps(p)
        raise AssertionError(args)

    def verify(self):
        with patch.object(egress_check, 'run', side_effect=self.command):
            return egress_check.verify({'plan': {'egress': {}}, 'root': str(self.root)}, self.bindings)

    def test_every_node_gets_a_scheduler_selected_non_gpu_probe_and_exact_cleanup(self):
        result = self.verify()
        self.assertTrue(result['passed']); self.assertTrue(self.deleted)
        self.assertEqual({r['node'] for r in result['results']}, {'cp', 'worker-a', 'worker-b'})
        for pod in self.pods.values():
            self.assertNotIn('nodeName', pod['spec'])
            self.assertFalse(pod['spec']['automountServiceAccountToken'])
            self.assertNotIn('nvidia.com/gpu', pod['spec']['containers'][0]['resources']['limits'])
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in (self.root / 'verification-logs').iterdir()))

    def test_failed_external_dns_prevents_acceptance_and_cleans_probes(self):
        self.failed = True
        with self.assertRaisesRegex(ValueError, 'external DNS'):
            self.verify()
        self.assertTrue(self.deleted)
        self.assertFalse(any(json.loads(p.read_text()).get('passed') for p in (self.root / 'verification-logs').iterdir()))

    def test_changed_namespace_uid_is_never_deleted(self):
        self.namespace_uid = 'someone-else'
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.verify()
        self.assertFalse(self.deleted)

    def test_replaced_node_is_rejected_before_probe_creation(self):
        self.nodes[1]['metadata']['uid'] = 'replaced'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.verify()
        self.assertEqual(self.pods, {})

    def test_final_ready_result_cannot_be_written_when_egress_fails(self):
        settings = {'plan': {'site': 'vxnip-test', 'networks': {'docker': '172.30.91.0/24'}},
                    'root': str(self.root)}
        with patch.object(verify, 'wait_bindings', return_value=self.bindings), \
                patch.object(verify, 'run_probe'), \
                patch.object(egress_check, 'verify', side_effect=ValueError('external DNS failed')):
            with self.assertRaisesRegex(ValueError, 'external DNS'):
                verify.main(settings)
        self.assertFalse((self.root / 'result.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
