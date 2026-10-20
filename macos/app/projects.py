"""Explicit project allow-list and remote multi-root workspace generation."""
import hashlib
import json
import os
import sys
import re
from pathlib import Path


def overlaps(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def normalize_projects(items, primary, protected):
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError('附加项目应为列表，最多 20 个。')
    result, seen, names, paths = [], set(), set(), [Path(primary).resolve()]
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('项目记录格式异常。')
        key, name, raw = item.get('id', ''), item.get('name', '').strip(), item.get('path', '')
        if not re.fullmatch(r'p-[a-f0-9]{12}', key) or key in seen:
            raise ValueError('项目标识重复或格式异常，请移除该条目后重新添加。')
        if not name or len(name) > 48 or any(ord(c) < 32 for c in name) or name.casefold() in names:
            raise ValueError('附加项目名称应唯一，长度 1–48 个字符。')
        path = Path(raw).expanduser()
        if not raw or not path.is_absolute() or not path.is_dir():
            raise ValueError(f'项目「{name}」请选择已存在的绝对目录。')
        path = path.resolve()
        if path == Path(path.anchor) or path == Path.home() or any(overlaps(path, p) for p in protected):
            raise ValueError(f'项目「{name}」与受保护目录重叠。请选择独立项目。')
        if any(overlaps(path, p) for p in paths):
            raise ValueError(f'项目「{name}」与其他挂载重复或嵌套，请保留一个明确边界。')
        if not isinstance(item.get('readonly', False), bool):
            raise ValueError('项目只读选项应为布尔值。')
        result.append({'id': key, 'name': name, 'path': str(path), 'readonly': item.get('readonly', False)})
        paths.append(path)
        seen.add(key)
        names.add(name.casefold())
    return result


def mounts(config):
    return [{'source': config['workspace'], 'target': '/workspace', 'read_only': bool(config.get('readonly', False))}] + [
        {'source': p['path'], 'target': '/projects/' + p['id'], 'read_only': p['readonly']}
        for p in config.get('projects', [])]


def signature(config):
    return hashlib.sha256(json.dumps(mounts(config), sort_keys=True).encode()).hexdigest()


def same_bind_source(actual, expected):
    # Docker Desktop may report its translated Linux path or the original Windows path.
    if os.name == 'nt':
        translated = re.fullmatch(r'/(?:run/desktop/mnt/host|host_mnt|mnt)/([a-zA-Z])/(.*)', actual)
        if translated:
            actual = translated[1] + ':/' + translated[2]
    if sys.platform=='darwin':
        for prefix in ('/host_mnt','/run/desktop/mnt/host'):
            if actual.startswith(prefix+'/'):
                actual=actual[len(prefix):]
                break
    return Path(actual).resolve() == Path(expected).resolve()


def workspace_document(config):
    return {'folders': [{'name': '主项目', 'path': '/workspace'}] + [
        {'name': p['name'] + (' [只读]' if p['readonly'] else ''), 'path': '/projects/' + p['id']}
        for p in config.get('projects', [])],
        'settings': {'remote.autoForwardPorts': False, 'git.autofetch': False}}
