"""Explicit opt-in live test; credentials are read only from stdin, never argv/files.
Input: JSON dict with host, port, username and password. Output: sanitized results.
Tests both upstream protocols and the real local HTTP CONNECT handler with TLS.
This does not validate the Linux network namespace, UDS volumes or browser.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from gateway import Gateway
from transport import fetch_json, parse_proxy


async def main(proxy):
    results = []
    ip = None
    for scheme in ('http', 'socks5h'):
        upstream = {**proxy, 'scheme': scheme}
        for through_gateway in (False, True):
            server = None
            start = time.monotonic()
            try:
                target = upstream
                if through_gateway:
                    g = Gateway(upstream)
                    server = await asyncio.start_server(g.handle_proxy, '127.0.0.1', 0)
                    target = parse_proxy('http://127.0.0.1:' + str(server.sockets[0].getsockname()[1]))
                body = await fetch_json(target, 'api.ipify.org', '/?format=json')
                ip = body['ip']
                results.append(dict(protocol=scheme, layer='gateway+transport' if through_gateway else 'transport',
                                    result='PASS', exit_ip=ip, seconds=round(time.monotonic() - start, 2)))
            except Exception as e:
                results.append(dict(protocol=scheme, layer='gateway+transport' if through_gateway else 'transport',
                                    result='FAIL', error_type=type(e).__name__))
            finally:
                if server:
                    server.close()
                    await server.wait_closed()
    geo = {}
    if ip:
        try:
            obj = await fetch_json({**proxy, 'scheme': 'socks5h'}, 'ipwho.is', '/' + ip)
            if obj.get('success'):
                geo = {k: obj.get(k) for k in ('ip', 'country', 'country_code', 'region', 'city', 'timezone')}
        except Exception as e:
            geo = {'error_type': type(e).__name__}
    return {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'results': results, 'geo_lookup': geo,
            'not_tested': ['Docker build/start (Docker unavailable)', 'Linux UDS mount permissions',
                           'runtime network isolation', 'Firefox policies', 'Claude OAuth', 'VS Code extension'],
            'credentials_included': False}


if __name__ == '__main__':
    data = json.load(sys.stdin)
    print(json.dumps(asyncio.run(main(data)), ensure_ascii=False, indent=2))
