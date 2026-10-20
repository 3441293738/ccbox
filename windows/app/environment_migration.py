"""Move the complete catalog only after every environment is stopped."""
import json
from pathlib import Path
from .environments import Environments
from .storage import migration_target, copy_verified, atomic_json, POINTER
from .projects import mounts, overlaps


def migrate_all(controller, target):
    from .controller import Controller, make_compose
    catalog = Environments(controller.root)
    old = catalog.base
    target = migration_target(target, old, '', controller.root)
    entries = []
    for item in catalog.read()['items']:
        c = Controller(controller.root, item['id'])
        for m in mounts(c.config()):
            if m['source'] and overlaps(target, m['source']):
                raise ValueError('新数据目录与某个环境的项目重叠。')
        if c.file.exists() and c.run(c.command('ps', '-q')):
            raise RuntimeError('多环境迁移前，请关闭所有 CCBOX 专用 VS Code 窗口，并逐一「停止并保留」所有环境；之后重试。资料和登录卷保留。')
        entries.append((c, c.credentials()))
    controller.log('复制所有环境；跳过运行时明文代理文件，保留原目录备份。')
    count = copy_verified(old, target)
    for c, cred in entries:
        before = c.data
        c._set_data(target / before.relative_to(old))
        if c.credentials() != cred:
            raise RuntimeError('保险库校验失败；仍使用原数据位置。')
        cfg = c.config()
        explicit = cfg.get('vscode_dir')
        if explicit and Path(explicit).resolve().is_relative_to(old):
            cfg['vscode_dir'] = str(target / Path(explicit).resolve().relative_to(old))
        if c.config_file.exists():
            atomic_json(c.config_file, cfg)
        if c.file.exists():
            if not cred.get('project'):
                raise RuntimeError('已有 Compose 缺少对应保险库，请先修复后重试。')
            atomic_json(c.file, make_compose(c.root, c.data, cfg, cred['project']))
    for file in target.rglob('devcontainer.json'):
        if file.parent.name != '.devcontainer':
            continue
        try:
            cfg = json.loads(file.read_text(encoding='utf-8'))
            src = Path(cfg.get('dockerComposeFile', ''))
            if cfg.get('name') == 'CCBOX isolated workspace' and src.is_absolute() and src.is_relative_to(old):
                cfg['dockerComposeFile'] = str(target / src.relative_to(old))
                atomic_json(file, cfg)
        except (ValueError, TypeError):
            pass
    # No active environments to recover: pointer is the final transaction commit.
    atomic_json(controller.root / POINTER, {'version': 1, 'data_dir': str(target), 'previous_data_dir': str(old)})
    controller._set_data(target / controller.data.relative_to(old))
    return f'全部环境数据已迁移到 {target}\n校验 {count} 个文件；旧目录保留：{old}\nDocker 项目标识和登录卷不变；请选择需要的环境重新启动。'
