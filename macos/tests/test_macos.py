import importlib.util
import io
import json
import os
import shlex
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import mac_vault, host_platform, storage, vault
from app.controller import Controller, DEFAULT, make_compose
from app.editor_launch import locate_code
from app.projects import same_bind_source
spec=importlib.util.spec_from_file_location('mac_editor_runtime',ROOT/'runtime/sandbox_editor.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)


class MemoryKeychain:
    def __init__(self):self.values={}
    def get_password(self,service,key):return self.values.get((service,key))
    def set_password(self,service,key,value):self.values[service,key]=value
    def delete_password(self,service,key):self.values.pop((service,key),None)


class MacVaultTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.base=Path(temp.name);self.file=self.base/'env/credentials.keychain.json'
        self.store=MemoryKeychain()
        p=patch.object(mac_vault,'backend',return_value=self.store);p.start();self.addCleanup(p.stop)
        self.secret={'proxy':{'username':'SERIAL','password':'TOKEN secret'},'project':'ccbox2-macfixture'}

    def test_reference_contains_no_proxy_or_account_credentials(self):
        mac_vault.save(self.file,self.secret)
        obj=json.loads(self.file.read_text());self.assertEqual(set(obj),{'version','backend','id'})
        self.assertNotIn('TOKEN',self.file.read_text());self.assertNotIn('SERIAL',self.file.read_text())
        self.assertEqual(mac_vault.load(self.file),self.secret)

    def test_reference_survives_same_mac_directory_migration(self):
        mac_vault.save(self.file,self.secret)
        moved=self.base/'newfolder/ref';moved.parent.mkdir();shutil.copy2(self.file,moved)
        self.assertEqual(mac_vault.load(moved),self.secret)

    def test_new_environment_gets_independent_item(self):
        other=self.base/'env-b/ref';mac_vault.save(self.file,self.secret);mac_vault.save(other,{'proxy':{'password':'OTHER'}})
        self.assertNotEqual(mac_vault.reference(self.file),mac_vault.reference(other))
        self.assertEqual(mac_vault.load(self.file),self.secret)

    def test_save_identical_reuses_item_and_update_preserves_backup(self):
        mac_vault.save(self.file,self.secret);before=self.file.read_bytes()
        mac_vault.save(self.file,self.secret);self.assertEqual(len(self.store.values),1)
        old=self.base/'old-ref';old.write_bytes(before)
        mac_vault.save(self.file,{'updated':'new'})
        self.assertEqual(mac_vault.load(old),self.secret)
        self.assertEqual(mac_vault.load(self.file),{'updated':'new'})

    def test_missing_item_fails_without_rotating_identity(self):
        mac_vault.save(self.file,self.secret);before=self.file.read_bytes();self.store.values.clear()
        with self.assertRaises(RuntimeError):mac_vault.save(self.file,{'reset':True})
        self.assertEqual(self.file.read_bytes(),before)

    def test_failed_commit_removes_only_new_item(self):
        mac_vault.save(self.file,self.secret);before=self.file.read_bytes();keys=set(self.store.values)
        with patch.object(mac_vault.os,'replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):mac_vault.save(self.file,{'changed':True})
        self.assertEqual(self.file.read_bytes(),before);self.assertEqual(set(self.store.values),keys)

    def test_keychain_failure_never_falls_back_to_plaintext(self):
        with patch.object(self.store,'set_password',side_effect=RuntimeError('TOKEN secret')):
            with self.assertRaises(RuntimeError) as err:mac_vault.save(self.file,self.secret)
        self.assertFalse(self.file.exists());self.assertNotIn('TOKEN',str(err.exception))

    def test_malformed_or_windows_blob_rejected(self):
        self.file.parent.mkdir()
        for raw in (b'WINDOWS DPAPI', b'{"version":1,"backend":"plaintext","id":"x"}',b'null'):
            self.file.write_bytes(raw)
            with self.assertRaises(RuntimeError):mac_vault.load(self.file)

    def test_vault_dispatch_uses_native_backend(self):
        with patch.object(vault.sys,'platform','darwin'),patch.object(vault,'transform',side_effect=AssertionError('DPAPI called')):
            vault.save(self.file,self.secret)
            self.assertEqual(vault.load(self.file),self.secret)


class MacHostTests(unittest.TestCase):
    def test_bundle_resources_and_mutable_data_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())  # macOS /var is an alias for /private/var.
            resources=Path(tmp)/'CCBOX.app/Contents/Frameworks';home=Path(tmp)/'user'
            with patch.object(host_platform.sys,'platform','darwin'),patch.object(host_platform.sys,'frozen',True,create=True),patch.object(host_platform.sys,'_MEIPASS',str(resources),create=True),patch.object(host_platform.Path,'home',return_value=home):
                self.assertEqual(host_platform.resource_root(),resources)
                self.assertEqual(storage.data_location(resources),home/'Library/Application Support/CCBOX/data')
                self.assertEqual(storage.pointer_file(resources),home/'Library/Application Support/CCBOX/storage-location.json')
                self.assertEqual(storage.data_location(Path(tmp)/'fixture'),Path(tmp)/'fixture/data')

    def test_finder_path_locates_docker_app_and_helper_directory(self):
        with patch.object(host_platform.sys,'platform','darwin'),patch.object(host_platform.shutil,'which',return_value=None),patch.object(host_platform.Path,'is_file',return_value=True):
            self.assertIn('Docker.app',host_platform.find_docker())
            env=host_platform.docker_environment()
            self.assertIn('Docker.app',env['PATH']);self.assertIn('/opt/homebrew/bin',env['PATH'])

    def test_terminal_quoting_roundtrips_metacharacters_and_spaces(self):
        args=['/Applications/Docker.app/bin/docker','compose','-f','/tmp/a b/";$(touch NO);/compose.json','exec','sandbox','claude']
        self.assertEqual(shlex.split(host_platform.terminal_text(args)),args)
        with self.assertRaises(ValueError):host_platform.terminal_text(['docker','bad\ncommand'])

    def test_terminal_script_is_constant_data_not_interpolated_code(self):
        args=['docker','exec','sandbox','claude']
        with patch.object(host_platform.sys,'platform','darwin'),patch.object(host_platform,'docker_environment',return_value={'PATH':'/usr/bin:/bin'}),patch.object(host_platform.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            host_platform.open_terminal(args)
            call=run.call_args.args[0]
            self.assertEqual(call[:2],['/usr/bin/osascript','-e'])
            self.assertIn('item 1 of argv',call[2]);self.assertNotIn('docker exec',call[2])
            self.assertTrue(call[3].endswith('docker exec sandbox claude'))
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_mac_compose_forces_arm64_without_weakening_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)/'project';project.mkdir()
            with patch.object(sys,'platform','darwin'):
                value=make_compose(Path(tmp)/'bundle',Path(tmp)/'data',{**DEFAULT,'workspace':str(project)},'ccbox2-test')
            for service in value['services'].values():
                self.assertEqual(service['platform'],'linux/arm64');self.assertEqual(service['cap_drop'],['ALL'])
            self.assertEqual(value['services']['sandbox']['network_mode'],'none')

    def test_mac_vault_filename_and_windows_import_guard(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(sys,'platform','darwin'):
            c=Controller(Path(tmp)/'bundle');self.assertEqual(c.vault_file.name,'credentials.keychain.json')
            c.data.mkdir(parents=True);(c.data/'credentials.dpapi').write_bytes(b'WINDOWS')
            with self.assertRaises(RuntimeError):Controller(c.root)

    def test_docker_bind_path_translation(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(sys,'platform','darwin'):
            # Actual path shape is POSIX on Mac; comparison itself is platform-native.
            if os.name!='nt':
                self.assertTrue(same_bind_source('/host_mnt'+tmp,tmp))
                self.assertTrue(same_bind_source('/run/desktop/mnt/host'+tmp,tmp))
            self.assertFalse(same_bind_source(tmp+'/other',tmp))

    def test_native_mac_vscode_locator(self):
        with patch.object(sys,'platform','darwin'),patch.object(Path,'is_file',return_value=True):
            exe,cli=locate_code();self.assertIn('Visual Studio Code.app',str(exe));self.assertEqual(exe.name,'Electron')
            self.assertEqual(cli.name,'cli.js')


class ArmRuntimeTests(unittest.TestCase):
    def test_architecture_selection(self):
        for machine,expected in [('arm64','arm64'),('aarch64','arm64'),('x86_64','x64'),('AMD64','x64')]:
            with patch.object(runtime.platform,'machine',return_value=machine):self.assertEqual(runtime.architecture(),expected)
        with patch.object(runtime.platform,'machine',return_value='riscv64'):
            with self.assertRaises(RuntimeError):runtime.architecture()

    def archive(self,path,name):
        with tarfile.open(path,'w:gz') as tar:
            entry=tarfile.TarInfo(name);entry.size=4;entry.mode=0o755
            tar.addfile(entry,io.BytesIO(b'CODE'))

    def test_arm64_tar_root_and_wrong_architecture_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);archive=base/'app.tgz';self.archive(archive,'VSCode-linux-arm64/code')
            runtime.extract(archive,base/'good','arm64')
            self.assertEqual((base/'good/VSCode-linux-arm64/code').read_bytes(),b'CODE')
            with self.assertRaises(ValueError):runtime.extract(archive,base/'wrong','x64')

    def test_arm64_tar_traversal_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);archive=base/'app.tgz';self.archive(archive,'VSCode-linux-arm64/../../escape')
            with self.assertRaises(ValueError):runtime.extract(archive,base/'out','arm64')

    def test_arm64_update_manifest_selected(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime,'BASE',Path(tmp)),patch.object(runtime,'installed',return_value=False),patch.object(runtime,'architecture',return_value='arm64'),patch.object(runtime,'download',side_effect=RuntimeError('test halt')) as download:
            with self.assertRaisesRegex(RuntimeError,'test halt'):runtime.install()
            self.assertEqual(download.call_args.args[0],'https://update.code.visualstudio.com/api/update/linux-arm64/stable/latest')


if __name__=='__main__':unittest.main()
