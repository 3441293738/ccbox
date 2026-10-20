# Native Windows build; runtime sidecars are staged by build_windows.py.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve().parent
datas, binaries, hiddenimports = [], [], ['transport']
for package in ('customtkinter', 'darkdetect'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
a = Analysis([str(root/'ccbox.py')], pathex=[str(root), str(root/'runtime')],
             datas=datas, binaries=binaries, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='CCBOX', console=False,
          strip=False, upx=False)
