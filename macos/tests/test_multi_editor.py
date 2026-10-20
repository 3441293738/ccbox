import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.controller import Controller, DEFAULT, make_compose, parse_proxy
from app.projects import mounts, signature, same_bind_source
from app.editor_launch import open_client, EXTENSION
from app.storage import atomic_json


class MultiEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.c = Controller(self.base / 'bundle')
        for name in ('one', 'two'):
            (self.base / name).mkdir()
        self.config = {**DEFAULT, 'workspace': str(self.base / 'one'), 'projects': [
            {'id':'p-123456789abc','name':'Two','path':str(self.base / 'two'),'readonly':True}]}
        self.c.save(self.config, parse_proxy('socks5h://HOST:1080'))
        self.project = self.c.credentials()['project']
        self.compose = make_compose(self.c.root, self.c.data, self.config, self.project)
        atomic_json(self.c.file, self.compose)
        self.info = {'Name':'/ccbox2-test-sandbox-1', 'HostConfig':{'NetworkMode':'none'},
                     'Config':{'User':'1000:1000','Labels':{'com.docker.compose.project':self.project,'io.ccbox.mounts':signature(self.config)}},
                     'Mounts':[{'Type':'bind','Source':m['source'],'Destination':m['target'],'RW':not m['read_only']} for m in mounts(self.config)]}
        self.calls = []

    def command(self, args, **kwargs):
        self.calls.append(args)
        if 'inspect' in args:
            return json.dumps([self.info])
        return 'container' if 'ps' in args else ''

    def connect(self):
        with patch.object(self.c,'docker',return_value='docker'), patch.object(self.c,'run',side_effect=self.command), patch('app.editor_launch.open_client',return_value='opened') as launch:
            self.c.open_vscode()
            return launch.call_args

    def test_multi_root_uploaded_to_container_and_opened(self):
        args = self.connect().args
        self.assertEqual(args[-1], '/home/claude/.ccbox/workspaces/projects.code-workspace')
        self.assertTrue(any('python3' in c for c in self.calls))

    def test_wrong_environment_rejected(self):
        self.info['Config']['Labels']['com.docker.compose.project'] = 'another'
        with self.assertRaisesRegex(RuntimeError,'不属于'):
            self.connect()

    def test_old_mount_label_rejected(self):
        self.info['Config']['Labels'].pop('io.ccbox.mounts')
        with self.assertRaisesRegex(RuntimeError,'旧挂载'):
            self.connect()

    def test_actual_permissions_source_and_missing_mount_rejected(self):
        old = self.info['Mounts'][1].copy()
        for field, value in [('RW',True),('Source',str(self.base / 'one')),('Destination','/wrong')]:
            self.info['Mounts'][1] = {**old, field:value}
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError,'挂载'):
                self.connect()

    def test_saved_not_applied_rejected(self):
        self.c.save({**self.config,'projects':[]}, parse_proxy('http://HOST:1080'))
        with self.assertRaisesRegex(RuntimeError,'尚未应用'):
            self.connect()

    def test_workspace_file_cli_not_host_folder(self):
        c=SimpleNamespace(vscode_directory=lambda:self.base/'editor', log=Mock(), environment_name='工作 B')
        with patch('app.editor_launch.locate_code',return_value=(Path('Code.exe'),Path('cli.js'))), patch.dict('os.environ',{'ANTHROPIC_API_KEY':'HOST_SECRET'}), patch('app.editor_launch.subprocess.run',side_effect=[SimpleNamespace(returncode=0,stdout=EXTENSION,stderr=''),SimpleNamespace(returncode=0,stdout='',stderr='')]) as call:
            open_client(c,'/ccbox2-fixture-sandbox-1','/home/claude/.ccbox/workspaces/projects.code-workspace')
        last=call.call_args
        self.assertIn('--file-uri',last.args[0])
        self.assertTrue(last.args[0][-1].endswith('/projects.code-workspace'))
        self.assertNotIn('ANTHROPIC_API_KEY',last.kwargs['env'])

    @unittest.skipUnless(os.name=='nt','Windows drive translation only')
    def test_docker_windows_bind_translation(self):
        self.assertTrue(same_bind_source('/run/desktop/mnt/host/d/project','D:\\project'))
        self.assertFalse(same_bind_source('D:\\other','D:\\project'))


if __name__ == '__main__':
    unittest.main()
