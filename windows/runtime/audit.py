"""Runtime evidence, not an anonymity or account-risk score."""
import json
import os
import socket
import urllib.request
from pathlib import Path


def main():
    checks = []
    def record(name, passed, detail):
        checks.append(dict(check=name, result='PASS' if passed else 'FAIL', detail=detail))
    interfaces = sorted(p.name for p in Path('/sys/class/net').iterdir())
    record('外部网卡', interfaces == ['lo'], ', '.join(interfaces))
    record('非 root', os.getuid() != 0, f'uid={os.getuid()}')
    resolver = Path('/etc/resolv.conf').read_text()
    record('DNS 配置', 'nameserver 127.0.0.1' in resolver, resolver.strip())
    for host, port in [('1.1.1.1', 443), ('2606:4700:4700::1111', 443)]:
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.close()
            record('直连 ' + host, False, 'unexpected successful connection')
        except OSError as e:
            record('直连 ' + host, True, type(e).__name__)
    for family, host in [(socket.AF_INET, '1.1.1.1'), (socket.AF_INET6, '2606:4700:4700::1111')]:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.sendto(b'ccbox-route-probe', (host, 53))
            record('UDP 出站 ' + host, False, 'sendto unexpectedly succeeded')
        except OSError as e:
            record('UDP 出站 ' + host, True, type(e).__name__)
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:18080'}))
        with op.open('https://api.ipify.org?format=json', timeout=30) as r:
            ip = json.load(r)['ip']
        record('代理出口', True, ip)
    except Exception as e:
        record('代理出口', False, type(e).__name__)
    checks.append(dict(check='浏览器 WebRTC / 语言 / 时区', result='MANUAL',
                       detail='打开沙箱浏览器首页核对；系统配置不等于浏览器实测'))
    print(json.dumps(checks, ensure_ascii=False))


if __name__ == '__main__':
    main()
