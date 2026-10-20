"""Keychain-only credentials. On disk: a random reference, never the secret."""
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

SERVICE = 'com.ccbox.credentials.v1'


def backend():
    # Explicit native backend: ignore user-selected plaintext/null backends.
    try:
        from keyring.backends.macOS import Keyring
        value = Keyring()
        value.keychain = None
        if value.priority <= 0:
            raise RuntimeError()
        return value
    except Exception:
        raise RuntimeError('macOS 钥匙串未就绪。请确认当前用户的登录钥匙串可用；原资料保持不变。') from None


def reference(path):
    try:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        if obj.get('version') != 1 or obj.get('backend') != 'macos-keychain' or not re.fullmatch(r'[a-f0-9]{32}', obj.get('id', '')):
            raise ValueError()
        return obj['id']
    except (ValueError, OSError, AttributeError, TypeError):
        raise RuntimeError('钥匙串引用文件异常。请恢复原文件；Windows 加密凭据须在原电脑处理，不会在这里重置账号。') from None


def load(path):
    identity = reference(path)
    try:
        raw = backend().get_password(SERVICE, identity)
        if raw is None:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Exception:
        raise RuntimeError('读取钥匙串凭据未成功，请解锁原用户钥匙串并重试。不会创建空白账号替代原资料。') from None


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.is_symlink():
        raise ValueError('凭据引用文件不应为符号链接。')
    previous = reference(path) if path.exists() else None
    if previous:
        if load(path) == obj:  # Never overwrite an inaccessible existing account.
            return
    # New immutable item + atomic reference commit: backups keep their old item.
    identity = secrets.token_hex(16)
    store = backend()
    try:
        store.set_password(SERVICE, identity, json.dumps(obj, ensure_ascii=False))
    except Exception:
        raise RuntimeError('保存钥匙串未成功，原凭据保留。请检查钥匙串访问提示后重试。') from None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.ccbox-keychain-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'version':1, 'backend':'macos-keychain', 'id':identity}, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            store.delete_password(SERVICE, identity)
        except Exception:
            pass
        raise
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
