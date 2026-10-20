import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.controller import Controller, DEFAULT
from app.storage import POINTER, atomic_json, data_location, migration_target, copy_verified


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Windows TEMP may use an 8.3 alias; compare canonical fixture paths.
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'bundle'
        self.c = Controller(self.root)
        self.c.data.mkdir(parents=True)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.c.config_file.write_text(json.dumps({**DEFAULT, 'workspace': str(self.project)}))
        self.c.vault_file.write_bytes(b'opaque-test-vault')
        self.c.file.write_text('{"name":"ccbox2-fixture"}')
        self.dest = self.base / 'new data'
        self.cred = {'project': 'ccbox2-fixture', 'proxy': {'host': 'HOST'}, 'desktop_token': 'TOKEN'}
        self.credentials = patch.object(Controller, 'credentials', return_value=self.cred)
        self.credentials.start()
        self.acl = patch('app.storage.vault.protect_directory', side_effect=lambda p: Path(p).mkdir(parents=True, exist_ok=True))
        self.acl.start()

    def tearDown(self):
        self.credentials.stop()
        self.acl.stop()
        self.tmp.cleanup()

    def test_default_and_pointer_resolution(self):
        self.assertEqual(data_location(self.root), self.c.data)
        self.dest.mkdir()
        (self.dest / 'settings.json').write_text('{}')
        atomic_json(self.root / POINTER, {'data_dir': str(self.dest)})
        self.assertEqual(Controller(self.root).data, self.dest)

    def test_invalid_pointer_never_falls_back(self):
        for data in ('invalid', '{}', '[]', '{"data_dir":"relative"}', json.dumps({'data_dir': str(self.dest)})):
            (self.root / POINTER).write_text(data)
            with self.assertRaises(RuntimeError):
                Controller(self.root)

    def test_stale_controller_guard(self):
        self.dest.mkdir()
        (self.dest / 'settings.json').write_text('{}')
        atomic_json(self.root / POINTER, {'data_dir': str(self.dest)})
        with self.assertRaises(RuntimeError):
            self.c.check_location()

    def test_overlap_targets_rejected(self):
        for target in (self.base, self.root, self.c.data, self.c.data / 'nested', self.project, self.project / 'data'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                migration_target(str(target), self.c.data, self.project, self.root)

    def test_nonempty_target_is_untouched(self):
        self.dest.mkdir()
        (self.dest / 'keep').write_text('unchanged')
        with self.assertRaises(ValueError):
            migration_target(str(self.dest), self.c.data, self.project, self.root)
        self.assertEqual((self.dest / 'keep').read_text(), 'unchanged')

    def test_copy_excludes_plaintext_and_preserves_source(self):
        (self.c.data / 'active').mkdir()
        (self.c.data / 'active/upstream.json').write_text('SECRET')
        count = copy_verified(self.c.data, self.dest)
        self.assertEqual(count, 3)
        self.assertFalse((self.dest / 'active').exists())
        self.assertEqual((self.dest / 'credentials.dpapi').read_bytes(), self.c.vault_file.read_bytes())
        self.assertTrue(self.c.vault_file.exists())

    def test_checksum_error_preserves_source(self):
        with patch('app.storage.file_digest', side_effect=['a', 'b']):
            with self.assertRaises(RuntimeError):
                copy_verified(self.c.data, self.dest)
        self.assertTrue(self.c.config_file.exists())

    def test_migration_stopped_retains_id_and_backup(self):
        old = self.c.data
        with patch.object(self.c, 'run', return_value=''), patch.object(self.c, 'docker', return_value='docker'), patch.object(self.c, 'start') as start:
            self.c.migrate_data(str(self.dest))
        start.assert_not_called()
        self.assertEqual(Controller(self.root).data, self.dest)
        self.assertTrue((old / 'credentials.dpapi').exists())
        self.assertEqual(json.loads(self.c.file.read_text())['name'], self.cred['project'])
        self.assertEqual(json.loads(self.c.file.read_text())['volumes'], {'home': {}, 'ipc': {}, 'desktop': {}})

    def test_running_migration_restores_running_state(self):
        with patch.object(self.c, 'run', return_value='container-id'), patch.object(self.c, 'docker', return_value='docker'), patch.object(self.c, 'stop') as stop, patch.object(self.c, 'start') as start:
            self.c.migrate_data(str(self.dest))
        stop.assert_called_once()
        start.assert_called_once()

    def test_migration_rewrites_internal_editor_path(self):
        cfg = self.c.config()
        cfg['vscode_dir'] = str(self.c.data / 'editor')
        atomic_json(self.c.config_file, cfg)
        self.c.generate_devcontainer()
        with patch.object(self.c, 'run', return_value=''), patch.object(self.c, 'docker', return_value='docker'):
            self.c.migrate_data(str(self.dest))
        self.assertEqual(self.c.config()['vscode_dir'], str(self.dest / 'editor'))
        dev = json.loads((self.dest / 'editor/.devcontainer/devcontainer.json').read_text())
        self.assertEqual(dev['dockerComposeFile'], str(self.dest / 'compose.json'))

    def test_copy_failure_rolls_back_and_restarts_old(self):
        old = self.c.data
        with patch.object(self.c, 'run', return_value='id'), patch.object(self.c, 'docker', return_value='docker'), patch.object(self.c, 'stop'), patch.object(self.c, 'start') as start, patch('app.controller.copy_verified', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.c.migrate_data(str(self.dest))
        self.assertEqual(self.c.data, old)
        self.assertFalse((self.root / POINTER).exists())
        start.assert_called_once()

    def test_failed_new_start_rolls_back_pointer(self):
        old = self.c.data
        with patch.object(self.c, 'run', return_value='id'), patch.object(self.c, 'docker', return_value='docker'), patch.object(self.c, 'stop'), patch.object(self.c, 'start', side_effect=[RuntimeError('start failed'), 'recovered']) as start:
            with self.assertRaises(RuntimeError):
                self.c.migrate_data(str(self.dest))
        self.assertEqual(start.call_count, 2)
        self.assertEqual(data_location(self.root), old)
        self.assertEqual(self.c.data, old)

    def test_relocated_data_cannot_be_shared_as_workspace(self):
        self.c._set_data(self.project / 'private')
        with self.assertRaises(ValueError):
            self.c.validate_config({**DEFAULT, 'workspace': str(self.project)})

    def test_vault_decryption_error_does_not_rotate_project(self):
        before = self.c.config_file.read_bytes(), self.c.vault_file.read_bytes()
        with patch.object(self.c, 'credentials', side_effect=RuntimeError('DPAPI')):
            with self.assertRaises(RuntimeError):
                self.c.save(self.c.config(), {'scheme': 'http', 'host': 'HOST', 'port': 1080, 'username': '', 'password': ''})
        self.assertEqual(before, (self.c.config_file.read_bytes(), self.c.vault_file.read_bytes()))


if __name__ == '__main__':
    unittest.main()
