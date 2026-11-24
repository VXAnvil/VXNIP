"""Real kernel regression in disposable Linux namespaces; no physical machines."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/vxnip-cluster/scripts'))
import egress

SERVER = r'''
import http.server,socket,ssl,struct,sys,threading
def dns():
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('1.1.1.1',53))
 while True:
  packet,peer=s.recvfrom(4096);end=12
  while packet[end]:end+=packet[end]+1
  end+=1;typ=struct.unpack('!H',packet[end:end+2])[0]
  answer=b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x05\x00\x04'+socket.inet_aton('1.1.1.1') if typ==1 else b''
  s.sendto(packet[:2]+b'\x81\x80'+struct.pack('!HHHH',1,bool(answer),0,0)+packet[12:end+4]+answer,peer)
threading.Thread(target=dns,daemon=True).start()
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'fixture egress OK')
 def log_message(self,*args):pass
httpd=http.server.HTTPServer(('1.1.1.1',443),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(sys.argv[1],sys.argv[2])
httpd.socket=context.wrap_socket(httpd.socket,server_side=True);httpd.serve_forever()
'''


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0, 'requires a disposable root Linux CI runner')
class KernelEgressTests(unittest.TestCase):
    def setUp(self):
        for command in ('ip', 'iptables', 'openssl'):
            self.assertIsNotNone(shutil.which(command), command + ' required in CI')
        self.temp = tempfile.TemporaryDirectory(prefix='vxnip-egress-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.prefix = 'vxeg-' + str(os.getpid())
        self.namespaces = []
        self.addCleanup(self.cleanup_namespaces)
        for suffix in ('pod', 'router', 'wan'):
            name = self.prefix + '-' + suffix
            self.command(['ip', 'netns', 'add', name])
            self.namespaces.append(name)
            self.command(['ip', '-n', name, 'link', 'set', 'lo', 'up'])
        self.pod, self.router, self.wan = self.namespaces
        self.link('pa', self.pod, 'eth0', 'ra', self.router, 'ovn-k8s-mp0')
        self.link('rb', self.router, 'uplink0', 'wa', self.wan, 'wan0')
        for ns, interface, address in ((self.pod, 'eth0', '10.230.5.10/24'),
                (self.router, 'ovn-k8s-mp0', '10.230.5.2/24'), (self.router, 'uplink0', '192.0.2.1/30'),
                (self.wan, 'wan0', '192.0.2.2/30'), (self.wan, 'lo', '1.1.1.1/32')):
            self.command(['ip', '-n', ns, 'addr', 'add', address, 'dev', interface])
        self.command(['ip', '-n', self.pod, 'route', 'add', 'default', 'via', '10.230.5.2'])
        self.command(['ip', '-n', self.router, 'route', 'add', 'default', 'via', '192.0.2.2'])
        self.in_router(['sysctl', '-w', 'net.ipv4.ip_forward=1'])
        self.in_router(['iptables', '-P', 'FORWARD', 'DROP'])
        self.in_router(['iptables', '-N', 'DOCKER-USER'])
        self.in_router(['iptables', '-A', 'FORWARD', '-j', 'DOCKER-USER'])
        # Existing OVN-equivalent SNAT is deliberately installed before the fix.
        self.in_router(['iptables', '-t', 'nat', '-A', 'POSTROUTING', '-s', '10.230.5.0/24', '-j', 'MASQUERADE'])
        self.resolver = Path('/etc/netns') / self.pod
        self.resolver.mkdir(mode=0o755, parents=True)
        self.addCleanup(shutil.rmtree, self.resolver)
        (self.resolver / 'resolv.conf').write_text('nameserver 1.1.1.1\noptions timeout:1 attempts:1\n')
        self.cert, self.key = self.directory / 'cert.pem', self.directory / 'key.pem'
        self.command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(self.key),
                      '-out', str(self.cert), '-days', '1', '-subj', '/CN=egress.example.test',
                      '-addext', 'subjectAltName=DNS:egress.example.test,IP:1.1.1.1'])
        self.server = subprocess.Popen(['ip', 'netns', 'exec', self.wan, sys.executable, '-c', SERVER,
                                        str(self.cert), str(self.key)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_server)
        for _ in range(50):
            ready = subprocess.run(['ip', 'netns', 'exec', self.wan, sys.executable, '-c',
                                    "import socket;socket.create_connection(('1.1.1.1',443),.2).close()"],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        else:
            self.fail('Local TLS fixture did not start')

    def command(self, args):
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=30).strip()

    def in_router(self, args):
        return self.command(['ip', 'netns', 'exec', self.router, *args])

    def link(self, left, left_ns, left_name, right, right_ns, right_name):
        left, right = 've' + str(os.getpid()) + left, 've' + str(os.getpid()) + right
        self.command(['ip', 'link', 'add', left, 'type', 'veth', 'peer', 'name', right])
        for interface, ns, name in ((left, left_ns, left_name), (right, right_ns, right_name)):
            self.command(['ip', 'link', 'set', interface, 'netns', ns])
            self.command(['ip', '-n', ns, 'link', 'set', interface, 'name', name])
            self.command(['ip', '-n', ns, 'link', 'set', name, 'up'])

    def stop_server(self):
        self.server.terminate()
        self.server.wait(timeout=5)

    def cleanup_namespaces(self):
        for namespace in reversed(self.namespaces):
            subprocess.run(['ip', 'netns', 'del', namespace], capture_output=True, check=True)

    def probe(self, trusted=True):
        value = {'dns_name': 'egress.example.test', 'tcp_ip': '1.1.1.1', 'tcp_port': 443,
                 'https_url': 'https://egress.example.test/'}
        env = dict(os.environ, SSL_CERT_FILE=str(self.cert) if trusted else str(self.directory / 'untrusted.pem'))
        result = subprocess.run(['ip', 'netns', 'exec', self.pod, sys.executable,
                                 str(ROOT / 'skills/vxnip-cluster/scripts/egress_probe.py'), json.dumps(value)],
                                env=env, capture_output=True, text=True, timeout=40)
        return result.returncode, json.loads(result.stdout)

    def test_drop_reproduction_scoped_repair_tls_and_recovery(self):
        before = subprocess.run(['ip', 'netns', 'exec', self.pod, sys.executable, '-c',
                                 "import socket;socket.create_connection(('1.1.1.1',443),.3)"], capture_output=True)
        self.assertNotEqual(before.returncode, 0, 'fixture must reproduce the broken Pod egress first')
        nat = self.in_router(['iptables', '-t', 'nat', '-S'])
        routes = self.in_router(['ip', '-4', 'route'])
        plan = {'site': 'vxnip-fixture', 'networks': {'pod': '10.230.0.0/16'}}
        which = lambda name: name if name == 'iptables' else None
        current = egress.ensure(plan, call=self.in_router, which=which)
        self.assertEqual(self.probe(), (0, {'passed': True, 'targets': {
            'dns_name': 'egress.example.test', 'tcp_ip': '1.1.1.1', 'tcp_port': 443,
            'https_url': 'https://egress.example.test/'}, 'checks': {
                name: {'passed': True} for name in ('external_dns', 'numeric_tcp', 'https_verified')}}))
        egress.ensure(plan, previous=current, call=self.in_router, which=which)
        self.assertEqual(self.in_router(['iptables', '-S', 'DOCKER-USER']).count('-A DOCKER-USER'), 2)
        self.assertTrue(self.in_router(['iptables', '-S', 'FORWARD']).startswith('-P FORWARD DROP'))
        self.assertEqual(self.in_router(['iptables', '-t', 'nat', '-S']), nat)
        self.assertEqual(self.in_router(['ip', '-4', 'route']), routes)
        rc, untrusted = self.probe(trusted=False)
        self.assertEqual(rc, 1)
        self.assertTrue(untrusted['checks']['external_dns']['passed'])
        self.assertTrue(untrusted['checks']['numeric_tcp']['passed'])
        self.assertFalse(untrusted['checks']['https_verified']['passed'])
        self.in_router(['iptables', '-F', 'DOCKER-USER'])
        with self.assertRaises(subprocess.CalledProcessError):
            egress.ensure(plan, check=True, call=self.in_router, which=which)
        plan['mode'] = 'create'
        identity = hashlib.sha256(('vxnip:' + Path('/etc/machine-id').read_text().strip()).encode()).hexdigest()
        settings = {'root': str(self.directory), 'plan': plan,
                    'node': {'kind': 'baremetal', 'facts': {'machine_id': identity}}}
        settings_file = self.directory / 'settings.json'
        settings_file.write_text(json.dumps(settings))
        (self.directory / 'plan.digest').write_text(
            hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        monitor = subprocess.Popen(['ip', 'netns', 'exec', self.router, sys.executable,
            str(ROOT / 'skills/vxnip-cluster/scripts/egress.py'), 'watch', str(settings_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (monitor.terminate(), monitor.wait(timeout=5)))
        def wait_rules():
            for _ in range(40):
                try:
                    return egress.ensure(plan, check=True, call=self.in_router, which=which)
                except subprocess.CalledProcessError:
                    self.assertIsNone(monitor.poll(), 'egress monitor exited')
                    time.sleep(.2)
            self.fail('Persistent egress monitor did not recover rules')
        current = wait_rules()
        self.in_router(['iptables', '-F', 'DOCKER-USER'])
        current = wait_rules()
        self.in_router(['ip', 'link', 'set', 'uplink0', 'name', 'uplink1'])
        current = wait_rules()
        self.assertEqual(current['uplink'], 'uplink1')
        for _ in range(25):
            if 'uplink0' not in self.in_router(['iptables', '-S', 'DOCKER-USER']):
                break
            time.sleep(.2)
        else:
            self.fail('Exact old uplink rules were not removed')
        self.assertEqual(self.probe()[0], 0)


if __name__ == '__main__':
    if '--require' in sys.argv:
        sys.argv.remove('--require')
        if not sys.platform.startswith('linux') or os.geteuid() != 0:
            raise SystemExit('Required kernel egress regression needs a disposable root Linux runner')
    unittest.main(verbosity=2)
