"""Collect original license notices in a clean build environment, never user data."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import sys
import time
import tkinter
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError


def fetch_notice(url, path):
    for attempt in range(3):
        try:
            with urlopen(url, timeout=45) as response:
                data = response.read(1_000_001)
            break
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    if not data or len(data) > 1_000_000 or b'<html' in data[:200].lower():
        raise RuntimeError('Unexpected upstream license response')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {'source': url, 'sha256': hashlib.sha256(data).hexdigest()}


def collect(destination):
    dest = Path(destination)
    if dest.exists() and any(dest.iterdir()):
        raise RuntimeError('License output must be empty')
    dest.mkdir(parents=True, exist_ok=True)
    report = {'python': sys.version.split()[0], 'packages': [], 'runtime_notices': {}}
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', name)
        row = {'name': name, 'version': dist.version,
               'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'), 'files': []}
        for file in dist.files or []:
            if (any(word in file.name.lower() for word in ('license', 'copying', 'notice'))
                    and file.suffix.lower() not in {'.py', '.pyc', '.so', '.dll'}):
                source = dist.locate_file(file)
                if not source.is_file():
                    continue
                out = dest/safe/str(len(row['files']))/file.name
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(source.read_bytes())
                row['files'].append(out.relative_to(dest).as_posix())
        report['packages'].append(row)
    # Exact upstream version tags, HTTPS verification remains enabled.
    py = sys.version.split()[0]
    report['runtime_notices']['Python'] = fetch_notice(
        f'https://raw.githubusercontent.com/python/cpython/v{py}/LICENSE', dest/'Python-LICENSE.txt')
    app = tkinter.Tk()
    app.withdraw()
    try:
        versions = {'tcl': app.tk.eval('info patchlevel'), 'tk': app.tk.eval('set tk_patchLevel')}
    finally:
        app.destroy()
    for name, version in versions.items():
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise RuntimeError('Unsupported Tcl/Tk license source version')
        tag = 'core-' + version.replace('.', '-')
        report['runtime_notices'][name] = fetch_notice(
            f'https://raw.githubusercontent.com/tcltk/{name}/{tag}/license.terms', dest/f'{name}-license.terms')
    (dest/'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Dependency and runtime notices collected.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    collect(parser.parse_args().output)
