"""Host-side editor bootstrap paths; independent of projects and Docker storage."""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path


class EditorDirectoryError(ValueError):
    key = 'vscode_dir'


def normalize_editor_directory(value):
    if not isinstance(value, str):
        raise EditorDirectoryError('VS Code 配置目录应为完整文件夹路径。')
    value = value.strip()
    if not value:
        return ''  # Backward-compatible default, resolved by the controller.
    if any(ord(c) < 32 for c in value):
        raise EditorDirectoryError('VS Code 配置目录含有无效字符。')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise EditorDirectoryError('VS Code 配置目录请填写绝对路径，例如 /Users/你的用户名/CCBOX-vscode。')
    if os.name == 'nt' and any(
            any(c in part for c in '<>:"|?*') or part.endswith((' ', '.')) or Path(part).is_reserved()
            for part in path.parts[1:]):
        raise EditorDirectoryError('VS Code 配置目录包含 Windows 不支持的文件夹名称。')
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        raise EditorDirectoryError('VS Code 配置路径解析失败，请选择可访问的本地文件夹。') from None
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise EditorDirectoryError('请选择专用配置文件夹，不要直接选择磁盘根目录或用户根目录。')
    for parent in (path, *path.parents):
        if parent.exists() and not parent.is_dir():
            raise EditorDirectoryError('VS Code 配置路径或其上级位置是文件，请选择文件夹。')
    return str(path)


def write_editor_configs(folder, compose_file, devconfig, settings):
    """Write only generated files; reject unrelated configurations before any writes."""
    folder = Path(folder).resolve()
    destinations = {
        folder / '.devcontainer/devcontainer.json': devconfig,
        folder / '.vscode/settings.json': settings,
    }
    for path in destinations:
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise EditorDirectoryError('配置文件指向所选目录之外，请选择独立文件夹。')
        if path.exists() and not path.is_file():
            raise EditorDirectoryError('目标位置存在同名文件夹，请选择其他配置目录。')
    devpath = folder / '.devcontainer/devcontainer.json'
    if any(p.exists() for p in destinations):
        try:
            existing = json.loads(devpath.read_text(encoding='utf-8-sig'))
            owned = (existing.get('name') == 'CCBOX isolated workspace'
                     and isinstance(existing.get('dockerComposeFile'), str)
                     and Path(existing['dockerComposeFile']).resolve() == Path(compose_file).resolve())
        except (OSError, ValueError, AttributeError, TypeError):
            owned = False
        if not owned:
            raise EditorDirectoryError('所选目录已有其他 VS Code 配置，已保留原文件。请选择空的专用文件夹。')

    contents = {p: (json.dumps(d, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                for p, d in destinations.items()}
    changed = {p: body for p, body in contents.items() if not p.exists() or p.read_bytes() != body}
    backup = folder / '.ccbox-backups' / str(time.time_ns())
    if not backup.resolve().is_relative_to(folder):
        raise EditorDirectoryError('配置备份位置指向所选目录之外，请选择独立文件夹。')
    for path in changed:
        if path.exists():
            dest = backup / path.relative_to(folder)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    for path, body in changed.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement keeps each JSON intact even if the process is interrupted.
        fd, temporary = tempfile.mkstemp(prefix='.ccbox-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(body)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return folder
