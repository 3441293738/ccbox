import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
from transport import parse_proxy, authority, connect_proxy, header
from gateway import Gateway, parse_request
from app.controller import DEFAULT, make_compose, validate, Controller
from app import vault


class ParsingTests(unittest.TestCase):
    def test_percent_encoded_credentials(self):
        p = parse_proxy('socks5h://SERIAL:p%40ss%3Aword@HOST:1080')
        self.assertEqual(p['password'], 'p@ss:word')
        self.assertEqual(p['host'], 'host')

    def test_ipv6_proxy(self):
        self.assertEqual(parse_proxy('http://[::1]:8080')['host'], '::1')
        self.assertEqual(authority('::1', 443), '[::1]:443')

    def test_invalid_proxy(self):
        for url in ['HOST:1080', 'ftp://HOST', 'http://HOST/path', 'http://HOST:99999', 'http://HOST:0',
                    'http://u%0d%0a:p@HOST', 'http://HOST?x=1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_proxy(url)

    def test_target_injection(self):
        for host in ['bad\r\nHeader: value', 'a/b', 'a@b', 'a b', 'bad\x00host', '']:
            with self.assertRaises(ValueError):
                authority(host, 443)

    def test_ambiguous_headers(self):
        for body in ['Host: x\r\nHost: y', 'Content-Length: 0\r\nTransfer-Encoding: chunked',
                     'Origin: a\r\nOrigin: b', 'Cookie: a\r\nCookie: b']:
            with self.assertRaises(ValueError):
                parse_request(('GET / HTTP/1.1\r\n' + body + '\r\n\r\n').encode())


class DesktopAuthTests(unittest.TestCase):
    def setUp(self):
        self.g = Gateway({}, 'TOKEN', 6080)
        self.host = [('Host', '127.0.0.1:6080')]

    def test_requires_token(self):
        self.assertEqual(self.g.desktop_auth('/vnc.html', self.host), (False, False))
        self.assertEqual(self.g.desktop_auth('/vnc.html?access=TOKEN', self.host), (True, True))

    def test_cookie(self):
        self.assertEqual(self.g.desktop_auth('/websockify', self.host + [('Cookie', 'ccbox_session_6080=TOKEN')]), (True, False))

    def test_cross_origin_and_rebinding(self):
        for h in [[('Host', 'evil.invalid:6080')], self.host + [('Origin', 'https://evil.invalid')]]:
            self.assertFalse(self.g.desktop_auth('/vnc.html?access=TOKEN', h)[0])

    def test_empty_secret_rejected(self):
        self.assertFalse(Gateway({}).desktop_auth('/', self.host + [('Cookie', 'ccbox_session=""')])[0])

    def test_external_redirect_target(self):
        self.assertFalse(self.g.desktop_auth('//evil.invalid?access=TOKEN', self.host)[0])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.writers = []
        self.observed = []

    async def asyncTearDown(self):
        for w in self.writers:
            w.close()
        for s in self.servers:
            s.close()
            await s.wait_closed()
        await asyncio.sleep(.01)

    async def server(self, handler):
        async def tracked(r, w):
            self.writers.append(w)
            try:
                await handler(r, w)
            except (ConnectionError, asyncio.IncompleteReadError):
                w.close()
        s = await asyncio.start_server(tracked, '127.0.0.1', 0)
        self.servers.append(s)
        return s.sockets[0].getsockname()[1]

    async def http_upstream(self, r, w):
        self.observed.append(await header(r))
        w.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        await w.drain()
        while data := await r.read(65536):
            w.write(data)
            await w.drain()

    async def test_http_remote_dns_and_auth(self):
        port = await self.server(self.http_upstream)
        p = parse_proxy(f'http://SERIAL:TOKEN@127.0.0.1:{port}')
        r, w = await connect_proxy(p, 'never-resolve-locally.invalid', 443)
        self.writers.append(w)
        w.write(b'hello')
        await w.drain()
        self.assertEqual(await r.readexactly(5), b'hello')
        self.assertIn(b'CONNECT never-resolve-locally.invalid:443', self.observed[0])
        self.assertIn(base64.b64encode(b'SERIAL:TOKEN'), self.observed[0])

    async def test_http_rejection(self):
        async def reject(r, w):
            await header(r)
            w.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n\r\n')
            await w.drain()
            w.close()
        port = await self.server(reject)
        with self.assertRaises(ConnectionError):
            await connect_proxy(parse_proxy(f'http://127.0.0.1:{port}'), 'example.invalid', 443)

    async def test_no_direct_fallback(self):
        with patch('transport.asyncio.open_connection', new_callable=AsyncMock) as fn:
            fn.side_effect = OSError('offline')
            with self.assertRaises(OSError):
                await connect_proxy(parse_proxy('socks5h://HOST:1080'), 'TARGET', 443)
            self.assertEqual(fn.call_count, 1)
            self.assertEqual(fn.call_args.args[:2], ('host', 1080))

    async def socks_upstream(self, r, w):
        self.observed.append(await r.readexactly(3))
        w.write(b'\x05\x02')
        await w.drain()
        self.assertEqual(await r.readexactly(1), b'\x01')
        n = (await r.readexactly(1))[0]
        user = await r.readexactly(n)
        n = (await r.readexactly(1))[0]
        password = await r.readexactly(n)
        self.observed.append((user, password))
        w.write(b'\x01\x00')
        await w.drain()
        self.observed.append(await r.readexactly(4))
        n = (await r.readexactly(1))[0]
        self.observed.append(await r.readexactly(n))
        self.observed.append(await r.readexactly(2))
        # Deliberately fragmented upstream reply.
        for chunk in [b'\x05\x00', b'\x00\x01\x00\x00', b'\x00\x00\x00\x00']:
            w.write(chunk)
            await w.drain()
            await asyncio.sleep(.01)
        w.write(b'OK')
        await w.drain()

    async def test_socks5_auth_remote_dns_fragmentation(self):
        port = await self.server(self.socks_upstream)
        r, w = await connect_proxy(parse_proxy(f'socks5h://SERIAL:TOKEN@127.0.0.1:{port}'), 'example.invalid', 443)
        self.writers.append(w)
        self.assertEqual(await r.readexactly(2), b'OK')
        self.assertEqual(self.observed[0], b'\x05\x01\x02')
        self.assertEqual(self.observed[1], (b'SERIAL', b'TOKEN'))
        self.assertEqual(self.observed[2], b'\x05\x01\x00\x03')
        self.assertEqual(self.observed[3], b'example.invalid')
        self.assertEqual(self.observed[4], struct.pack('!H', 443))

    async def test_socks_method_downgrade_rejected(self):
        async def bad(r, w):
            await r.readexactly(3)
            w.write(b'\x05\x00')
            await w.drain()
        port = await self.server(bad)
        with self.assertRaises(ConnectionError):
            await connect_proxy(parse_proxy(f'socks5h://SERIAL:TOKEN@127.0.0.1:{port}'), 'example.invalid', 443)

    async def test_gateway_connect_end_to_end(self):
        upstream_port = await self.server(self.http_upstream)
        g = Gateway(parse_proxy(f'http://127.0.0.1:{upstream_port}'))
        local_port = await self.server(g.handle_proxy)
        r, w = await asyncio.open_connection('127.0.0.1', local_port)
        self.writers.append(w)
        w.write(b'CONNECT target.invalid:443 HTTP/1.1\r\nHost: target.invalid:443\r\n\r\n')
        await w.drain()
        self.assertIn(b'200 Connection Established', await header(r))
        w.write(b'PAYLOAD')
        await w.drain()
        self.assertEqual(await r.readexactly(7), b'PAYLOAD')

    async def test_gateway_http_removes_client_proxy_auth(self):
        received = []
        async def upstream(r, w):
            await header(r)
            w.write(b'HTTP/1.1 200 OK\r\n\r\n')
            await w.drain()
            received.append(await header(r))
            w.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK')
            await w.drain()
            w.close()
        port = await self.server(upstream)
        g = Gateway(parse_proxy(f'http://127.0.0.1:{port}'))
        local_port = await self.server(g.handle_proxy)
        r, w = await asyncio.open_connection('127.0.0.1', local_port)
        self.writers.append(w)
        w.write(b'GET http://target.invalid/a?b=1 HTTP/1.1\r\nHost: other.invalid\r\nProxy-Authorization: SHOULD_NOT_FORWARD\r\n\r\n')
        await w.drain()
        self.assertIn(b'200', await header(r))
        self.assertIn(b'GET /a?b=1 HTTP/1.1', received[0])
        self.assertIn(b'Host: target.invalid', received[0])
        self.assertNotIn(b'SHOULD_NOT_FORWARD', received[0])

    async def test_gateway_failure_is_generic(self):
        with patch('gateway.connect_proxy', new_callable=AsyncMock) as fn:
            fn.side_effect = RuntimeError('TOKEN must not appear in responses')
            g = Gateway({})
            port = await self.server(g.handle_proxy)
            r, w = await asyncio.open_connection('127.0.0.1', port)
            self.writers.append(w)
            w.write(b'CONNECT example.invalid:443 HTTP/1.1\r\n\r\n')
            await w.drain()
            response = await r.read()
            self.assertIn(b'502', response)
            self.assertNotIn(b'TOKEN', response)

    async def test_desktop_link_cookie_and_redirect(self):
        g = Gateway({}, 'TOKEN')
        port = await self.server(g.handle_desktop)
        r, w = await asyncio.open_connection('127.0.0.1', port)
        self.writers.append(w)
        w.write(b'GET /vnc.html?access=TOKEN&resize=scale HTTP/1.1\r\nHost: 127.0.0.1:6080\r\n\r\n')
        await w.drain()
        response = await r.read()
        self.assertIn(b'303', response)
        self.assertIn(b'Location: /vnc.html?resize=scale', response)
        self.assertIn(b'HttpOnly; SameSite=Strict', response)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.c = {**DEFAULT, 'workspace': str(self.workspace)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_network_boundary_and_secrets(self):
        d = make_compose(ROOT, self.base / 'data', self.c, 'ccbox2-test')
        s = d['services']['sandbox']
        self.assertEqual(s['network_mode'], 'none')
        self.assertTrue(all(v['pull_policy'] == 'never' for v in d['services'].values()))
        self.assertEqual(s['user'], '1000:1000')
        self.assertEqual(s['cap_drop'], ['ALL'])
        self.assertTrue(s['read_only'])
        self.assertNotIn('ports', s)
        self.assertNotIn('networks', s)
        self.assertNotIn('privileged', s)
        self.assertTrue(d['services']['gateway']['ports'][0].startswith('127.0.0.1:'))
        self.assertNotIn('docker.sock', json.dumps(d))
        self.assertNotIn('upstream.json', json.dumps(s))
        self.assertNotIn('password', json.dumps(d))
        self.assertIn('ipc:/ipc:ro', s['volumes'])

    def test_readonly_workspace(self):
        self.c['readonly'] = True
        d = make_compose(ROOT, self.base / 'data', self.c, 'ccbox2-test')
        mounts = [x for x in d['services']['sandbox']['volumes'] if isinstance(x, dict)]
        self.assertTrue(next(x for x in mounts if x['target'] == '/workspace')['read_only'])

    def test_reject_sensitive_roots(self):
        for path in [Path.home(), ROOT, ROOT / 'data', Path(ROOT.anchor)]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate({**self.c, 'workspace': str(path)})

    def test_reject_invalid_settings(self):
        for key, value in [('timezone', 'Made/Up'), ('locale', 'bad'), ('desktop_port', 80),
                           ('claude_version', 'latest; whoami'), ('memory_gb', 0)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**self.c, key: value})

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_roundtrip_no_cleartext(self):
        p = self.base / 'private/credentials.dpapi'
        vault.save(p, {'password': 'LICENSE_BLOB secret', 'user': 'SERIAL'})
        self.assertNotIn(b'LICENSE_BLOB', p.read_bytes())
        self.assertEqual(vault.load(p)['password'], 'LICENSE_BLOB secret')

    def test_stop_preserves_login_volumes(self):
        controller = Controller(self.base)
        controller.data.mkdir()
        controller.file.write_text('{"name":"ccbox2-test"}')
        vault.save(controller.vault_file, {'project': 'ccbox2-test'})
        with patch.object(controller, 'docker', return_value='docker'), patch.object(controller, 'run', return_value='') as fn:
            controller.stop()
            self.assertIn('stop', fn.call_args.args[0])
            self.assertNotIn('down', fn.call_args.args[0])
            self.assertNotIn('-v', fn.call_args.args[0])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_save_prepare_stop_secret_lifecycle(self):
        controller = Controller(self.base / 'bundle')
        runtime = controller.root / 'runtime'
        runtime.mkdir(parents=True)
        (runtime / 'firefox-policies.json').write_bytes((ROOT / 'runtime/firefox-policies.json').read_bytes())
        proxy = parse_proxy('socks5h://SERIAL:LICENSE_BLOB@HOST:1080')
        controller.save(self.c, proxy)
        self.assertNotIn(b'LICENSE_BLOB', controller.vault_file.read_bytes())
        with patch.object(controller, 'docker', return_value='docker'), patch.object(controller, 'run', return_value=''):
            controller.prepare()
            self.assertNotIn('LICENSE_BLOB', controller.file.read_text())
            active = controller.data / 'active/upstream.json'
            self.assertIn('LICENSE_BLOB', active.read_text())
            controller.stop()
            self.assertEqual(active.read_bytes(), b'')
            self.assertTrue(controller.vault_file.exists())

    def test_running_environment_prepare_is_rejected(self):
        controller = Controller(self.base / 'bundle')
        controller.data.mkdir(parents=True)
        controller.file.write_text('{}')
        with patch.object(controller, 'config', return_value=self.c), \
             patch.object(controller, 'credentials', return_value={'project': 'ccbox2-test'}), \
             patch.object(controller, 'docker', return_value='docker'), \
             patch.object(controller, 'run', return_value='a-running-container'):
            with self.assertRaises(RuntimeError):
                controller.prepare()
        self.assertFalse((controller.data / 'active/upstream.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
