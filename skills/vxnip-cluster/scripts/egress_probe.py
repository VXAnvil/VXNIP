"""Independent DNS, numeric TCP and certificate-validated HTTPS checks inside a Pod."""
import ipaddress
import json
import socket
import ssl
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen

DEFAULTS = {'dns_name': 'example.com', 'tcp_ip': '1.1.1.1', 'tcp_port': 443,
            'https_url': 'https://example.com/'}


def targets(value=None):
    if value is not None and not isinstance(value, dict) or set(value or {}) - set(DEFAULTS):
        raise ValueError('Unknown egress probe targets')
    result = dict(DEFAULTS, **(value or {}))
    name = result['dns_name']
    if not isinstance(name, str) or not name or len(name) > 253 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in name):
        raise ValueError('Use a DNS hostname for the egress DNS test')
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError('DNS acceptance requires a hostname, not an IP address')
    ipaddress.IPv4Address(result['tcp_ip'])
    if type(result['tcp_port']) is not int or not 1 <= result['tcp_port'] <= 65535:
        raise ValueError('Invalid egress TCP port')
    if not isinstance(result['https_url'], str):
        raise ValueError('Use an HTTPS URL for egress acceptance')
    url = urlsplit(result['https_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Use a credential-free HTTPS URL for egress acceptance')
    return result


def probe(value):
    value = targets(value)
    results = {}
    checks = {
        'external_dns': lambda: bool(socket.getaddrinfo(value['dns_name'], 443, type=socket.SOCK_STREAM)),
        'numeric_tcp': lambda: tcp(value),
        'https_verified': lambda: https(value)}
    socket.setdefaulttimeout(8)
    for name, check in checks.items():
        try:
            if check() is not True:
                raise RuntimeError('Empty/unsuccessful response')
            results[name] = {'passed': True}
        except Exception as error:
            results[name] = {'passed': False, 'error': type(error).__name__ + ': ' + str(error)}
    return {'passed': all(r['passed'] for r in results.values()), 'targets': value, 'checks': results}


def tcp(value):
    with socket.create_connection((value['tcp_ip'], value['tcp_port']), timeout=8):
        return True


def https(value):
    context = ssl.create_default_context()
    with urlopen(value['https_url'], timeout=10, context=context) as response:
        response.read(1024)
        return 200 <= response.status < 400


if __name__ == '__main__':
    result = probe(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {})
    print(json.dumps(result), flush=True)
    sys.exit(0 if result['passed'] else 1)
