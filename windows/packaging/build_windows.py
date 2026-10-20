"""Clean Windows release build. No account files, in-place updates or Docker work."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def run(args, log, timeout=600, cwd=ROOT):
    result = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
    output = result.stdout + result.stderr
    log.write_text(output, encoding='utf-8')
    print(output, flush=True)
    if result.returncode:
        raise RuntimeError(f'{log.name}: exit {result.returncode}')


def assert_selftest(path):
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('gui_init') != 'PASS' or any(report.get(k) is not True for k in (
            'frozen', 'runtime_sidecar', 'host_development_ui', 'guided_start_ui',
            'sandbox_editor_ui', 'multi_project_ui', 'isolated_environments_ui', 'clipboard_support')):
        raise RuntimeError('Packaged GUI/runtime self-test failed')


def build(output):
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('Use native Windows x64 Python')
    output = Path(output).resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError('Use an empty output directory; existing files are preserved')
    output.mkdir(parents=True, exist_ok=True)
    parent = ROOT/'.windows-build'
    parent.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='build-', dir=parent))
    logs = work/'logs'
    logs.mkdir()
    py = sys.executable
    run([py, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v'], logs/'unit-tests.log')
    run([py, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', work/'dist',
         '--workpath', work/'work', ROOT/'packaging/CCBOX.spec'], logs/'pyinstaller.log', timeout=1200)
    stage = work/'distribution/CCBOX'
    stage.mkdir(parents=True)
    shutil.copy2(work/'dist/CCBOX.exe', stage/'CCBOX.exe')
    # Only tracked runtime sources; do not collect stray user files or caches.
    runtime_names = subprocess.check_output(['git', 'ls-files', '-z', 'windows/runtime'], cwd=REPO).decode().split('\0')
    (stage/'runtime').mkdir(exist_ok=True)
    for name in filter(None, runtime_names):
        source = REPO/name
        if source.parent != ROOT/'runtime' or not source.is_file():
            raise RuntimeError('Unexpected runtime path')
        shutil.copy2(source, stage/'runtime'/source.name)
    shutil.copy2(ROOT/'README.html', stage/'README.html')
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(REPO/name, stage/name)
    run([py, REPO/'tools/collect_licenses.py', stage/'third-party-licenses'], logs/'licenses.log')
    run([stage/'CCBOX.exe', '--self-test', logs/'selftest.json'], logs/'selftest.log', timeout=180, cwd=stage)
    assert_selftest(logs/'selftest.json')
    archive = output/'CCBOX-windows-x64.zip'
    try:
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for file in sorted(stage.rglob('*')):
                if file.is_file():
                    z.write(file, file.relative_to(stage.parent).as_posix())
        with zipfile.ZipFile(archive) as z:
            if z.testzip():
                raise RuntimeError('ZIP integrity check failed')
            restored = work/'extracted package with spaces'
            z.extractall(restored)
        run([restored/'CCBOX/CCBOX.exe', '--self-test', logs/'extracted-selftest.json'],
            logs/'extracted.log', timeout=180, cwd=restored)
        assert_selftest(logs/'extracted-selftest.json')
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    report = {'version': (REPO/'VERSION').read_text().strip(), 'commit': os.environ.get('GITHUB_SHA', 'local'),
              'architecture': platform.machine(), 'python': platform.python_version(),
              'unit_tests': 'PASS', 'packaged_gui_selftest': 'PASS', 'extracted_gui_selftest': 'PASS',
              'authenticode_signed': False, 'docker_integration': 'NOT RUN',
              'claude_login_and_model_network': 'NOT RUN', 'sha256': digest}
    (output/'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (output/'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'release')
    build(parser.parse_args().output)
