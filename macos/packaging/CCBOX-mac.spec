# Run only with native ARM64 Python on macOS. No Windows executable is reused.
import os
import platform
import sys
import subprocess
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

if sys.platform != 'darwin' or platform.machine().lower() != 'arm64':
    raise SystemExit('Build on an Apple silicon Mac with native ARM64 Python.')

root = Path(SPECPATH).parent
runtime_names = subprocess.check_output(['git', 'ls-files', '-z', 'macos/runtime'], cwd=root.parent).decode().split('\0')
datas = [(str(root.parent/name), 'runtime') for name in filter(None, runtime_names)]
datas += [(str(root/'README.html'), '.'), (str(root.parent/'LICENSE'), '.'),
          (str(root.parent/'THIRD_PARTY_NOTICES.md'), '.'),
          (os.environ['CCBOX_LICENSES_DIR'], 'third-party-licenses')]
binaries = []
hiddenimports = ['transport', 'certifi', *collect_submodules('keyring.backends.macOS')]
datas += collect_data_files('certifi', includes=['cacert.pem'])
datas += copy_metadata('keyring')
for package in ('customtkinter', 'darkdetect'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis([str(root/'ccbox.py')], pathex=[str(root),str(root/'runtime')],
             datas=datas, binaries=binaries, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CCBOX', console=False,
          target_arch='arm64', codesign_identity=os.environ.get('CCBOX_SIGN_IDENTITY') or None)
coll = COLLECT(exe, a.binaries, a.datas, name='CCBOX')
app = BUNDLE(coll, name='CCBOX.app', bundle_identifier='com.ccbox.desktop',
             info_plist={'CFBundleDisplayName':'CCBOX', 'CFBundleShortVersionString':'0.3.2',
                         'CFBundleVersion':'8', 'NSHighResolutionCapable':True,
                         'NSAppleEventsUsageDescription':'仅在你点击打开 Claude 终端时，打开当前沙箱的交互终端。'})
