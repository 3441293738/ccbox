import importlib.util
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ccbox_mac_build', root/'packaging/build_macos.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class MacBuildTests(unittest.TestCase):
    def test_rejects_windows_and_translated_intel_builds(self):
        for system, machine in [('win32', 'ARM64'), ('darwin', 'x86_64'), ('linux', 'aarch64')]:
            with patch.object(build.sys, 'platform', system), patch.object(build.platform, 'machine', return_value=machine):
                with self.assertRaises(RuntimeError):
                    build.require_native_mac()

    def test_accepts_native_arm64_mac(self):
        with patch.object(build.sys, 'platform', 'darwin'), patch.object(build.platform, 'machine', return_value='arm64'):
            build.require_native_mac()

    def test_selftest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'result.json'
            path.write_text(json.dumps({'gui_init': 'PASS', 'frozen': True}), encoding='utf-8')
            with self.assertRaises(RuntimeError):
                build.assert_selftest(path)

    def test_build_python_paths_removed_from_runtime_check(self):
        with patch.dict(os.environ, {'PYTHONPATH': '/build', 'VIRTUAL_ENV': '/venv', 'TCL_LIBRARY': '/tk'}, clear=True):
            env = build.isolated_environment(Path('/tmp/test-home'))
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('VIRTUAL_ENV', env)
        self.assertNotIn('TCL_LIBRARY', env)
        self.assertEqual(env['PATH'], '/usr/bin:/bin:/usr/sbin:/sbin')
        self.assertEqual(env['SSL_CERT_FILE'], str(Path('/tmp/test-home/missing-system-ca.pem')))
        self.assertEqual(env['SSL_CERT_DIR'], str(Path('/tmp/test-home/missing-system-ca-dir')))

    def test_selftest_requires_independent_ca_bundle_and_strict_tls(self):
        good = dict(gui_init='PASS', frozen=True, runtime_sidecar=True, guided_start_ui=True,
                    sandbox_editor_ui=True, multi_project_ui=True, isolated_environments_ui=True, clipboard_support=True,
                    host_development_ui=True)
        tls = dict(bundled_ca=True, bundled_ca_count=100, certificate_required=True, hostname_check=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'result.json'
            for field, value in [('bundled_ca', False), ('bundled_ca_count', 0),
                                 ('certificate_required', False), ('hostname_check', False)]:
                path.write_text(json.dumps({**good, 'tls': {**tls, field: value}}), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    build.assert_selftest(path)
            path.write_text(json.dumps({**good, 'tls': tls}), encoding='utf-8')
            self.assertEqual(build.assert_selftest(path)['tls'], tls)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(build, 'require_native_mac'):
            dest = Path(tmp)
            old = dest/'old.zip'
            old.write_bytes(b'keep')
            with self.assertRaises(RuntimeError):
                build.build(dest)
            self.assertEqual(old.read_bytes(), b'keep')

    def test_bundle_rejects_account_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)/'CCBOX.app'
            exe = app/'Contents/MacOS/CCBOX'
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b'app')
            exe.chmod(0o755)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleExecutable': 'CCBOX', 'CFBundleIdentifier': 'com.ccbox.desktop'}))
            with patch.object(build.os, 'access', return_value=True):
                self.assertEqual(build.validate_bundle(app), exe)
                (app/'credentials.dpapi').write_bytes(b'private')
                with self.assertRaises(RuntimeError):
                    build.validate_bundle(app)

    def test_workflow_has_manual_release_and_readonly_build_token(self):
        text = (root.parent/'.github/workflows/build.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', text)
        self.assertNotIn('pull_request_target:', text)
        self.assertIn('contents: read', text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('architecture: arm64', text)
        self.assertIn('timeout-minutes: 30', text)
        self.assertIn("github.event_name == 'workflow_dispatch'", text)


if __name__ == '__main__':
    unittest.main()
