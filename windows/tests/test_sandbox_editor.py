import importlib.util
import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import sandbox_editor as host
sys.path.insert(0,str(ROOT/'runtime'))
from gateway import Gateway, desktop_transport_headers
spec=importlib.util.spec_from_file_location('sandbox_editor_runtime',ROOT/'runtime/sandbox_editor.py')
runtime=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        p=patch.object(runtime,'BASE',self.base)
        p.start();self.addCleanup(p.stop)

    def test_process_titles_and_missing_chromium_environment(self):
        exe=str(self.base/'install/code');profile=str(self.base/'user-data')
        rows={10:{'exe':exe,'ppid':1,'raw':exe+' --user-data-dir '+profile+' --extensions-dir EXT --proxy-server='+runtime.PROXY},
              11:{'exe':exe,'ppid':10,'raw':exe+' --type=zygote'},
              12:{'exe':exe,'ppid':11,'raw':exe+' --type=renderer'},
              13:{'exe':exe,'ppid':10,'raw':exe+' --type=utility --utility-sub-type=node.mojom.NodeService'},
              14:{'exe':exe,'ppid':13,'raw':exe+' jsonServerMain','env':{'VSCODE_ESM_ENTRYPOINT':'vs/workbench/api/node/extensionHostProcess'}},
              15:{'exe':str(self.base/'extensions/anthropic.claude-code-2.0/resources/claude'),'ppid':13,'raw':'claude'}}
        actual=runtime.classify_processes(rows)
        for key in ('desktop_main','desktop_renderer','desktop_extension_host','desktop_claude_backend','desktop_proxy_argument'):
            self.assertTrue(actual[key],key)
        self.assertFalse(actual['desktop_proxy_environment'])

    def test_orphan_helper_and_other_profile_are_not_main_editor(self):
        exe=str(self.base/'install/code')
        rows={1:{'exe':exe,'ppid':0,'raw':exe+' jsonServerMain','env':{'CCBOX_SANDBOX_EDITOR':'1'}},
              2:{'exe':exe,'ppid':0,'raw':exe+' --user-data-dir /tmp/qa --extensions-dir /tmp/extensions'},
              3:{'exe':exe,'ppid':2,'raw':exe+' --type=renderer'}}
        actual=runtime.classify_processes(rows)
        self.assertFalse(any(actual.values()))

    def test_manifest_restricts_source_and_requires_checksum(self):
        valid={'url':'https://vscode.download.prss.microsoft.com/code.tar.gz','sha256hash':'a'*64,'productVersion':'1.141.0'}
        self.assertEqual(runtime.metadata(valid),valid)
        for update in ({'url':'http://vscode.download.prss.microsoft.com/x'}, {'url':'https://evil.invalid/x'},
                       {'url':'https://user:pass@vscode.download.prss.microsoft.com/x'}, {'sha256hash':''},
                       {'productVersion':'../evil'}):
            with self.subTest(update=update),self.assertRaises(ValueError):runtime.metadata({**valid,**update})

    def test_explicit_proxy_download_never_uses_noproxy_environment(self):
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            runtime.download('https://example.invalid/code',self.base/'download')
        args=run.call_args.args[0]
        self.assertEqual(args[args.index('--proxy')+1],runtime.PROXY)
        self.assertEqual(args[args.index('--noproxy')+1],'')
        self.assertIn('--proto-redir',args)

    def test_failed_download_does_not_retry_direct(self):
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=7,stderr='failed')) as run:
            with self.assertRaisesRegex(RuntimeError,'直连'):runtime.download('https://example.invalid',self.base/'x')
        self.assertEqual(run.call_count,1)

    def test_archive_retry_resumes_through_same_proxy(self):
        target=self.base/'partial.tar.gz';target.write_bytes(b'partial')
        with patch('subprocess.run',side_effect=[SimpleNamespace(returncode=18,stderr='interrupted'),SimpleNamespace(returncode=0)] ) as run,patch.object(runtime.time,'sleep'):
            runtime.download('https://example.invalid/code',target,resume=True)
        self.assertEqual(run.call_count,2)
        for call in run.call_args_list:
            args=call.args[0]
            self.assertEqual(args[args.index('--continue-at')+1],'-')
            self.assertEqual(args[args.index('--proxy')+1],runtime.PROXY)
        self.assertEqual(target.read_bytes(),b'partial')

    def test_ranges_cover_only_missing_tail(self):
        spans=runtime.ranges(7,53,8)
        self.assertEqual([x for a,b in spans for x in range(a,b+1)],list(range(7,53)))

    def test_range_files_are_distinct_and_response_checked(self):
        paths=[]
        def run(args,**kwargs):
            target=Path(args[args.index('--output')+1]);headers=Path(args[args.index('--dump-header')+1])
            a,b=map(int,args[args.index('--range')+1].split('-'))
            target.write_bytes(b'x'*(b-a+1));headers.write_text(f'HTTP/1.1 206 Partial Content\nContent-Range: bytes {a}-{b}/32\n')
            paths.append(str(target));return SimpleNamespace(returncode=0)
        with patch('subprocess.run',side_effect=run):
            for a,b in runtime.ranges(0,32,16):
                runtime.download_range('https://example.invalid',self.base/f'x.tar.gz.chunk-{a}-{b}',a,b,32)
        self.assertEqual(len(set(paths)),2)

    def test_range_rejects_wrong_content_range(self):
        def run(args,**kwargs):
            Path(args[args.index('--output')+1]).write_bytes(b'abc')
            Path(args[args.index('--dump-header')+1]).write_text('Content-Range: bytes 5-7/32\n')
            return SimpleNamespace(returncode=0)
        with patch('subprocess.run',side_effect=run),self.assertRaisesRegex(RuntimeError,'范围不符'):
            runtime.download_range('https://example.invalid',self.base/'x.chunk-0-2',0,2,32)

    def archive(self,name,link=None):
        file=self.base/'archive.tar.gz'
        with tarfile.open(file,'w:gz') as tar:
            member=tarfile.TarInfo(name)
            if link:
                member.type=tarfile.SYMTYPE;member.linkname=link;tar.addfile(member)
            else:
                member.size=2;tar.addfile(member,io.BytesIO(b'OK'))
        return file

    def test_archive_traversal_and_links_rejected(self):
        for name,link in [('../escape',None),('/outside',None),('VSCode-linux-x64/../../escape',None),('VSCode-linux-x64/escape','../../outside')]:
            with self.subTest(name=name),self.assertRaises((ValueError,tarfile.FilterError)):
                runtime.extract(self.archive(name,link),self.base/'unpack')

    def test_archive_good(self):
        runtime.extract(self.archive('VSCode-linux-x64/code'),self.base/'unpack')
        self.assertEqual((self.base/'unpack/VSCode-linux-x64/code').read_bytes(),b'OK')

    @patch('window_restore.install_binding')
    def test_settings_preserve_preferences_and_trust_defaults(self, binding):
        runtime.configure('Account A')
        file=self.base/'user-data/User/settings.json'
        settings=json.loads(file.read_text());settings['editor.tabSize']=3
        file.write_text(json.dumps(settings))
        runtime.configure('Account B')
        settings=json.loads(file.read_text())
        self.assertEqual(settings['editor.tabSize'],3)
        self.assertIn('Account B',settings['window.title'])
        self.assertEqual(settings['http.proxy'],runtime.PROXY)
        self.assertTrue(settings['http.proxyStrictSSL'])
        self.assertNotIn('security.workspace.trust.enabled',settings)
        self.assertEqual(settings['telemetry.telemetryLevel'],'off')
        self.assertEqual(len(json.loads((file.parent/'keybindings.json').read_text())),1)

    def test_desktop_environment_clears_remote_host_browser_helpers(self):
        with patch.dict(os.environ,{'VSCODE_BROWSER':'HOST_HELPER','VSCODE_IPC_HOOK_CLI':'HOST_PIPE','ANTHROPIC_API_KEY':'TOKEN','ELECTRON_RUN_AS_NODE':'1','SSH_AUTH_SOCK':'SOCKET'}):
            env=runtime.environment()
        for key in ('VSCODE_BROWSER','VSCODE_IPC_HOOK_CLI','ANTHROPIC_API_KEY','ELECTRON_RUN_AS_NODE','SSH_AUTH_SOCK'):
            self.assertNotIn(key,env)
        self.assertEqual(env['BROWSER'],'/usr/local/bin/ccbox-browser')
        self.assertEqual(env['HTTPS_PROXY'],runtime.PROXY)

    def test_launch_rejects_other_paths(self):
        with self.assertRaises(ValueError):runtime.launch('/home/claude')

    def test_installed_application_skips_network(self):
        (self.base/'install/resources/app').mkdir(parents=True)
        (self.base/'install/code').touch();(self.base/'install/resources/app/product.json').touch()
        with patch.object(runtime,'download') as download:runtime.install()
        download.assert_not_called()

    def test_existing_official_publisher_case_reuses_container_extension(self):
        source=self.base/'home/.vscode-server/extensions/anthropic.claude-code-2.1.296-linux-x64'
        source.mkdir(parents=True);(source/'package.json').write_text(json.dumps({'publisher':'Anthropic','name':'claude-code','version':'2.1.296'}))
        (self.base/'extensions').mkdir()
        with patch.object(runtime.Path,'home',return_value=self.base/'home'),patch('subprocess.run') as run:
            runtime.prepare_extension()
        self.assertTrue((self.base/'extensions'/source.name/'package.json').exists())
        run.assert_not_called()


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name)
        self.c=Mock(data=self.data,environment_name='Environment B & Test')
        self.c.command.side_effect=lambda *a:['docker','compose',*a]
        self.c.credentials.return_value={'project':'ccbox2-test'}
        self.c.config.return_value={'workspace':'/fixture'}
        self.info={'HostConfig':{'Privileged':False,'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']},'Config':{'Labels':{host.LABEL:host.REVISION}},
                   'Mounts':[{'Type':'volume','Destination':t,'Name':'ccbox2-test_'+n,'RW':rw} for t,n,rw in [('/home/claude','home',True),('/ipc','ipc',False),('/desktop','desktop',True)]]}
        self.c.editor_context.return_value=(self.info,'/home/claude/.ccbox/workspaces/projects.code-workspace')
        self.c.file=self.data/'compose.json'
        self.c.file.write_text(json.dumps({'services':{k:{'image':'ccbox2-test-'+k} for k in ('sandbox','gateway')}}))

    def test_native_entry_reuses_workspace_and_does_not_open_host_code(self):
        with patch.object(host,'capable',return_value=True),patch.object(host,'deploy_runtime'),patch.object(host,'stream') as stream,patch.object(host,'open_viewer',return_value=True),patch.object(host,'ensure_viewer_mount'):
            result=host.open_editor(self.c)
        args=stream.call_args.args[1]
        self.assertIn('python3',args);self.assertIn(host.SCRIPT,args)
        self.assertEqual(args[-1],'/home/claude/.ccbox/workspaces/projects.code-workspace')
        self.assertTrue(host.ready(self.c));self.assertIn('沙箱 VS Code',result)
        self.c.stop.assert_not_called()

    def test_old_image_requires_restart_consent(self):
        with patch.object(host,'capable',return_value=False),patch.object(host,'upgrade') as upgrade:
            with self.assertRaisesRegex(RuntimeError,'确认重启'):host.open_editor(self.c)
        upgrade.assert_not_called()

    def test_privileged_container_rejected(self):
        self.info['HostConfig']['Privileged']=True
        with self.assertRaisesRegex(RuntimeError,'特权'):host.open_editor(self.c,True)

    def test_foreign_account_volume_rejected(self):
        self.info['Mounts'][0]['Name']='another_home'
        with self.assertRaisesRegex(RuntimeError,'不属于'):host.verify_boundary(self.c,self.info)

    def test_extra_host_mount_rejected(self):
        self.info['Mounts'].append({'Destination':'/host-secret','Type':'bind'})
        with self.assertRaisesRegex(RuntimeError,'额外挂载'):host.verify_boundary(self.c,self.info)

    def test_weakened_container_boundaries_rejected(self):
        for key,value in [('ReadonlyRootfs',False),('CapDrop',[]),('SecurityOpt',[])]:
            original=self.info['HostConfig'][key];self.info['HostConfig'][key]=value
            with self.assertRaisesRegex(RuntimeError,'限制'):host.verify_boundary(self.c,self.info)
            self.info['HostConfig'][key]=original

    def test_failed_install_does_not_mark_ready(self):
        with patch.object(host,'capable',return_value=True),patch.object(host,'deploy_runtime'),patch.object(host,'stream',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):host.open_editor(self.c)
        self.assertFalse(host.ready(self.c))

    def test_browser_only_opens_local_authenticated_viewer(self):
        (self.data/'active').mkdir();(self.data/'active/upstream.json').write_text(json.dumps({'desktop_port':6083,'desktop_token':'TOKEN'}))
        with patch.object(host.webbrowser,'open',return_value=True) as browser:
            host.open_viewer(self.c)
        url=browser.call_args.args[0]
        self.assertTrue(url.startswith('http://127.0.0.1:6083/desktop.html?'))
        self.assertIn('access=TOKEN',url)

    def test_build_failure_restores_tags_without_stopping(self):
        self.c.build_images.side_effect=RuntimeError('build')
        with self.assertRaises(RuntimeError):host.upgrade(self.c)
        self.c.stop.assert_not_called();self.c.start.assert_not_called()
        self.assertEqual(sum('before-desktop-v2' in str(a) for a in self.c.run.call_args_list),4)

    def test_start_failure_rolls_back_original_images(self):
        self.c.start.side_effect=[RuntimeError('failed'),None]
        with self.assertRaisesRegex(RuntimeError,'已恢复'):host.upgrade(self.c)
        self.assertEqual(self.c.start.call_count,2)
        self.assertEqual(self.c.stop.call_count,2)

    def test_stopped_status_does_not_start(self):
        self.c.run.return_value=''
        self.assertIn('已停止',host.status(self.c))
        self.c.start.assert_not_called()

    def test_viewer_has_no_automatic_host_clipboard_or_external_assets(self):
        html=(ROOT/'runtime/desktop.html').read_text(encoding='utf-8')
        self.assertNotIn('navigator.clipboard.read',html)
        self.assertNotIn('navigator.clipboard.write',html)
        self.assertNotIn('src="https://',html)
        self.assertIn("uploadClipboard('text',value)",html)
        self.assertIn('.textContent=label',html)

    def test_viewer_headers_do_not_forward_host_metadata(self):
        incoming=[('Host','127.0.0.1:6080'),('User-Agent','Windows fixture'),('Accept-Language','zh-CN'),
                  ('Sec-CH-UA','fixture'),('Cookie','TOKEN'),('Referer','http://host/private'),
                  ('Origin','http://127.0.0.1:6080'),('Connection','Upgrade'),('Upgrade','websocket'),('Sec-WebSocket-Key','fixture-key')]
        outgoing=dict(desktop_transport_headers(incoming))
        for key in ('Accept-Language','Sec-CH-UA','Cookie','Referer'):self.assertNotIn(key,outgoing)
        self.assertEqual(outgoing['User-Agent'],'CCBOX-Desktop-Viewer')
        self.assertEqual(outgoing['Sec-WebSocket-Key'],'fixture-key')

    def test_helper_update_stays_in_container_home(self):
        self.c.root=ROOT
        with patch('app.sandbox_editor.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            host.deploy_runtime(self.c)
        self.assertIn('exec',run.call_args.args[0])
        self.assertIn(host.SCRIPT,run.call_args.args[0][-1])
        self.assertIn(b'def install',run.call_args.kwargs['input'])


class ViewerGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def request(self,path,cookie=True,origin=None):
        gateway=Gateway({},'TOKEN',6080)
        server=await asyncio.start_server(gateway.handle_desktop,'127.0.0.1',0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port=server.sockets[0].getsockname()[1]
        gateway.desktop_port=port
        gateway.hosts={f"127.0.0.1:{port}"}
        gateway.cookie_name=f"ccbox_session_{port}"
        reader,writer=await asyncio.open_connection('127.0.0.1',port)
        headers=f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
        if cookie:headers+='Cookie: '+gateway.cookie_name+'=TOKEN\r\n'
        if origin:headers+='Origin: '+origin+'\r\n'
        writer.write((headers+'\r\n').encode());await writer.drain()
        with patch('gateway.Path.read_bytes',return_value=b'<html>Fixture</html>'):
            response=await asyncio.wait_for(reader.read(),5)
        writer.close();await writer.wait_closed()
        server.close()
        return response

    async def test_viewer_requires_auth_and_same_origin(self):
        self.assertIn(b'403 Forbidden',await self.request('/desktop.html',cookie=False))
        self.assertIn(b'403 Forbidden',await self.request('/desktop.html',origin='https://evil.invalid'))

    async def test_viewer_served_only_after_authentication(self):
        response=await self.request('/desktop.html')
        self.assertIn(b'200 OK',response);self.assertIn(b'<html>Fixture</html>',response)
        self.assertIn(b'Cache-Control: no-store',response)
        self.assertIn(b'frame-ancestors',response)

    async def test_access_token_removed_before_viewer(self):
        response=await self.request('/desktop.html?access=TOKEN&environment=Fixture',cookie=False)
        self.assertIn(b'303 See Other',response)
        location=next(line for line in response.split(b'\r\n') if line.startswith(b'Location:'))
        self.assertNotIn(b'TOKEN',location)


if __name__=='__main__':unittest.main()
