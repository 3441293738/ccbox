"""Local environment catalog. No account credentials are cloned or stored here."""
import json
import re
import secrets
from pathlib import Path

from . import vault
from .storage import atomic_json, data_location

CATALOG = 'environments.json'


def valid_name(value):
    if not isinstance(value, str):
        raise ValueError('环境名称应为文字。')
    value = value.strip()
    if not 1 <= len(value) <= 32 or any(ord(c) < 32 for c in value) or any(c in value for c in '/\\${}'):
        raise ValueError('环境名称填写 1–32 个字符，不包含斜杠、控制字符或 ${}。')
    return value


class Environments:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = data_location(self.root)
        self.file = self.base / CATALOG

    def read(self):
        if not self.file.exists():
            return {'version': 1, 'selected': 'default', 'items': [{'id': 'default', 'name': '默认环境'}]}
        try:
            obj = json.loads(self.file.read_text(encoding='utf-8'))
            assert obj['version'] == 1 and 1 <= len(obj['items']) <= 50
            ids, names = set(), set()
            for item in obj['items']:
                key, name = item['id'], valid_name(item['name'])
                assert key == 'default' or re.fullmatch(r'env-[a-f0-9]{12}', key)
                assert key not in ids and name.casefold() not in names
                ids.add(key)
                names.add(name.casefold())
            assert 'default' in ids and obj['selected'] in ids
            return obj
        except (OSError, ValueError, KeyError, TypeError, AssertionError):
            raise RuntimeError('环境目录索引损坏；请恢复 environments.json。原容器和资料未被修改。') from None

    def path(self, key):
        if key not in {i['id'] for i in self.read()['items']}:
            raise ValueError('环境不存在，请重新选择。')
        path = self.base if key == 'default' else self.base / 'environments' / key
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.base.resolve()):
            raise ValueError('环境数据路径不应指向外部目录。')
        if key != 'default' and not (path / 'settings.json').is_file():
            raise RuntimeError('环境设置文件缺失；请恢复原目录，避免创建空白登录环境。')
        return path

    def write(self, obj):
        if data_location(self.root) != self.base:
            raise RuntimeError('数据位置已变化，请重新打开工具。')
        vault.protect_directory(self.base)
        atomic_json(self.file, obj)

    def select(self, key):
        self.path(key)
        obj = self.read()
        obj['selected'] = key
        self.write(obj)

    def create(self, name):
        from .controller import DEFAULT
        name, obj = valid_name(name), self.read()
        if len(obj['items']) >= 50:
            raise ValueError('最多管理 50 个环境。')
        if name.casefold() in {i['name'].casefold() for i in obj['items']}:
            raise ValueError('环境名称已存在，请使用不同名称。')
        used = set()
        for item in obj['items']:
            file = self.path(item['id']) / 'settings.json'
            cfg = json.loads(file.read_text(encoding='utf-8')) if file.exists() else DEFAULT
            used.add(int(cfg.get('desktop_port', 6080)))
        port = next(p for p in range(6080, 65536) if p not in used)
        key = 'env-' + secrets.token_hex(6)
        dest = self.base / 'environments' / key
        if not dest.resolve().is_relative_to(self.base.resolve()):
            raise ValueError('环境目录指向外部位置，请恢复普通文件夹后重试。')
        vault.protect_directory(dest)
        # Deliberately no vault, browser, home data, proxy or project inheritance.
        atomic_json(dest / 'settings.json', {**DEFAULT, 'projects': [], 'desktop_port': port})
        obj['items'].append({'id': key, 'name': name})
        obj['selected'] = key
        self.write(obj)
        return key

    def rename(self, key, name):
        name, obj = valid_name(name), self.read()
        self.path(key)
        if any(i['id'] != key and i['name'].casefold() == name.casefold() for i in obj['items']):
            raise ValueError('环境名称已存在。')
        next(i for i in obj['items'] if i['id'] == key)['name'] = name
        self.write(obj)
