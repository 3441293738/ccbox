"""Non-interactive native Mac build. No account login or Docker workloads in CI."""
import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require_native_mac():
    if sys.platform != 'darwin' or platform.machine().lower() != 'arm64':
        raise RuntimeError('Use an Apple silicon macOS runner with native ARM64 Python.')
    if not (3, 12) <= sys.version_info < (3, 16):
        raise RuntimeError('Python 3.12 through 3.15 is required.')


def run(args, log, *, env=None, timeout=600, cwd=None):
    """Preserve diagnostics without exposing shell-expanded environment variables."""
    result = subprocess.run([str(a) for a in args], cwd=cwd or ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
    log.write_text(result.stdout, encoding='utf-8')
    print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f'{log.name} failed with exit code {result.returncode}')
    return result.stdout


def assert_selftest(path):
    report = json.loads(path.read_text(encoding='utf-8'))
    required = ('frozen', 'runtime_sidecar', 'lan_access_ui', 'guided_start_ui', 'sandbox_editor_ui',
                'multi_project_ui', 'isolated_environments_ui', 'clipboard_support', 'host_development_ui')
    if report.get('gui_init') != 'PASS' or any(report.get(k) is not True for k in required):
        raise RuntimeError('Packaged application self-test did not pass all required checks.')
    tls = report.get('tls', {})
    if (tls.get('bundled_ca') is not True or tls.get('bundled_ca_count', 0) < 100
            or tls.get('certificate_required') is not True or tls.get('hostname_check') is not True):
        raise RuntimeError('Packaged CA bundle or strict TLS self-test failed.')
    return report


def isolated_environment(home):
    # Prove that a downloaded app runs without build Python/Tk paths or a user config.
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(('PYTHON', 'VIRTUAL_ENV', 'TCL_', 'TK_')) or k in ('TCL_LIBRARY', 'TK_LIBRARY'))}
    env.update(HOME=str(home), PATH='/usr/bin:/bin:/usr/sbin:/sbin', PYTHONNOUSERSITE='1')
    # A runner's CA installation must not hide a broken standalone app bundle.
    env.update(SSL_CERT_FILE=str(home/'missing-system-ca.pem'), SSL_CERT_DIR=str(home/'missing-system-ca-dir'))
    return env


def validate_bundle(app):
    with (app / 'Contents/Info.plist').open('rb') as f:
        info = plistlib.load(f)
    if info.get('CFBundleExecutable') != 'CCBOX' or info.get('CFBundleIdentifier') != 'com.ccbox.desktop':
        raise RuntimeError('Unexpected application identity.')
    executable = app / 'Contents/MacOS/CCBOX'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError('Application executable or executable permission is missing.')
    base = app.resolve()
    for path in app.rglob('*'):
        if path.is_symlink() and (not path.exists() or not path.resolve().is_relative_to(base)):
            raise RuntimeError('Bundle contains a broken or external symlink.')
        if path.name in {'credentials.dpapi', 'credentials.keychain.json', 'storage-location.json', 'proxy.active.json'}:
            raise RuntimeError('User data must not be packaged in the application.')
    return executable


def build(output):
    require_native_mac()
    output = Path(output).resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError('Use an empty output directory; existing packages are preserved.')
    output.mkdir(parents=True, exist_ok=True)
    workspace = ROOT / '.mac-build'
    workspace.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='cloud-', dir=workspace))
    logs = work / 'logs'
    logs.mkdir()
    print(f'Build diagnostics: {logs}', flush=True)
    py = sys.executable
    run([py, '-c', 'import platform,tkinter; r=tkinter.Tk(); r.withdraw(); '
         'print(platform.machine(), tkinter.TkVersion); r.destroy()'], logs/'tk-runtime.log')
    run([py, ROOT/'tests/run_tests.py'], logs/'unit-tests.log')
    run([py, ROOT.parent/'tools/collect_licenses.py', work/'licenses'], logs/'licenses.log')
    build_env = dict(os.environ, CCBOX_LICENSES_DIR=str(work/'licenses'))
    run([py, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', work/'dist',
         '--workpath', work/'work', ROOT/'packaging/CCBOX-mac.spec'], logs/'pyinstaller.log', timeout=1200, env=build_env)
    app = work/'dist/CCBOX.app'
    exe = validate_bundle(app)
    arch = run(['/usr/bin/lipo', '-archs', exe], logs/'architecture.log').strip()
    if arch != 'arm64':
        raise RuntimeError(f'Unexpected application architecture: {arch}')
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], logs/'signature.log')
    home = work/'test-home'
    home.mkdir()
    env = isolated_environment(home)
    run([exe, '--self-test', logs/'selftest.json'], logs/'gui-selftest.log', env=env, timeout=180, cwd=home)
    selftest = assert_selftest(logs/'selftest.json')
    # The ZIP holds an app, not a script that installs Python or builds on first launch.
    stage = work/'distribution'
    stage.mkdir()
    run(['/usr/bin/ditto', app, stage/'CCBOX.app'], logs/'stage.log')
    shutil.copyfile(ROOT/'packaging/START-HERE.html', stage/'开始使用.html')
    for notice in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(ROOT.parent/notice, stage/notice)
    archive = output/'CCBOX-macos-arm64.zip'
    try:
        run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', stage, archive], logs/'archive.log')
        extracted = work/'extracted package with spaces'
        run(['/usr/bin/ditto', '-x', '-k', archive, extracted], logs/'extract.log')
        restored = extracted/'CCBOX.app'
        restored_exe = validate_bundle(restored)
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', restored], logs/'extracted-signature.log')
        run([restored_exe, '--self-test', logs/'extracted-selftest.json'], logs/'extracted-gui.log',
            env=env, timeout=180, cwd=home)
        assert_selftest(logs/'extracted-selftest.json')
    except Exception:
        # Only our newly-created, unverified output is removed, never a user's existing file.
        archive.unlink(missing_ok=True)
        raise
    report = {
        'version': (ROOT.parent/'VERSION').read_text().strip(),
        'built_at_utc': datetime.now(timezone.utc).isoformat(),
        'commit': os.environ.get('GITHUB_SHA', 'local'),
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'os': platform.mac_ver(), 'architecture': arch, 'python': platform.python_version(),
        'unit_tests': 'PASS', 'packaged_gui_selftest': 'PASS', 'extracted_gui_selftest': 'PASS',
        'signature_integrity': 'PASS', 'notarized': False,
        'bundled_tls_ca': selftest['tls'], 'system_ca_paths_hidden_during_selftest': True,
        'real_user_proxy_test': 'NOT RUN',
        'signing': 'Developer ID (not notarized)' if os.environ.get('CCBOX_SIGN_IDENTITY') else 'ad-hoc',
        'docker_integration': 'NOT RUN', 'real_keychain_persistence': 'NOT RUN',
        'claude_login_and_model_network': 'NOT RUN',
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    (output/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'SHA256SUMS.txt').write_text(f'{report["sha256"]}  {archive.name}\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'release')
    build(parser.parse_args().output)
