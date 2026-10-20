"""Fail on common accidental data/credential commits; not a full secret audit."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS = {'data', 'active', 'environments', '__pycache__', '.venv', 'venv',
                  'release', 'dist', 'build', '.mac-build', '.windows-build', 'host-project-locks'}
FORBIDDEN_NAMES = {'storage-location.json', 'upstream.json', 'proxy.active.json', 'compose.json',
                   'settings.json', 'development.json', 'TEST-RESULTS.json', 'FONT-TEST-RESULTS.json'}
FORBIDDEN_SUFFIX = {'.exe', '.zip', '.dmg', '.vhdx', '.pfx', '.p12', '.pem', '.key', '.log', '.db', '.pyc'}
PATTERNS = [
    re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{40,}'),
    re.compile(r'sk-(?:ant-)?[A-Za-z0-9_-]{35,}'),
    re.compile(r'AKIA[A-Z0-9]{16}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'C:[\\/]Users[\\/]PC(?:[\\/]|\b)', re.I),
]


def check():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    failures = []
    for name in filter(None, paths):
        path = Path(name)
        if (set(path.parts) & FORBIDDEN_DIRS or path.name in FORBIDDEN_NAMES
                or path.suffix.lower() in FORBIDDEN_SUFFIX or path.name.startswith(('credentials.', '.env'))):
            failures.append(name + ': forbidden runtime/artifact path')
            continue
        file = ROOT/path
        if file.is_symlink():
            failures.append(name + ': unexpected symlink')
            continue
        if file.stat().st_size > 1_000_000:
            failures.append(name + ': unexpected large source file')
            continue
        try:
            body = file.read_text(encoding='utf-8-sig')
        except UnicodeError:
            failures.append(name + ': unexpected binary')
            continue
        for line, value in enumerate(body.splitlines(), 1):
            if any(p.search(value) for p in PATTERNS):
                failures.append(f'{name}:{line}: potential private data (value redacted)')
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'Source hygiene PASS: {len([p for p in paths if p])} tracked files. Manual review still required.')


if __name__ == '__main__':
    check()
