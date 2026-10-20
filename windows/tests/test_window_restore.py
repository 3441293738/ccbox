import json
import os
import shlex
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'runtime'))
import window_restore as restore
from app import sandbox_editor as host


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.template = self.home/'default.xml'
        self.template.write_text(f'<openbox_config xmlns="{restore.NS}"><keyboard><keybind key="A-Tab"><action name="NextWindow"/></keybind></keyboard></openbox_config>')
        self.file = self.home/'.config/openbox/rc.xml'

    def install(self):
        restore.install_binding(self.home, self.template)

    @patch('window_restore.subprocess.run', return_value=SimpleNamespace(returncode=0))
    def test_install_is_idempotent_and_preserves_existing_bindings(self, run):
        self.install()
        first = self.file.read_bytes()
        self.install()
        self.assertEqual(self.file.read_bytes(), first)
        root = ET.fromstring(first)
        keys = root.findall(f'.//{{{restore.NS}}}keybind')
        self.assertEqual([k.get('key') for k in keys], ['A-Tab', restore.KEY])
        self.assertIn(b'window_restore.py', first)
        self.assertEqual(run.call_args.args[0], ['openbox', '--reconfigure'])

    def test_reserved_binding_conflict_leaves_original_unchanged(self):
        self.file.parent.mkdir(parents=True)
        original = self.template.read_text().replace('A-Tab', restore.KEY).encode()
        self.file.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, '占用'):
            self.install()
        self.assertEqual(self.file.read_bytes(), original)

    @patch('window_restore.subprocess.run', return_value=SimpleNamespace(returncode=0))
    def test_migrates_only_our_old_binding(self, run):
        self.file.parent.mkdir(parents=True)
        xml = self.template.read_text().replace('</keyboard>', '<keybind key="C-A-S-F8" data-ccbox="restore-editor"/><keybind key="C-A-F8"><action name="NextWindow"/></keybind></keyboard>')
        self.file.write_text(xml)
        self.install()
        keys = ET.fromstring(self.file.read_bytes()).findall(f'.//{{{restore.NS}}}keybind')
        self.assertEqual([k.get('key') for k in keys], ['A-Tab', 'C-A-F8', restore.KEY])

    def test_shortcut_avoids_x11_virtual_terminal_switch(self):
        self.assertEqual(restore.KEY, 'C-F8')

    def test_invalid_xml_is_not_overwritten(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'<malformed')
        with self.assertRaises(ET.ParseError):
            self.install()
        self.assertEqual(self.file.read_bytes(), b'<malformed')

    @patch('window_restore.subprocess.run', return_value=SimpleNamespace(returncode=1))
    def test_failed_reconfigure_restores_previous_file(self, run):
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(self.template.read_bytes())
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.file.read_bytes(), self.template.read_bytes())
        self.assertEqual(self.file.with_name('rc.xml.before-ccbox-restore').read_bytes(), self.template.read_bytes())


class IdentityTests(unittest.TestCase):
    def check(self, args, exe='/managed/install/code', owner=1000):
        with patch('window_restore.os.readlink', return_value=exe), \
             patch('window_restore.os.getuid', return_value=1000, create=True), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=owner)), \
             patch.object(Path, 'read_bytes', return_value=b'\0'.join(a.encode() for a in args)):
            return restore.managed_pid(123, base=Path('/managed'))

    def args(self):
        base = Path('/managed')
        return ['code', '--user-data-dir', str(base/'user-data'), '--extensions-dir', str(base/'extensions')]

    def test_matches_exact_profile(self):
        self.assertTrue(self.check(self.args(), exe=str(Path('/managed/install/code'))))

    def test_electron_rewritten_title_matches_exact_profile(self):
        self.assertTrue(self.check([shlex.join(self.args())], exe=str(Path('/managed/install/code'))))

    def test_rewritten_other_profile_rejected(self):
        args = self.args()
        args[2] += '-other'
        self.assertFalse(self.check([shlex.join(args)], exe=str(Path('/managed/install/code'))))

    def test_electron_reordered_positional_values(self):
        a = self.args()
        title = shlex.join([a[0], a[1], a[3], '--reuse-window', a[2], a[4], '/workspace'])
        self.assertTrue(self.check([title], exe=str(Path('/managed/install/code'))))

    def test_other_profile_rejected(self):
        self.assertFalse(self.check(['code', '--user-data-dir', '/managed/user-data-other', '--extensions-dir', '/managed/extensions'], exe=str(Path('/managed/install/code'))))

    def test_other_executable_rejected(self):
        self.assertFalse(self.check(self.args(), exe='/usr/bin/firefox'))

    def test_other_user_rejected(self):
        self.assertFalse(self.check(self.args(), exe=str(Path('/managed/install/code')), owner=1001))

    def test_renderer_is_not_main(self):
        self.assertFalse(self.check(self.args()+['--type=renderer'], exe=str(Path('/managed/install/code'))))

    def test_missing_pid_is_ignored(self):
        self.assertFalse(restore.managed_pid(999999999))


class RestoreTests(unittest.TestCase):
    @patch('window_restore.X11')
    def test_activates_only_matching_window_and_preserves_geometry(self, cls):
        x = cls.return_value
        x.root = 1
        properties = {(1, '_NET_CLIENT_LIST_STACKING'):[8, 9], (8, '_NET_WM_PID'):[100],
                      (9, '_NET_WM_PID'):[200], (8, '_NET_WM_DESKTOP'):[2]}
        x.values.side_effect = lambda w, n: properties.get((w, n), [])
        self.assertTrue(restore.restore(matches=lambda pid: pid == 100))
        x.lib.XMapRaised.assert_called_once_with(x.display, 8)
        x.send.assert_any_call(8, '_NET_ACTIVE_WINDOW', 2, 0, 0)
        x.send.assert_any_call(1, '_NET_CURRENT_DESKTOP', 2, 0)
        x.close.assert_called_once()

    @patch('window_restore.X11')
    def test_closed_editor_is_noop_not_new_process(self, cls):
        cls.return_value.values.return_value = []
        with patch('window_restore.subprocess.run') as run:
            self.assertFalse(restore.restore())
        run.assert_not_called()
        cls.return_value.send.assert_not_called()
        cls.return_value.close.assert_called_once()


class ViewerUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.c = Mock(root=ROOT, file=folder/'compose.json')
        self.c.command.side_effect = lambda *a: ['docker', 'compose', *a]
        self.c.docker.return_value = 'docker'
        self.original = {'name':'fixture', 'services':{'gateway':{'volumes':['ipc:/ipc']}, 'sandbox':{'unchanged':True}}}
        self.c.file.write_text(json.dumps(self.original))
        self.c.run.side_effect = ['gateway-id', json.dumps([{'Mounts':[]}]), 'OK']

    def test_existing_gateway_requires_consent_before_reconnect(self):
        with self.assertRaisesRegex(RuntimeError, '确认短暂重连'):
            host.ensure_viewer_mount(self.c)
        self.assertEqual(json.loads(self.c.file.read_text()), self.original)
        self.assertEqual(self.c.run.call_count, 2)

    def test_update_changes_only_gateway_static_file_bind(self):
        host.ensure_viewer_mount(self.c, True)
        config = json.loads(self.c.file.read_text())
        self.assertEqual(config['services']['sandbox'], self.original['services']['sandbox'])
        bind = config['services']['gateway']['volumes'][-1]
        self.assertEqual(bind['target'], '/opt/ccbox/desktop.html')
        self.assertTrue(bind['read_only'])
        args = self.c.run.call_args.args[0]
        self.assertIn('--no-deps', args)
        self.assertIn('--no-build', args)
        self.assertEqual(args[-1], 'gateway')
        self.c.stop.assert_not_called()

    def test_failed_update_restores_compose(self):
        self.c.run.side_effect = ['gateway-id', json.dumps([{'Mounts':[]}]), RuntimeError('fixture'), 'OK']
        with self.assertRaisesRegex(RuntimeError, '已恢复原网关配置'):
            host.ensure_viewer_mount(self.c, True)
        self.assertEqual(json.loads(self.c.file.read_text()), self.original)

    def test_updated_gateway_needs_no_restart(self):
        host.ensure_viewer_mount(self.c, True)
        self.c.run.reset_mock(side_effect=True)
        self.c.run.side_effect = ['gateway-id', json.dumps([{'Mounts':[{'Destination':'/opt/ccbox/desktop.html','Type':'bind','RW':False}]}])]
        host.ensure_viewer_mount(self.c)
        self.assertEqual(self.c.run.call_count, 2)

    def test_deploy_helper_first_and_abort_on_failure(self):
        with patch('app.sandbox_editor.subprocess.run', return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                host.deploy_runtime(self.c)
        self.assertEqual(run.call_count, 1)
        self.assertIn('window_restore.py', run.call_args.args[0][-1])

    def test_viewer_has_fixed_shortcut_and_honest_feedback(self):
        html = (ROOT/'runtime/desktop.html').read_text(encoding='utf-8')
        self.assertIn('id="restore" disabled', html)
        self.assertIn('0xffc5', html)
        self.assertIn('held.reverse()', html)
        self.assertIn('aria-busy', html)
        self.assertIn('已发送恢复请求', html)
        self.assertNotIn('已恢复窗口', html)
        self.assertIn("fetch('/clipboard/'+kind", html)
        self.assertIn("$('image-paste').addEventListener('paste'", html)
        self.assertIn('kind===\'file\'', html)


if __name__ == '__main__':
    unittest.main()
