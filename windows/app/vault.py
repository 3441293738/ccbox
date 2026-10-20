"""Windows CurrentUser DPAPI; no portable cleartext credentials at rest."""
import ctypes
from ctypes import wintypes
import csv
import io
import json
import os
import subprocess
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]


def transform(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('凭据保险库需要 Windows DPAPI')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), 'CCBOX upstream', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def protect_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)
        return
    flags = subprocess.CREATE_NO_WINDOW
    identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], creationflags=flags)
    sid = list(csv.reader(io.StringIO(identity.decode(errors='replace'))))[0][-1]
    if not sid.startswith('S-1-'):
        raise RuntimeError('读取 Windows SID 失败')
    p = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F',
                        '*S-1-5-18:(OI)(CI)F'], capture_output=True, creationflags=flags)
    if p.returncode:
        raise RuntimeError('设置凭据目录 ACL 失败；未写入明文凭据')


def save(path, obj):
    path = Path(path)
    protect_directory(path.parent)
    encrypted = transform(json.dumps(obj, ensure_ascii=False).encode())
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(encrypted)
    tmp.replace(path)


def load(path):
    return json.loads(transform(Path(path).read_bytes(), decrypt=True))
