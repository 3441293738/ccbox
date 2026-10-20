import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT/'tools/prepare_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.artifacts = self.base/'artifacts'
        self.dest = self.base/'release'
        for name, archive in [('windows', 'CCBOX-windows-x64.zip'), ('macos', 'CCBOX-macos-arm64.zip')]:
            path = self.artifacts/('CCBOX-'+name)
            path.mkdir(parents=True)
            body = b'test fixture, not an actual distribution'
            (path/archive).write_bytes(body)
            report = dict(commit='fixture', sha256=hashlib.sha256(body).hexdigest(),
                          version=(ROOT/'VERSION').read_text().strip(), architecture='arm64',
                          signature_integrity='PASS', unit_tests='PASS', packaged_gui_selftest='PASS',
                          extracted_gui_selftest='PASS')
            (path/'verification.json').write_text(json.dumps(report), encoding='utf-8')

    def test_stages_both_packages_and_distinct_reports(self):
        release.prepare(self.artifacts, self.dest, 'fixture')
        self.assertEqual(len(list(self.dest.iterdir())), 5)
        self.assertEqual(len((self.dest/'SHA256SUMS.txt').read_text().splitlines()), 4)

    def test_wrong_commit_rejected(self):
        with self.assertRaises(RuntimeError):
            release.prepare(self.artifacts, self.dest, 'different')

    def test_tampered_archive_rejected(self):
        (self.artifacts/'CCBOX-windows/CCBOX-windows-x64.zip').write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            release.prepare(self.artifacts, self.dest, 'fixture')

    def test_incomplete_verification_rejected(self):
        report = self.artifacts/'CCBOX-macos/verification.json'
        data = json.loads(report.read_text())
        data['extracted_gui_selftest'] = 'NOT RUN'
        report.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError):
            release.prepare(self.artifacts, self.dest, 'fixture')

    def test_existing_output_preserved(self):
        self.dest.mkdir()
        (self.dest/'keep').write_text('keep')
        with self.assertRaises(RuntimeError):
            release.prepare(self.artifacts, self.dest, 'fixture')
        self.assertEqual((self.dest/'keep').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
