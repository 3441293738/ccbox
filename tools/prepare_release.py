"""Validate artifact provenance and hashes before publishing; never overwrite releases."""
import hashlib
import json
import shutil
import sys
from pathlib import Path


def prepare(artifacts, output, commit):
    artifacts, output = Path(artifacts), Path(output)
    if output.exists():
        raise RuntimeError('Release staging must be a new directory')
    output.mkdir(parents=True)
    sums = []
    for platform, archive in [('windows', 'CCBOX-windows-x64.zip'), ('macos', 'CCBOX-macos-arm64.zip')]:
        source = artifacts/('CCBOX-'+platform)
        report = json.loads((source/'verification.json').read_text(encoding='utf-8'))
        digest = hashlib.sha256((source/archive).read_bytes()).hexdigest()
        if report['commit'] != commit or report['sha256'] != digest:
            raise RuntimeError('Artifact commit/hash mismatch')
        if report.get('version') != (Path(__file__).resolve().parents[1]/'VERSION').read_text().strip():
            raise RuntimeError('Artifact version mismatch')
        if any(report.get(key) != 'PASS' for key in ('unit_tests', 'packaged_gui_selftest', 'extracted_gui_selftest')):
            raise RuntimeError('Incomplete build verification')
        if platform == 'macos' and (report.get('architecture') != 'arm64' or report.get('signature_integrity') != 'PASS'):
            raise RuntimeError('Mac architecture/signature verification incomplete')
        shutil.copy2(source/archive, output/archive)
        notice = 'verification-'+platform+'.json'
        shutil.copy2(source/'verification.json', output/notice)
        sums += [f'{digest}  {archive}', f'{hashlib.sha256((output/notice).read_bytes()).hexdigest()}  {notice}']
    (output/'SHA256SUMS.txt').write_text('\n'.join(sums)+'\n', encoding='utf-8')


if __name__ == '__main__':
    prepare(*sys.argv[1:])
