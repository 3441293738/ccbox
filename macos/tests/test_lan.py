import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'runtime')]
from app.lan import private_ipv4, validate_access, desktop_ports, apply_access, sharing_details, local_addresses
from app.controller import make_compose, DEFAULT
from gateway import Gateway

TOKEN = 'test-access-token-' + 'x'*32
HOST = '192.168.1.20:6080'


class Writer:
    def __init__(self): self.data = b''
    def write(self, value): self.data += value
    async def drain(self): pass
    def close(self): pass
    def get_extra_info(self, name): return ('192.168.1.30', 30000)


def reader(data):
    r = asyncio.StreamReader(); r.feed_data(data); r.feed_eof(); return r


class AccessConfigTests(unittest.TestCase):
    def test_mac_discovery_ignores_broadcast_and_peer_addresses(self):
        with patch('app.lan.sys.platform','darwin'), patch('app.lan.subprocess.run') as run:
            run.side_effect=[Mock(stdout=''),Mock(stdout=''),Mock(stdout='inet 192.168.1.20 netmask 255.255.255.0 broadcast 192.168.1.255\ninet 127.0.0.1\ninet 10.0.0.2 --> 10.0.0.1')]
            self.assertEqual(local_addresses(),['192.168.1.20','10.0.0.2'])

    def test_only_explicit_private_ipv4(self):
        for address in ('10.0.0.10', '172.16.1.20', '192.168.1.20'):
            self.assertTrue(private_ipv4(address))
        for address in ('0.0.0.0', '127.0.0.1', '169.254.1.1', '8.8.8.8', '::1', 'localhost', '192.168.1.1:80', '192.168.01.1', '224.1.1.1'):
            with self.subTest(address=address):
                self.assertFalse(private_ipv4(address))
                with self.assertRaises(ValueError): validate_access({'lan_enabled': True, 'lan_address': address})
        with self.assertRaises(ValueError): validate_access({'lan_enabled': 'false'})

    def test_default_local_and_opt_in_bindings(self):
        self.assertEqual(desktop_ports(DEFAULT), ['127.0.0.1:6080:6080'])
        self.assertEqual(desktop_ports({**DEFAULT, 'lan_enabled': True, 'lan_address': HOST.split(':')[0]}),
                         ['127.0.0.1:6080:6080', HOST+':6080'])

    def test_compose_does_not_open_proxy_or_workload_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)/'project'; project.mkdir()
            cfg={**DEFAULT,'workspace':str(project),'lan_enabled':True,'lan_address':HOST.split(':')[0]}
            compose=make_compose(ROOT, Path(tmp)/'data', cfg, 'ccbox2-test')
            self.assertEqual(compose['services']['sandbox']['network_mode'],'none')
            self.assertNotIn('ports',compose['services']['sandbox'])
            self.assertEqual(compose['services']['gateway']['ports'],['127.0.0.1:6080:6080',HOST+':6080'])
            self.assertNotIn(TOKEN,json.dumps(compose))
            self.assertNotIn('docker.sock',json.dumps(compose))


class AccessHttpTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, gateway, method='GET', path='/desktop.html', host=HOST, origin=None, cookie=None, data=b'', extra=''):
        headers=f'{method} {path} HTTP/1.1\r\nHost: {host}\r\n'
        if origin is not None: headers+='Origin: '+origin+'\r\n'
        if cookie: headers+='Cookie: '+cookie+'\r\n'
        if method=='POST': headers+='Content-Type: application/x-www-form-urlencoded\r\nContent-Length: '+str(len(data))+'\r\n'
        w=Writer()
        with patch('gateway.Path.read_text', return_value='<html><!--ERROR--><input name="access"></html>'):
            await gateway.handle_desktop(reader((headers+extra+'\r\n').encode()+data),w)
        return w.data

    def gateway(self): return Gateway({},TOKEN,6080,True,HOST.split(':')[0])

    async def test_default_local_only_rejects_lan_even_valid_cookie(self):
        result=await self.call(Gateway({},TOKEN,6080),cookie='ccbox_session_6080='+TOKEN)
        self.assertIn(b'403',result)

    async def test_unauthenticated_page_has_no_secrets(self):
        result=await self.call(self.gateway())
        self.assertIn(b'200 OK',result);self.assertNotIn(TOKEN.encode(),result)
        self.assertIn(b"form-action 'self'",result)

    async def test_login_and_cookie_without_credential_in_location(self):
        result=await self.call(self.gateway(),'POST','/login',origin='http://'+HOST,data=('access='+TOKEN).encode())
        self.assertIn(b'303 See Other',result)
        self.assertIn(b'Location: /desktop.html\r\n',result)
        self.assertIn(b'HttpOnly; SameSite=Strict',result)
        self.assertNotIn(b'?access=',result)

    async def test_login_requires_origin_correct_token_and_known_host(self):
        for origin,host,token in [(None,HOST,TOKEN),('http://evil.invalid',HOST,TOKEN),('http://'+HOST,'evil.invalid',TOKEN),('http://'+HOST,HOST,'wrong')]:
            with self.subTest(origin=origin,host=host):
                result=await self.call(self.gateway(),'POST','/login',host=host,origin=origin,data=('access='+token).encode())
                self.assertIn(b'403',result); self.assertNotIn(b'Set-Cookie:',result)

    async def test_lan_query_token_is_not_a_login(self):
        result=await self.call(self.gateway(),path='/desktop.html?access='+TOKEN)
        self.assertNotIn(b'Set-Cookie:',result); self.assertNotIn(TOKEN.encode(),result)

    async def test_login_rate_limit(self):
        g=self.gateway()
        for _ in range(10):
            self.assertIn(b'403',await self.call(g,'POST','/login',origin='http://'+HOST,data=b'access=wrong'))
        self.assertIn(b'429',await self.call(g,'POST','/login',origin='http://'+HOST,data=b'access=wrong'))

    async def test_clipboard_same_origin_required_on_lan(self):
        g=self.gateway()
        for origin,allowed in [('http://'+HOST,True),(None,False),('http://127.0.0.1:6080',False),('http://192.168.1.20:6081',False)]:
            with patch.object(g,'handle_clipboard',new=AsyncMock()) as send:
                await self.call(g,'POST','/clipboard/text',origin=origin,cookie='ccbox_session_6080='+TOKEN,data=b'access=ok')
                self.assertEqual(send.await_count,1 if allowed else 0)

    async def test_assets_and_websocket_require_authentication(self):
        for path in ('/core/rfb.js','/websockify','/clipboard/image'):
            result=await self.call(self.gateway(),path=path)
            self.assertIn(b'403',result)
        for origin in (None,'http://evil.invalid'):
            result=await self.call(self.gateway(),path='/websockify',origin=origin,cookie='ccbox_session_6080='+TOKEN,
                                   extra='Connection: Upgrade\r\nUpgrade: websocket\r\n')
            self.assertIn(b'403',result)

    async def test_bad_paths_and_old_port_cookie_rejected(self):
        for path in ('//evil.invalid/login','/\\evil.invalid/login'):
            self.assertIn(b'403',await self.call(self.gateway(),'POST',path,origin='http://'+HOST,data=('access='+TOKEN).encode()))
        self.assertIn(b'403',await self.call(self.gateway(),path='/websockify',cookie='ccbox_session_6081='+TOKEN))


class ApplyAccessTests(unittest.TestCase):
    def fixture(self, root):
        c=Mock();c.root=root;c.data=root/'data';c.data.mkdir();(c.data/'active').mkdir()
        c.file=c.data/'compose.json';c.vault_file=c.data/'vault-test'
        old={'services':{'gateway':{'ports':['127.0.0.1:6080:6080'],'volumes':['desktop:/desktop:ro']},'sandbox':{'network_mode':'none','volumes':['home:/home/claude']}}}
        c.file.write_text(json.dumps(old));(c.data/'active/upstream.json').write_text(json.dumps({'proxy':{'host':'HOST'},'desktop_token':TOKEN,'desktop_port':6080}))
        c.config.return_value={**DEFAULT,'lan_enabled':True,'lan_address':HOST.split(':')[0]}
        c.credentials.return_value={'desktop_token':TOKEN,'project':'ccbox2-test','proxy':{'host':'HOST'}}
        c.command.side_effect=lambda *args:list(args);c.run.return_value='container-id'
        return c,old

    def test_apply_recreates_only_gateway_and_keeps_identity(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.lan.local_addresses',return_value=[HOST.split(':')[0]]):
            c,old=self.fixture(Path(tmp));apply_access(c)
            new=json.loads(c.file.read_text());active=json.loads((c.data/'active/upstream.json').read_text())
            self.assertEqual(new['services']['sandbox'],old['services']['sandbox'])
            self.assertEqual(active['proxy'],{'host':'HOST'});self.assertEqual(active['desktop_token'],TOKEN)
            self.assertTrue(active['lan_enabled'])
            args=c.run.call_args[0][0];self.assertIn('--no-deps',args);self.assertEqual(args[-1],'gateway')
            self.assertEqual(sharing_details(c),('http://'+HOST+'/desktop.html',TOKEN))

    def test_failed_update_rolls_back_files_and_never_stops_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.lan.local_addresses',return_value=[HOST.split(':')[0]]):
            c,old=self.fixture(Path(tmp));secret=c.data/'active/upstream.json';before=secret.read_bytes()
            c.run.side_effect=['container-id',RuntimeError('bind failed'),'restored']
            with self.assertRaises(RuntimeError): apply_access(c)
            self.assertEqual(json.loads(c.file.read_text()),old);self.assertEqual(secret.read_bytes(),before)
            c.stop.assert_not_called()

    def test_unassigned_address_is_rejected_before_any_container_action(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.lan.local_addresses',return_value=[]):
            c,_=self.fixture(Path(tmp))
            with self.assertRaises(ValueError): apply_access(c)
            c.run.assert_not_called()

    def test_disabling_sharing_restores_loopback_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            c,_=self.fixture(Path(tmp));c.config.return_value['lan_enabled']=False;apply_access(c)
            self.assertEqual(json.loads(c.file.read_text())['services']['gateway']['ports'],['127.0.0.1:6080:6080'])
            with self.assertRaises(ValueError):sharing_details(c)

    def test_rotation_revokes_cookie_and_preserves_proxy_project(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.lan.local_addresses',return_value=[HOST.split(':')[0]]), patch('app.vault.save') as save:
            c,_=self.fixture(Path(tmp));apply_access(c,rotate=True)
            cred=save.call_args.args[1]
            self.assertNotEqual(cred['desktop_token'],TOKEN)
            self.assertEqual(cred['proxy'],{'host':'HOST'});self.assertEqual(cred['project'],'ccbox2-test')
            g=Gateway({},cred['desktop_token'])
            self.assertFalse(g.desktop_auth('/websockify',[('Host','127.0.0.1:6080'),('Cookie','ccbox_session_6080='+TOKEN)])[0])


if __name__ == '__main__': unittest.main()
