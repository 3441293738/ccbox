import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.editor_launch import attach_uri, configure_client, open_client, EXTENSION, locate_code


class EditorLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.name = '/ccbox2-test-sandbox-1'

    def tearDown(self):
        self.tmp.cleanup()

    def test_uri_targets_existing_container_workspace(self):
        uri = attach_uri(self.name)
        encoded = uri.split('+')[1].split('/')[0]
        self.assertEqual(json.loads(bytes.fromhex(encoded)), {'containerName': self.name})
        self.assertTrue(uri.endswith('/workspace'))

    def test_invalid_name(self):
        for name in ('no-slash', '/x?key=secret', '/x\\y', '/x x'):
            with self.assertRaises(ValueError):
                attach_uri(name)

    def test_profile_and_proxy_boundary(self):
        user, ext = configure_client(self.base, self.name)
        cfg = json.loads((user / f'User/globalStorage/{EXTENSION}/nameConfigs/ccbox2-test-sandbox-1.json').read_text())
        settings = json.loads((user / 'User/settings.json').read_text())
        self.assertEqual(cfg['remoteEnv']['HTTPS_PROXY'], 'http://127.0.0.1:18080')
        self.assertEqual(cfg['remoteEnv']['SSH_AUTH_SOCK'], '')
        self.assertEqual(cfg['remoteUser'], 'claude')
        self.assertEqual(cfg['extensions'], ['anthropic.claude-code'])
        self.assertEqual(settings['remote.extensionKind']['anthropic.claude-code'], ['workspace'])
        self.assertFalse(settings['dev.containers.copyGitConfig'])
        self.assertEqual(settings['dev.containers.gitCredentialHelperConfigLocation'], 'none')
        self.assertTrue(user.is_relative_to(self.base))
        self.assertTrue(ext.is_relative_to(self.base))

    def test_profile_preserves_other_settings(self):
        user, _ = configure_client(self.base, self.name)
        file = user / 'User/settings.json'
        settings = json.loads(file.read_text())
        settings['editor.tabSize'] = 3
        file.write_text(json.dumps(settings))
        configure_client(self.base, self.name)
        self.assertEqual(json.loads(file.read_text())['editor.tabSize'], 3)

    def launch(self, outputs):
        self.c = SimpleNamespace(vscode_directory=lambda: self.base, log=Mock())
        with patch('app.editor_launch.locate_code', return_value=(Path('Code.exe'), Path('cli.js'))), patch('app.editor_launch.subprocess.run', side_effect=[SimpleNamespace(returncode=0, stdout=x, stderr='') for x in outputs]) as run:
            result = open_client(self.c, self.name)
            return result, run.call_args_list

    def test_existing_dependency_skips_install(self):
        result, calls = self.launch([EXTENSION + '\n', ''])
        self.assertEqual(len(calls), 2)
        self.assertIn('已发起', result)
        self.assertIn('--folder-uri', calls[1].args[0])

    def test_first_launch_installs_only_remote_dependency_locally(self):
        _, calls = self.launch(['', 'installed', ''])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1].args[0][-2:], ['--install-extension', EXTENSION])
        for call in calls:
            self.assertNotIn('shell', call.kwargs)
            self.assertIn('--user-data-dir', call.args[0])
            self.assertEqual(call.kwargs['env']['ELECTRON_RUN_AS_NODE'], '1')
            self.assertNotIn('SSH_AUTH_SOCK', call.kwargs['env'])

    def test_cli_errors_reported_without_success(self):
        c = SimpleNamespace(vscode_directory=lambda: self.base, log=Mock())
        with patch('app.editor_launch.locate_code', return_value=(Path('Code.exe'), Path('cli.js'))), patch('app.editor_launch.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='connection failed')):
            with self.assertRaises(RuntimeError):
                open_client(c, self.name)

    def test_commit_resource_directory_detection(self):
        exe = self.base / 'Code.exe'
        exe.touch()
        cli = self.base / 'commit123/resources/app/out/cli.js'
        cli.parent.mkdir(parents=True)
        cli.touch()
        cmd = self.base / 'bin/code.cmd'
        with patch('app.editor_launch.sys.platform','win32'), patch('app.editor_launch.shutil.which', return_value=str(cmd)):
            self.assertEqual(locate_code(), (exe, cli))


if __name__ == '__main__':
    unittest.main()
