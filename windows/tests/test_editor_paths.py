import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.controller import Controller, DEFAULT, make_compose, validate
from app.editor_paths import EditorDirectoryError, normalize_editor_directory


class EditorPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.controller = Controller(self.base / 'bundle')
        self.controller.data.mkdir(parents=True)
        self.controller.file.write_text('{"name":"test"}', encoding='utf-8')
        self.workspace = self.base / 'project'
        self.workspace.mkdir()
        self.config = {**DEFAULT, 'workspace': str(self.workspace)}
        self.custom = self.base / 'another disk' / '连接 & 配置'

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, directory):
        self.config['vscode_dir'] = str(directory)
        self.controller.config_file.write_text(json.dumps(self.config), encoding='utf-8')

    def test_legacy_settings_keep_default_directory(self):
        self.controller.config_file.write_text(json.dumps({'workspace': str(self.workspace)}))
        self.assertEqual(self.controller.config()['vscode_dir'], '')
        self.assertEqual(self.controller.vscode_directory(), self.controller.data / 'vscode')

    def test_default_generation(self):
        folder = self.controller.generate_devcontainer()
        self.assertEqual(folder, self.controller.data / 'vscode')
        self.assertTrue((folder / '.devcontainer/devcontainer.json').is_file())

    def test_absolute_spaces_unicode_and_missing_directory(self):
        value = '  ' + str(self.custom) + '  '
        self.assertEqual(normalize_editor_directory(value), str(self.custom))
        self.assertFalse(self.custom.exists())  # Validation does not create directories.
        self.assertEqual(validate({**self.config, 'vscode_dir': value})['vscode_dir'], str(self.custom))

    def test_invalid_paths_have_field_key(self):
        for value in ('relative/path', str(Path(self.base.anchor)), str(Path.home()), 'x\nfile', None):
            with self.subTest(value=value), self.assertRaises(EditorDirectoryError) as caught:
                normalize_editor_directory(value)
            self.assertEqual(caught.exception.key, 'vscode_dir')

    def test_file_and_file_parent_are_rejected(self):
        f = self.base / 'not-a-directory'
        f.write_text('untouched')
        for value in (f, f / 'child'):
            with self.assertRaises(EditorDirectoryError):
                normalize_editor_directory(str(value))
        self.assertEqual(f.read_text(), 'untouched')

    @unittest.skipUnless(os.name == 'nt', 'Windows path rules')
    def test_invalid_windows_names_are_rejected(self):
        for name in ('bad:name', 'bad?name', 'name.', 'CON', 'AUX.txt'):
            with self.subTest(name=name), self.assertRaises(EditorDirectoryError):
                normalize_editor_directory(str(self.base / name))

    def test_save_and_reload_custom_path(self):
        from app.controller import parse_proxy
        c = {**self.config, 'vscode_dir': str(self.custom)}
        with patch('app.controller.vault.protect_directory'), patch('app.controller.vault.save'), patch.object(self.controller, 'credentials', return_value={}):
            result = self.controller.save(c, parse_proxy('http://HOST:1080'))
        self.assertEqual(Controller(self.controller.root).config()['vscode_dir'], str(self.custom))
        self.assertIn('无需重启容器', result)

    def test_custom_generation_preserves_proxy_and_workspace_boundary(self):
        self.store(self.custom)
        folder = self.controller.generate_devcontainer()
        config = json.loads((folder / '.devcontainer/devcontainer.json').read_text(encoding='utf-8'))
        settings = json.loads((folder / '.vscode/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(config['dockerComposeFile'], str(self.controller.file))
        self.assertEqual(config['workspaceFolder'], '/workspace')
        self.assertEqual(config['remoteUser'], 'claude')
        self.assertEqual(config['remoteEnv']['HTTPS_PROXY'], 'http://127.0.0.1:18080')
        self.assertEqual(config['remoteEnv']['SSH_AUTH_SOCK'], '')
        self.assertIn('anthropic.claude-code', config['customizations']['vscode']['extensions'])
        self.assertFalse(settings['dev.containers.copyGitConfig'])
        self.assertEqual(settings['dev.containers.gitCredentialHelperConfigLocation'], 'none')
        self.assertFalse((self.controller.data / 'vscode').exists())

    def test_editor_directory_does_not_change_compose(self):
        before = make_compose(ROOT, self.controller.data, self.config, 'ccbox2-test')
        after = make_compose(ROOT, self.controller.data, {**self.config, 'vscode_dir': str(self.custom)}, 'ccbox2-test')
        self.assertEqual(before, after)

    def test_changing_location_preserves_old_files_and_vault(self):
        old = self.controller.generate_devcontainer()
        dev = old / '.devcontainer/devcontainer.json'
        before = dev.read_bytes()
        self.controller.vault_file.write_bytes(b'opaque encrypted vault')
        compose_before = self.controller.file.read_bytes()
        self.store(self.custom)
        with patch.object(self.controller, 'run', side_effect=AssertionError('No Docker calls expected')):
            self.controller.generate_devcontainer()
        self.assertEqual(dev.read_bytes(), before)
        self.assertEqual(self.controller.file.read_bytes(), compose_before)
        self.assertEqual(self.controller.vault_file.read_bytes(), b'opaque encrypted vault')

    def test_other_devcontainer_config_is_not_overwritten(self):
        self.store(self.custom)
        p = self.custom / '.devcontainer/devcontainer.json'
        p.parent.mkdir(parents=True)
        p.write_text('{"name":"my project"}')
        before = p.read_bytes()
        with self.assertRaises(EditorDirectoryError):
            self.controller.generate_devcontainer()
        self.assertEqual(p.read_bytes(), before)
        self.assertFalse((self.custom / '.vscode').exists())

    def test_settings_without_owned_devcontainer_are_preserved(self):
        self.store(self.custom)
        p = self.custom / '.vscode/settings.json'
        p.parent.mkdir(parents=True)
        p.write_text('{"editor.tabSize":2}')
        with self.assertRaises(EditorDirectoryError):
            self.controller.generate_devcontainer()
        self.assertEqual(p.read_text(), '{"editor.tabSize":2}')
        self.assertFalse((self.custom / '.devcontainer').exists())

    def test_different_environment_config_is_preserved(self):
        self.store(self.custom)
        self.controller.generate_devcontainer()
        other = Controller(self.base / 'another-bundle')
        other.data.mkdir(parents=True)
        other.file.write_text('{}')
        other.config_file.write_text(json.dumps({'vscode_dir': str(self.custom)}))
        with self.assertRaises(EditorDirectoryError):
            other.generate_devcontainer()

    def test_owned_changed_config_is_backed_up(self):
        self.store(self.custom)
        self.controller.generate_devcontainer()
        p = self.custom / '.vscode/settings.json'
        old = b'{"editor.tabSize": 7}'
        p.write_bytes(old)
        self.controller.generate_devcontainer()
        copies = list((self.custom / '.ccbox-backups').glob('*/.vscode/settings.json'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), old)

    def test_identical_generation_does_not_make_backups(self):
        self.store(self.custom)
        self.controller.generate_devcontainer()
        self.controller.generate_devcontainer()
        self.assertFalse((self.custom / '.ccbox-backups').exists())

    def test_reset_to_default_does_not_remove_custom_folder(self):
        self.store(self.custom)
        self.controller.generate_devcontainer()
        self.store('')
        self.assertEqual(self.controller.generate_devcontainer(), self.controller.data / 'vscode')
        self.assertTrue((self.custom / '.devcontainer/devcontainer.json').is_file())

    def test_existing_directory_in_place_of_config_is_rejected(self):
        self.store(self.custom)
        p = self.custom / '.vscode/settings.json'
        p.mkdir(parents=True)
        with self.assertRaises(EditorDirectoryError):
            self.controller.generate_devcontainer()
        self.assertFalse((self.custom / '.devcontainer').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
