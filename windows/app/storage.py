"""Transactional host-data relocation. Docker volumes remain managed by Docker."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import vault
from .editor_paths import normalize_editor_directory

POINTER = 'storage-location.json'


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.ccbox-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def data_location(root):
    root = Path(root).resolve()
    pointer = root / POINTER
    if not pointer.exists():
        return root / 'data'
    try:
        obj = json.loads(pointer.read_text(encoding='utf-8'))
        raw = obj['data_dir']
        if not raw or not Path(raw).is_absolute():
            raise ValueError()
        folder = Path(raw).resolve()
        if not folder.is_dir() or not ((folder / 'settings.json').is_file() or (folder / 'environments.json').is_file()):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('自定义数据目录不可用。请连接对应磁盘或修复 storage-location.json；程序未创建新环境。') from None
    return folder


def migration_target(value, source, workspace, root):
    value = normalize_editor_directory(value)
    if not value:
        raise ValueError('请选择新的数据目录。')
    target, source, root = Path(value).resolve(), Path(source).resolve(), Path(root).resolve()
    protected = [source, root]
    if workspace:
        protected.append(Path(workspace).resolve())
    if any(target == p or target in p.parents or p in target.parents for p in protected):
        raise ValueError('数据目录应独立于项目、程序和原数据目录，避免把凭据共享给容器。')
    if target.exists() and any(target.iterdir()):
        raise ValueError('目标目录不是空目录；请另选位置。已有文件保持不变。')
    return target


def file_digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def copy_verified(source, target):
    """Keep source as backup. Never copy live plaintext proxy data or redirected links."""
    source, target = Path(source).resolve(), Path(target).resolve()
    vault.protect_directory(target)
    count = 0
    for path in source.rglob('*'):
        rel = path.relative_to(source)
        if rel.parts[0] == 'active' or (len(rel.parts) >= 3 and rel.parts[0] == 'environments' and rel.parts[2] == 'active'):
            continue
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(source):
            raise ValueError('数据目录包含指向外部的链接；请先移除链接后迁移。')
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            if file_digest(path) != file_digest(dest):
                raise RuntimeError('复制后的文件校验不一致；仍保留原数据。')
            count += 1
    return count


def docker_disk_location():
    settings = Path(os.environ.get('APPDATA', Path.home())) / 'Docker/settings-store.json'
    try:
        obj = json.loads(settings.read_text(encoding='utf-8'))
        for key in ('CustomWslDistroDir', 'DataFolder', 'DiskImageLocation', 'diskImageLocation', 'dataFolder'):
            if isinstance(obj.get(key), str) and obj[key]:
                return obj[key]
    except (OSError, ValueError):
        pass
    default = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Docker/wsl'
    return str(default) if default.exists() else '请在 Docker Desktop → Settings → Resources → Advanced 查看'
