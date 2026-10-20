"""Offline TLS regression tests. All test keys are generated in a temp directory."""
import asyncio
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
import transport


class CAContextTests(unittest.TestCase):
    def test_public_bundle_loads_without_system_roots(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'SSL_CERT_FILE': str(Path(tmp)/'missing.pem'), 'SSL_CERT_DIR': str(Path(tmp)/'missing-dir')}):
            result = transport.tls_diagnostics()
        self.assertTrue(result['bundled_ca'])
        self.assertGreaterEqual(result['bundled_ca_count'], 100)
        self.assertTrue(result['certificate_required'])
        self.assertTrue(result['hostname_check'])

    def test_default_system_trust_is_retained(self):
        create = ssl.create_default_context
        with patch.object(transport.ssl, 'create_default_context', wraps=create) as default:
            context = transport.client_tls_context()
        default.assert_called_once_with()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_frozen_app_requires_bundle_module(self):
        with patch.dict(sys.modules, {'certifi': None}), patch.object(sys, 'frozen', True, create=True):
            with self.assertRaisesRegex(RuntimeError, 'CA'):
                transport.client_tls_context()

    def test_mac_source_requires_bundle_module(self):
        with patch.dict(sys.modules, {'certifi': None}), patch.object(sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(RuntimeError, 'CA'):
                transport.client_tls_context()

    def test_linux_gateway_uses_system_trust_without_pip(self):
        with patch.dict(sys.modules, {'certifi': None}), patch.object(sys, 'platform', 'linux'), \
                patch.object(sys, 'frozen', False, create=True):
            self.assertIsNone(transport.bundled_ca_file())
            ctx = transport.client_tls_context()
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)

    def test_missing_or_corrupt_bundle_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp)/'bad.pem'
            for exists in (False, True):
                if exists:
                    bad.write_text('not a certificate', encoding='ascii')
                with patch.object(transport, 'bundled_ca_file', return_value=str(bad)):
                    with self.assertRaisesRegex(RuntimeError, 'CA'):
                        transport.client_tls_context()

    def test_tls_error_does_not_echo_secrets_or_raw_exception(self):
        error = ssl.SSLCertVerificationError('TOKEN SECRET proxy://private')
        error.verify_code = 20
        for stage in ('proxy', 'destination'):
            sanitized = transport.ProxyTLSVerificationError(stage, error)
            self.assertEqual(sanitized.stage, stage)
            self.assertEqual(sanitized.verify_code, 20)
            self.assertNotIn('SECRET', str(sanitized))
            self.assertNotIn('TOKEN', str(sanitized))
            self.assertIn('未切换直连', str(sanitized))


class TLSOverProxyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which('openssl')
        if not openssl and sys.platform == 'win32':
            candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Git/usr/bin/openssl.exe'
            if candidate.is_file():
                openssl = str(candidate)
        if not openssl:
            raise RuntimeError('OpenSSL is required to generate the local TLS fixture.')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        tmp = Path(cls.tmp.name)
        cls.cert, cls.key = tmp/'cert.pem', tmp/'key.pem'
        config = tmp/'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\n[dn]\n[v3]\n'
                          'basicConstraints=critical,CA:TRUE\n'
                          'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n'
                          'extendedKeyUsage=serverAuth\n'
                          'subjectAltName=DNS:fixture.invalid,IP:127.0.0.1\n', encoding='ascii')
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                        '-subj', '/CN=CCBOX local TLS fixture', '-config', str(config), '-extensions', 'v3',
                        '-keyout', str(cls.key), '-out', str(cls.cert)],
                       capture_output=True, check=True, timeout=30)

    async def asyncSetUp(self):
        self.servers, self.writers, self.tasks, self.seen = [], [], set(), []
        self.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_context.load_cert_chain(self.cert, self.key)
        self.loop = asyncio.get_running_loop()
        self.previous_handler = self.loop.get_exception_handler()
        def expected_handshake_failure(loop, context):
            if isinstance(context.get('exception'), (ssl.SSLError, ConnectionError)):
                return
            loop.default_exception_handler(context)
        self.loop.set_exception_handler(expected_handshake_failure)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for writer in self.writers:
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.sleep(.02)
        self.loop.set_exception_handler(self.previous_handler)

    async def make_proxy(self, scheme):
        async def handle(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            self.writers.append(writer)
            try:
                if scheme == 'socks5h':
                    self.assertEqual(await reader.readexactly(3), b'\x05\x01\x00')
                    writer.write(b'\x05\x00')
                    await writer.drain()
                    self.assertEqual(await reader.readexactly(4), b'\x05\x01\x00\x03')
                    length = (await reader.readexactly(1))[0]
                    name = (await reader.readexactly(length)).decode('ascii')
                    self.assertEqual(await reader.readexactly(2), b'\x01\xbb')
                    self.seen.append(name)
                    writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb')
                else:
                    self.seen.append((await transport.header(reader)).decode('ascii'))
                    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                await writer.start_tls(self.server_context, ssl_handshake_timeout=5)
                self.seen.append((await transport.header(reader)).decode('ascii'))
                writer.write(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"fixture":true}')
                await writer.drain()
            except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
                pass  # Expected when a strict client rejects our untrusted test certificate.
            finally:
                writer.close()
                self.tasks.discard(task)
        server = await asyncio.start_server(handle, '127.0.0.1', 0,
                    ssl=self.server_context if scheme == 'https' else None)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return transport.parse_proxy(f'{scheme}://127.0.0.1:{port}')

    async def successful_tunnel(self, scheme):
        proxy = await self.make_proxy(scheme)
        with patch.object(transport, 'bundled_ca_file', return_value=str(self.cert)):
            result = await asyncio.wait_for(transport.fetch_json(proxy, 'fixture.invalid'), 10)
        self.assertEqual(result, {'fixture': True})
        self.assertIn('fixture.invalid', self.seen[0])
        self.assertIn('GET / HTTP/1.0', self.seen[1])

    async def test_http_connect_valid_certificate(self):
        await self.successful_tunnel('http')

    async def test_socks_remote_dns_valid_certificate(self):
        await self.successful_tunnel('socks5h')

    async def test_https_proxy_nested_tls_valid_certificate(self):
        await self.successful_tunnel('https')

    async def test_untrusted_destination_rejected_no_direct_fallback(self):
        proxy = await self.make_proxy('http')
        original = asyncio.open_connection
        with patch.object(transport.asyncio, 'open_connection', wraps=original) as connect:
            with self.assertRaises(transport.ProxyTLSVerificationError) as error:
                await asyncio.wait_for(transport.fetch_json(proxy, 'fixture.invalid'), 10)
        self.assertEqual(error.exception.stage, 'destination')
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[:2], ('127.0.0.1', proxy['port']))
        self.assertEqual(len(self.seen), 1)  # No plaintext GET after the rejected handshake.

    async def test_wrong_hostname_rejected_with_trusted_certificate(self):
        proxy = await self.make_proxy('socks5h')
        with patch.object(transport, 'bundled_ca_file', return_value=str(self.cert)):
            with self.assertRaises(transport.ProxyTLSVerificationError) as error:
                await asyncio.wait_for(transport.fetch_json(proxy, 'wrong.invalid'), 10)
        self.assertEqual(error.exception.stage, 'destination')
        self.assertEqual(error.exception.verify_code, 62)
        self.assertEqual(len(self.seen), 1)

    async def test_untrusted_https_proxy_rejected_before_connect(self):
        proxy = await self.make_proxy('https')
        original = asyncio.open_connection
        with patch.object(transport.asyncio, 'open_connection', wraps=original) as connect:
            with self.assertRaises(transport.ProxyTLSVerificationError) as error:
                await asyncio.wait_for(transport.fetch_json(proxy, 'fixture.invalid'), 10)
        self.assertEqual(error.exception.stage, 'proxy')
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.seen, [])


if __name__ == '__main__':
    unittest.main()
