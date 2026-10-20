"""Regression coverage for missing local images and compact Docker errors."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.controller import Controller
from app.feedback import compact_notice


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = Controller(self.tmp.name)
        self.c.data.mkdir()
        self.c.file.write_text(json.dumps({'name': 'ccbox2-test', 'services': {
            name: {'image': f'ccbox2-test-{name}'} for name in ('gateway', 'sandbox')
        }}), encoding='utf-8')
        self.active = self.c.data / 'active/upstream.json'
        self.active.parent.mkdir()
        self.active.write_text('TEST SECRET', encoding='utf-8')
        self.images = {'gateway', 'sandbox'}
        self.commands = []
        self.logs = []
        self.c.log = self.logs.append
        for name, kwargs in [('docker', {'return_value': 'docker'}),
                             ('credentials', {'return_value': {'project': 'ccbox2-test'}}),
                             ('doctor', {'return_value': 'Docker ready'}),
                             ('prepare', {'return_value': {}}),
                             ('run', {'side_effect': self.run_command})]:
            p = patch.object(self.c, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def run_command(self, args, **kwargs):
        self.commands.append(args)
        if args[1:3] == ['image', 'ls']:
            service = args[-1].split('-')[-1].removesuffix(':latest')
            return 'sha256:TEST' if service in self.images else ''
        return ''

    def fake_build(self, services):
        self.assertTrue(self.active.exists(), 'start credentials must survive the build')
        self.images.update(services)

    def test_missing_both_auto_builds_before_start(self):
        self.images.clear()
        with patch.object(self.c, 'build_images', side_effect=self.fake_build) as build:
            self.c.start()
        build.assert_called_once_with(['gateway', 'sandbox'])
        self.c.prepare.assert_called_once()
        self.assertTrue(self.active.exists())
        up = next(args for args in self.commands if 'up' in args)
        self.assertIn('--no-build', up)
        self.assertEqual(up[up.index('--pull') + 1], 'never')
        self.assertIn('--wait', up)

    def test_only_missing_service_is_built(self):
        self.images.remove('sandbox')
        with patch.object(self.c, 'build_images', side_effect=self.fake_build) as build:
            self.c.start()
        build.assert_called_once_with(['sandbox'])

    def test_existing_images_skip_build(self):
        with patch.object(self.c, 'build_images') as build:
            self.c.start()
        build.assert_not_called()
        self.assertTrue(any('跳过构建' in log for log in self.logs))

    def test_empty_listing_is_missing_not_inspect_error(self):
        self.images.clear()
        self.assertEqual(self.c.missing_images(), ['gateway', 'sandbox'])
        self.assertTrue(all('--quiet' in args for args in self.commands))
        self.assertEqual(self.commands[0][-1], 'reference=ccbox2-test-gateway:latest')

    def test_daemon_error_is_not_misclassified_as_missing(self):
        self.c.run.side_effect = RuntimeError('daemon unavailable')
        with patch.object(self.c, 'build_images') as build, self.assertRaisesRegex(RuntimeError, 'daemon'):
            self.c.start()
        build.assert_not_called()
        self.assertTrue(self.active.exists(), 'failed cleanup must preserve active credentials')

    def test_failed_build_never_attempts_up_and_cleans_secret(self):
        self.images.clear()
        with patch.object(self.c, 'build_images', side_effect=RuntimeError('build failed')):
            with self.assertRaisesRegex(RuntimeError, 'build failed'):
                self.c.start()
        self.assertFalse(any('up' in args for args in self.commands))
        stop = next(args for args in self.commands if 'stop' in args)
        self.assertNotIn('-v', stop)
        self.assertEqual(self.active.read_bytes(), b'')

    def test_failed_up_retains_secret_if_cleanup_failed(self):
        def run(args, **kwargs):
            if 'up' in args:
                raise RuntimeError('start failed')
            return self.run_command(args, **kwargs)
        self.c.run.side_effect = run
        with patch.object(self.c, 'stop', side_effect=RuntimeError('stop failed')):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                self.c.start()
        self.assertTrue(self.active.exists())

    def test_running_environment_error_does_not_stop_it(self):
        self.c.prepare.side_effect = RuntimeError('already running')
        with patch.object(self.c, 'stop') as stop, self.assertRaisesRegex(RuntimeError, 'already running'):
            self.c.start()
        stop.assert_not_called()

    def mock_process(self, code=0):
        p = MagicMock()
        p.__enter__.return_value = p
        p.stdout = iter(['step one\n', 'step two\n'])
        p.wait.return_value = code
        return p

    def test_streamed_build_preserves_startup_secret(self):
        with patch('app.controller.subprocess.Popen', return_value=self.mock_process()) as popen:
            self.c.build_images(['gateway'])
        self.assertEqual(popen.call_args.args[0][-4:], ['--progress', 'plain', 'build', 'gateway'])
        self.assertIn('step one', self.logs)
        self.assertIn('step two', self.logs)
        self.assertTrue(self.active.exists())

    def test_streamed_build_failure_stops_before_up(self):
        with patch('app.controller.subprocess.Popen', return_value=self.mock_process(1)):
            with self.assertRaisesRegex(RuntimeError, '构建失败'):
                self.c.build_images()
        self.assertEqual(self.commands, [])

    def test_successful_build_must_produce_images(self):
        self.images.clear()
        with patch('app.controller.subprocess.Popen', return_value=self.mock_process()):
            with self.assertRaisesRegex(RuntimeError, '仍缺失'):
                self.c.build_images()

    def test_explicit_build_cleans_runtime_secret_on_success_and_failure(self):
        for error in (None, RuntimeError('build failed')):
            with self.subTest(error=error):
                self.active.write_text('TEST SECRET')
                with patch.object(self.c, 'build_images', side_effect=error):
                    if error:
                        with self.assertRaises(RuntimeError):
                            self.c.build()
                    else:
                        self.c.build()
                self.assertEqual(self.active.read_bytes(), b'')


class CommandErrorTests(unittest.TestCase):
    def test_image_listing_does_not_hide_daemon_failure(self):
        c = Controller()
        result = subprocess.CompletedProcess([], 1, '', 'error from Docker daemon')
        with patch('app.controller.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'Docker daemon'):
                c.run(['docker', 'image', 'ls'])


class FeedbackTests(unittest.TestCase):
    def test_short_notice_is_unchanged(self):
        self.assertEqual(compact_notice('请选择项目目录', True), '请选择项目目录')

    def test_docker_progress_collapses_to_cause(self):
        raw = '\n'.join(['Image Pulling'] * 30 + ['Error response from daemon: No such image: ccbox2-test-gateway:latest'])
        notice = compact_notice(raw, True)
        self.assertIn('No such image', notice)
        self.assertNotIn('Pulling', notice)
        self.assertEqual(len(notice.splitlines()), 2)
        self.assertLess(len(notice), 125)

    def test_long_line_is_bounded(self):
        self.assertLess(len(compact_notice('长' * 10000, True)), 125)

    def test_ansi_and_blank_lines_are_removed(self):
        self.assertEqual(compact_notice('\n\x1b[31m错误\x1b[0m\n'), '错误')

    def test_multiline_success_keeps_summary(self):
        notice = compact_notice('配置已生成\n请用 VS Code 打开引导文件夹\n下一步')
        self.assertTrue(notice.startswith('配置已生成\n'))
        self.assertIn('操作记录', notice)


if __name__ == '__main__':
    unittest.main(verbosity=2)
