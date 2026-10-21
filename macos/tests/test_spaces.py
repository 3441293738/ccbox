import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.spaces import catalog_rows, snapshot, classify
from app.environments import Environments
from app.storage import atomic_json
from app.lan import access_mounts


def container(project, service, state='running', status='Up 2 minutes', **extra):
    return dict(State=state, Status=status, Labels=f'com.docker.compose.project={project},com.docker.compose.service={service}', **extra)


class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'bundle'
        self.catalog = Environments(self.root)
        self.catalog.base.mkdir(parents=True)
        self.second = self.catalog.create('账号 B')
        for key, project, port in [('default', 'ccbox2-test-a', 6080), (self.second, 'ccbox2-test-b', 6081)]:
            data = self.catalog.path(key)
            atomic_json(data/'settings.json', {'workspace': '/projects/example', 'desktop_port': port, 'projects': []})
            atomic_json(data/'compose.json', {'name': project})
        self.catalog.select('default')

    def read(self, containers):
        run = Mock(return_value='\n'.join(json.dumps(c) for c in containers))
        result = snapshot(self.root, run, lambda:'docker')
        run.assert_called_once_with(['docker','ps','-a','--filter','label=com.docker.compose.project','--format','{{json .}}'], timeout=12)
        return result

    def test_two_accounts_running_and_selection_preserved(self):
        before = self.catalog.file.read_bytes()
        rows = self.read([container(project, service, status='Up 2 minutes (healthy)' if service=='gateway' else 'Up 2 minutes')
                          for project in ('ccbox2-test-a','ccbox2-test-b') for service in ('gateway','sandbox')])
        self.assertEqual([r['state'] for r in rows], ['running','running'])
        self.assertEqual([r['port'] for r in rows], ['6080','6081'])
        self.assertEqual(self.catalog.file.read_bytes(), before)

    def test_other_project_never_contributes_to_account_status(self):
        rows = self.read([container('ccbox2-test-a-extra', 'gateway'), container('ccbox2-unrelated','sandbox')])
        self.assertEqual([r['state'] for r in rows], ['new','new'])

    def test_one_account_failure_does_not_hide_other(self):
        rows = self.read([container('ccbox2-test-a','gateway',status='Up (healthy)'), container('ccbox2-test-a','sandbox'),
                          container('ccbox2-test-b','gateway','exited','Exited (1) 2 minutes ago')])
        self.assertEqual([r['state'] for r in rows], ['running','error'])

    def test_docker_unavailable_is_unknown_not_stopped(self):
        rows = snapshot(self.root, Mock(side_effect=RuntimeError('SECRET diagnostic')), lambda:'docker')
        self.assertTrue(all(r['state']=='unknown' for r in rows))
        self.assertNotIn('SECRET', json.dumps(rows))

    def test_status_does_not_read_vault(self):
        with patch('app.vault.load', side_effect=AssertionError('must not unlock credentials')):
            self.assertEqual(len(self.read([])), 2)

    def test_corrupt_one_space_preserves_other(self):
        (self.catalog.path(self.second)/'settings.json').write_text('broken',encoding='utf-8')
        rows = self.read([])
        self.assertEqual([r['state'] for r in rows], ['new','error'])

    def test_duplicate_identity_not_two_healthy_accounts(self):
        atomic_json(self.catalog.path(self.second)/'compose.json', {'name': 'ccbox2-test-a'})
        self.assertTrue(all(r['invalid'] for r in catalog_rows(self.root)))

    def test_oneoff_not_counted(self):
        c = container('ccbox2-test-a','gateway')
        c['Labels'] += ',com.docker.compose.oneoff=True'
        self.assertEqual(self.read([c])[0]['state'], 'new')

    def test_stopped_and_partial_and_unhealthy(self):
        cases = [('stopped', {'gateway':[container('a','gateway','exited','Exited (0)')]}),
                 ('partial', {'gateway':[container('a','gateway')]}),
                 ('partial', {'gateway':[container('a','gateway',status='Up (health: starting)')], 'sandbox':[container('a','sandbox')]}),
                 ('error', {'gateway':[container('a','gateway',status='Up (unhealthy)')]}),
                 ('error', {'sandbox':[container('a','sandbox'),container('a','sandbox')]})]
        for expected, services in cases:
            with self.subTest(expected=expected, services=services):
                self.assertEqual(classify(services)[0], expected)


class GatewayCompatibilityTests(unittest.TestCase):
    def test_complete_transitive_local_imports_are_readonly_mounts(self):
        mounts = access_mounts(ROOT)
        names = {Path(m['source']).name for m in mounts}
        # Old image only supplied Python. Mounted code must carry all local imports.
        todo, seen = ['gateway.py'], set()
        while todo:
            name = todo.pop()
            if name in seen: continue
            seen.add(name)
            self.assertIn(name, names)
            tree = ast.parse((ROOT/'runtime'/name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                modules = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
                for module in modules:
                    filename = str(module).split('.')[0]+'.py'
                    if (ROOT/'runtime'/filename).is_file(): todo.append(filename)
        self.assertIn('clipboard_transfer.py', seen)
        self.assertTrue(all(m['read_only'] and m['bind']['create_host_path'] is False for m in mounts))
        self.assertTrue(all(m['target'].startswith('/opt/ccbox/') for m in mounts))


if __name__ == '__main__': unittest.main()
