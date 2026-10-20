import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
from app.controller import Controller, DEFAULT, make_compose, parse_proxy
from app.environments import Environments, CATALOG
from app.projects import mounts, signature, workspace_document
from app.storage import atomic_json, copy_verified, POINTER
from app import vault
from gateway import Gateway

PROXY = parse_proxy('socks5h://SERIAL:TOKEN@HOST:1080')


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'bundle'
        self.project = self.base / 'project'
        self.project.mkdir()
        self.extra = self.base / '多项目 & two'
        self.extra.mkdir()
        self.c = Controller(self.root)
        self.cfg = {**DEFAULT, 'workspace': str(self.project)}
        self.p = {'id': 'p-123456789abc', 'name': '第二项目', 'path': str(self.extra), 'readonly': True}
        self.c.save(self.cfg, PROXY)
        self.catalog = Environments(self.root)

    def new(self):
        key = self.catalog.create('账号 B')
        c = Controller(self.root, key)
        c.save({**self.cfg, 'desktop_port': c.config()['desktop_port']}, PROXY)
        return c

    def test_legacy_read_does_not_rewrite_data(self):
        before = self.c.vault_file.read_bytes(), self.c.config_file.read_bytes()
        c = Controller(self.root)
        self.assertEqual(c.environment_id, 'default')
        self.assertEqual(c.environment_name, '默认环境')
        self.assertEqual(before, (c.vault_file.read_bytes(), c.config_file.read_bytes()))
        self.assertFalse(self.catalog.file.exists())

    def test_create_is_blank_and_port_distinct(self):
        key = self.catalog.create('账号 B')
        c = Controller(self.root)
        self.assertEqual(c.environment_id, key)
        self.assertEqual(c.credentials(), {})
        self.assertEqual(c.config()['workspace'], '')
        self.assertEqual(c.config()['projects'], [])
        self.assertEqual(c.config()['vscode_dir'], '')
        self.assertEqual(c.config()['desktop_port'], 6081)

    def test_switch_and_rename_preserve_vault_and_project(self):
        other = self.new()
        before = self.c.vault_file.read_bytes(), other.vault_file.read_bytes()
        self.catalog.rename(other.environment_id, '工作 B')
        self.catalog.select('default')
        self.assertEqual(Controller(self.root).data, self.c.data)
        self.assertEqual(other.environment_name, '工作 B')
        self.assertEqual(before, (self.c.vault_file.read_bytes(), other.vault_file.read_bytes()))

    def test_independent_vault_tokens_projects_and_profiles(self):
        other = self.new()
        self.assertNotEqual(self.c.credentials()['project'], other.credentials()['project'])
        self.assertNotEqual(self.c.credentials()['desktop_token'], other.credentials()['desktop_token'])
        shared_base = self.base / 'editor'
        for c in [self.c, other]:
            c.save({**c.config(), 'vscode_dir': str(shared_base)}, PROXY)
        self.assertEqual(self.c.vscode_directory(), shared_base)
        self.assertEqual(other.vscode_directory(), shared_base / 'environments' / other.environment_id)
        self.assertNotIn(b'TOKEN', other.vault_file.read_bytes())

    def test_profile_collision_rejected(self):
        other = self.new()
        with self.assertRaisesRegex(ValueError, '共用 VS Code'):
            self.c.validate_config({**self.cfg, 'vscode_dir': str(other.vscode_directory())})

    def test_duplicate_port_prevents_save(self):
        other = self.new()
        before = other.vault_file.read_bytes()
        with self.assertRaisesRegex(ValueError, '端口'):
            other.save({**other.config(), 'desktop_port': 6080}, PROXY)
        self.assertEqual(before, other.vault_file.read_bytes())

    def test_duplicate_project_id_fails_closed(self):
        other = self.new()
        vault.save(other.vault_file, self.c.credentials())
        with self.assertRaisesRegex(RuntimeError, '同一 Docker'):
            other.save(other.config(), PROXY)

    def test_bad_names_and_duplicate_names(self):
        for name in ['', '../', 'a\nB', '${rootName}', '默认环境', 'x' * 33]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.catalog.create(name)

    def test_catalog_corruption_never_defaults(self):
        self.catalog.file.write_text('broken')
        with self.assertRaises(RuntimeError):
            Controller(self.root)

    def test_catalog_traversal_blocked(self):
        atomic_json(self.catalog.file, {'version': 1, 'selected': '../outside', 'items': [{'id': 'default','name':'A'},{'id':'../outside','name':'B'}]})
        with self.assertRaises(RuntimeError):
            Controller(self.root)

    def test_missing_environment_settings_not_recreated(self):
        other = self.new()
        other.config_file.unlink()
        with self.assertRaisesRegex(RuntimeError, '设置文件缺失'):
            Controller(self.root)

    def test_multiple_mounts_and_workspace_document(self):
        c = self.c.validate_config({**self.cfg, 'projects': [self.p]})
        compose = make_compose(self.root, self.c.data, c, 'ccbox2-fixture')
        sandbox = compose['services']['sandbox']
        self.assertEqual(sandbox['network_mode'], 'none')
        self.assertEqual(sandbox['labels']['io.ccbox.mounts'], signature(c))
        extra = next(m for m in sandbox['volumes'] if isinstance(m, dict) and m['target'].startswith('/projects/'))
        self.assertTrue(extra['read_only'])
        self.assertEqual(extra['source'], str(self.extra))
        self.assertEqual(workspace_document(c)['folders'][1]['path'], '/projects/' + self.p['id'])

    def test_ids_and_names_not_injected_in_mount_target(self):
        for key in ['../home/claude', 'p-123', 'p-123456789abc:rw']:
            with self.assertRaises(ValueError):
                self.c.validate_config({**self.cfg, 'projects': [{**self.p, 'id': key}]})

    def test_duplicate_overlapping_and_missing_projects_rejected(self):
        child = self.extra / 'child'
        child.mkdir()
        for p in [self.project, self.project / 'missing', self.root, self.c.data, Path.home(), self.c.data / 'environments']:
            with self.subTest(p=p), self.assertRaises(ValueError):
                self.c.validate_config({**self.cfg, 'projects': [{**self.p, 'path': str(p)}]})
        for p in [self.extra, child]:
            with self.assertRaises(ValueError):
                self.c.validate_config({**self.cfg, 'projects': [self.p, {**self.p, 'id':'p-000000000001','name':'Other','path':str(p)}]})

    def test_other_editor_profile_cannot_be_mounted(self):
        other = self.new()
        folder = self.base / 'editor'
        folder.mkdir()
        other.save({**other.config(), 'vscode_dir': str(folder)}, PROXY)
        with self.assertRaises(ValueError):
            self.c.validate_config({**self.cfg, 'projects': [{**self.p, 'path': str(folder)}]})

    def test_proxy_and_volumes_are_environment_scoped(self):
        other = self.new()
        a = make_compose(self.root, self.c.data, self.c.config(), self.c.credentials()['project'])
        b = make_compose(self.root, other.data, other.config(), other.credentials()['project'])
        self.assertNotEqual(a['name'], b['name'])
        self.assertNotEqual(a['services']['gateway']['volumes'][-1]['source'], b['services']['gateway']['volumes'][-1]['source'])
        self.assertNotEqual(a['services']['gateway']['ports'], b['services']['gateway']['ports'])
        for x in (a, b):
            self.assertEqual(x['volumes'], {'home': {}, 'ipc': {}, 'desktop': {}})
            self.assertNotIn('external', json.dumps(x['volumes']))
            self.assertNotIn('TOKEN', json.dumps(x))

    def test_nested_runtime_plaintext_not_migrated(self):
        other = self.new()
        for c in (self.c, other):
            (c.data / 'active').mkdir()
            (c.data / 'active/upstream.json').write_text('SECRET')
        dest = self.base / 'copy'
        copy_verified(self.c.data_root, dest)
        self.assertFalse(list(dest.rglob('upstream.json')))
        self.assertTrue((dest / 'environments' / other.environment_id / 'credentials.dpapi').exists())

    def test_running_multi_migration_rejected_without_stopping_any(self):
        other = self.new()
        atomic_json(other.file, {'name': other.credentials()['project']})
        with patch.object(Controller,'docker',return_value='docker'), patch.object(Controller,'run',return_value='id'), patch.object(Controller,'stop') as stop:
            with self.assertRaisesRegex(RuntimeError, '逐一'):
                other.migrate_data(str(self.base / 'new data'))
        stop.assert_not_called()
        self.assertFalse((self.root / POINTER).exists())

    def test_all_environment_migration_keeps_identity_and_selection(self):
        other = self.new()
        before = [c.credentials() for c in (self.c, other)]
        for c in (self.c, other):
            atomic_json(c.file, make_compose(self.root,c.data,c.config(),c.credentials()['project']))
        dest = self.base / 'new data'
        with patch.object(Controller,'docker',return_value='docker'), patch.object(Controller,'run',return_value=''):
            other.migrate_data(str(dest))
        self.assertEqual(Controller(self.root).environment_id, other.environment_id)
        for old, cred in zip((self.c, other), before):
            new = Controller(self.root, old.environment_id)
            self.assertTrue(new.data.is_relative_to(dest))
            self.assertEqual(new.credentials(), cred)
            self.assertEqual(json.loads(new.file.read_text())['name'], cred['project'])
        self.assertTrue((self.catalog.base / CATALOG).exists())

    def test_cookie_namespace_does_not_overlap_and_wrong_env_token_fails(self):
        a, b = Gateway({}, 'A', 6080), Gateway({}, 'B', 6081)
        cookie = 'ccbox_session_6080=A; ccbox_session_6081=B'
        for g in (a,b):
            self.assertEqual(g.desktop_auth('/websockify',[('Host',f'127.0.0.1:{g.desktop_port}'),('Cookie',cookie)]),(True,False))
        self.assertEqual(b.desktop_auth('/vnc.html?access=A',[('Host','127.0.0.1:6081')]),(False,False))

    def test_wrong_compose_environment_is_never_operated(self):
        atomic_json(self.c.file, {'name': 'ccbox2-another'})
        with self.assertRaisesRegex(RuntimeError, '身份不一致'):
            self.c.command('stop')

    def test_missing_vault_does_not_create_new_account_volume(self):
        atomic_json(self.c.file, {'name': self.c.credentials()['project']})
        self.c.vault_file.unlink()
        with self.assertRaises(RuntimeError):
            self.c.save(self.cfg, PROXY)
        self.assertFalse(self.c.vault_file.exists())


if __name__ == '__main__':
    unittest.main()
