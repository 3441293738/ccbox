import asyncio
import base64
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
from gateway import Gateway
import bridge
from clipboard_transfer import PROTOCOL, MAX_IMAGE, MAX_TEXT, validate

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII=')
TEXT = '查询问题\nhost: HOST\nusername: USER\npassword: TOKEN!@#$%^&*()\n目录 /data/logs/项目/tenants 😀'.encode('utf-8')

class Writer:
    def __init__(self): self.data = bytearray(); self.closed = False
    def write(self, data): self.data.extend(data)
    async def drain(self): pass
    def close(self): self.closed = True

def reader_with(data):
    reader = asyncio.StreamReader(); reader.feed_data(data); reader.feed_eof(); return reader

def headers(kind, data):
    return [('Content-Type', 'image/png' if kind == 'image' else 'text/plain; charset=utf-8'),
            ('Content-Length', str(len(data)))]

class ClipboardGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def transfer(self, kind, payload, custom_headers=None, acknowledgement=b'OK\n'):
        reader = AsyncMock(); reader.readline.return_value = acknowledgement
        control = Writer(); response = Writer()
        with patch('gateway.open_unix_connection', new=AsyncMock(return_value=(reader, control))) as connect:
            await Gateway({}, 'TOKEN', 6080).handle_clipboard(reader_with(payload), response,
                custom_headers or headers(kind, payload), kind)
        return bytes(response.data), bytes(control.data), connect

    async def test_image_is_forwarded_without_interpretation(self):
        response, data, _ = await self.transfer('image', PNG)
        self.assertIn(b'204 No Content', response)
        self.assertEqual(data, PROTOCOL + b'image\n' + str(len(PNG)).encode() + b'\n' + PNG)

    async def test_utf8_multiline_and_special_characters_roundtrip(self):
        response, data, _ = await self.transfer('text', TEXT)
        self.assertIn(b'204 No Content', response)
        self.assertEqual(data, PROTOCOL + b'text\n' + str(len(TEXT)).encode() + b'\n' + TEXT)

    async def test_wrong_media_and_size_rejected_before_forwarding(self):
        for kind, fields, expected in [
            ('image', [('Content-Type','image/jpeg'),('Content-Length','5')], b'415'),
            ('image', [('Content-Type','image/png'),('Content-Length',str(MAX_IMAGE+1))], b'413'),
            ('text', [('Content-Type','text/plain'),('Content-Length',str(MAX_TEXT+1))], b'413'),
            ('text', [('Content-Type','text/plain'),('Content-Length','-1')], b'400'),
            ('text', [('Content-Type','text/plain'),('Content-Length','0')], b'413'),
            ('text', [('Content-Type','text/plain')], b'400'),
            ('text', [('Content-Type','text/plain'),('Content-Length','1'),('Transfer-Encoding','chunked')], b'400')]:
            with self.subTest(fields=fields):
                response, data, connect = await self.transfer(kind, b'', fields)
                self.assertIn(expected, response); connect.assert_not_called()

    async def test_invalid_utf8_png_and_nul_are_rejected(self):
        for kind, data in [('text',b'bad\xff'),('text',b'a\0b'),('image',b'notpng')]:
            response, _, connect = await self.transfer(kind,data)
            self.assertIn(b'400',response); connect.assert_not_called()

    async def test_backend_error_is_not_reported_as_success(self):
        response, _, _ = await self.transfer('text',TEXT,acknowledgement=b'ERR\n')
        self.assertIn(b'502 Bad Gateway',response); self.assertNotIn(TEXT,response)

    async def test_disconnected_backend_returns_useful_status(self):
        with patch('gateway.open_unix_connection',new=AsyncMock(side_effect=FileNotFoundError)):
            response=Writer()
            await Gateway({},'TOKEN').handle_clipboard(reader_with(TEXT),response,headers('text',TEXT),'text')
        self.assertIn(b'502',response.data)

    async def test_post_requires_cookie_and_explicit_same_origin(self):
        gateway = Gateway({},'TOKEN',6080)
        for cookie, origin, target, status in [
            ('', 'http://127.0.0.1:6080', '/clipboard/text', b'403'),
            ('ccbox_session_6080=TOKEN', '', '/clipboard/text', b'403'),
            ('ccbox_session_6080=TOKEN','https://evil.invalid','/clipboard/text',b'403'),
            ('ccbox_session_6080=WRONG','http://127.0.0.1:6080','/clipboard/text',b'403'),
            ('','http://127.0.0.1:6080','/clipboard/text?access=TOKEN',b'403'),
            ('ccbox_session_6080=TOKEN','http://127.0.0.1:6080','/clipboard/text',None)]:
            request=f'POST {target} HTTP/1.1\r\nHost: 127.0.0.1:6080\r\nCookie: {cookie}\r\n'
            if origin: request+='Origin: '+origin+'\r\n'
            request+='Content-Type: text/plain\r\nContent-Length: 3\r\n\r\nabc'
            response=Writer()
            with patch.object(gateway,'handle_clipboard',new=AsyncMock()) as send:
                await gateway.handle_desktop(reader_with(request.encode()),response)
            if status:
                self.assertIn(status,response.data);send.assert_not_awaited()
            else: send.assert_awaited_once()

class DesktopConnectionTests(unittest.IsolatedAsyncioTestCase):
    def test_assets_close_connection_instead_of_tunneling(self):
        from gateway import desktop_transport_headers, desktop_websocket
        fields=[('Connection','keep-alive'),('Keep-Alive','timeout=60')]
        self.assertFalse(desktop_websocket(fields))
        sent=dict(desktop_transport_headers(fields))
        self.assertEqual(sent['Connection'],'close')
        self.assertNotIn('Keep-Alive',sent)

    def test_valid_websocket_still_upgrades(self):
        from gateway import desktop_transport_headers, desktop_websocket
        fields=[('Connection','keep-alive, Upgrade'),('Upgrade','WebSocket'),('Sec-WebSocket-Key','KEY')]
        self.assertTrue(desktop_websocket(fields))
        sent=dict(desktop_transport_headers(fields))
        self.assertEqual(sent['Connection'],'Upgrade')
        self.assertEqual(sent['Upgrade'],'websocket')
        self.assertEqual(sent['Sec-WebSocket-Key'],'KEY')

    async def test_asset_response_closes_and_never_relays_second_request(self):
        upstream=Writer(); response=Writer()
        source=reader_with(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nJS')
        request=(b'GET /core/rfb.js HTTP/1.1\r\nHost: 127.0.0.1:6080\r\nCookie: ccbox_session_6080=TOKEN\r\n'
                 b'Connection: keep-alive\r\n\r\nPOST /clipboard/text HTTP/1.1\r\n\r\nSHOULD_NOT_FORWARD')
        with patch('gateway.open_unix_connection',new=AsyncMock(return_value=(source,upstream))), patch('gateway.relay',new=AsyncMock()) as relay:
            await Gateway({},'TOKEN',6080).handle_desktop(reader_with(request),response)
        relay.assert_not_awaited()
        self.assertNotIn(b'POST',upstream.data)
        self.assertIn(b'Connection: close',upstream.data)
        self.assertIn(b'Connection: close',response.data)
        self.assertNotIn(b'keep-alive',response.data)
        self.assertTrue(bytes(response.data).endswith(b'\r\n\r\nJS'))
        self.assertTrue(response.closed)


class ClipboardBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge.clipboard_lock=asyncio.Lock();bridge.clipboard_slots=asyncio.Semaphore(4)

    async def test_xclip_uses_utf8_target_and_stdin_not_shell(self):
        process=AsyncMock();process.returncode=0;process.communicate.return_value=(b'',b'')
        for kind, payload, target in [('image',PNG,'image/png'),('text',TEXT,'UTF8_STRING')]:
            writer=Writer()
            with patch('bridge.asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)) as spawn:
                await bridge.clipboard_control(reader_with(PROTOCOL+kind.encode()+b'\n'+str(len(payload)).encode()+b'\n'+payload),writer)
            self.assertEqual(bytes(writer.data),b'OK\n')
            self.assertEqual(spawn.call_args.args,('/usr/bin/xclip','-selection','clipboard','-t',target,'-i'))
            process.communicate.assert_awaited_with(payload)
            self.assertTrue(writer.closed)

    async def test_invalid_input_never_spawns_process(self):
        for data in [b'UNKNOWN\n', PROTOCOL+b'exec\n2\nsh',PROTOCOL+b'image\n3\nabc',PROTOCOL+b'text\n99999999999\n']:
            writer=Writer()
            with patch('bridge.asyncio.create_subprocess_exec') as spawn:
                await bridge.clipboard_control(reader_with(data),writer)
            self.assertEqual(bytes(writer.data),b'ERR\n');spawn.assert_not_called()

    async def test_failed_xclip_is_reported(self):
        process=AsyncMock();process.returncode=1;process.communicate.return_value=(b'',b'')
        writer=Writer()
        with patch('bridge.asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)):
            await bridge.clipboard_control(reader_with(PROTOCOL+b'text\n3\nabc'),writer)
        self.assertEqual(bytes(writer.data),b'ERR\n')

class ClipboardContractTests(unittest.TestCase):
    def test_png_dimensions_bounded(self):
        payload=bytearray(PNG);payload[16:20]=(50000).to_bytes(4,'big')
        with self.assertRaises(ValueError):validate('image',payload)

    def test_runtime_revision_and_components_match(self):
        from app.sandbox_editor import REVISION
        self.assertEqual(REVISION,'3')
        for name in ['Dockerfile.sandbox','Dockerfile.gateway']:
            content=(ROOT/'runtime'/name).read_text(encoding='utf-8')
            self.assertIn('io.ccbox.desktop-editor="'+REVISION+'"',content)
            self.assertIn('clipboard_transfer.py',content)
        self.assertIn(' xclip ',(ROOT/'runtime/Dockerfile.sandbox').read_text(encoding='utf-8'))

    def test_viewer_only_sends_after_confirmation(self):
        html=(ROOT/'runtime/desktop.html').read_text(encoding='utf-8')
        self.assertNotIn('clipboardPasteFrom(',html)
        self.assertNotIn("document.addEventListener('paste'",html)
        self.assertIn("$('image-paste').addEventListener('paste'",html)
        self.assertIn("$('image-send').onclick=async",html)
        self.assertIn("uploadClipboard('text',value)",html)
        self.assertIn('内容已保留',html)
        self.assertNotIn('navigator.clipboard.read',html)

if __name__=='__main__':unittest.main()
