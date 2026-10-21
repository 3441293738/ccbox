"""Read-only account overview. Never unlock credentials or start containers."""
import json
import re

from .environments import Environments


def catalog_rows(root):
    catalog = Environments(root)
    result = []
    for item in catalog.read()['items']:
        row = {**item, 'project': '', 'summary': '尚未选择项目', 'port': '', 'invalid': False}
        try:
            data = catalog.path(item['id'])
            cfg = json.loads((data/'settings.json').read_text(encoding='utf-8')) if (data/'settings.json').exists() else {}
            workspace = cfg.get('workspace', '')
            if workspace:
                name = workspace.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
                row['summary'] = f"{name} · {1 + len(cfg.get('projects', []))} 个项目"
            row['port'] = str(int(cfg.get('desktop_port', 6080)))
            if (data/'compose.json').exists():
                project = json.loads((data/'compose.json').read_text(encoding='utf-8')).get('name', '')
                if not re.fullmatch(r'ccbox2-[a-z0-9-]{4,64}', project):
                    raise ValueError('invalid project identity')
                row['project'] = project
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            row['invalid'] = True
        result.append(row)
    identities = [row['project'] for row in result if row['project']]
    for row in result:
        if row['project'] and identities.count(row['project']) > 1:
            row['invalid'] = True
    return result


def classify(services):
    """ps JSON contains only state/labels, not container env or secret mounts."""
    if not services:
        return 'new', '未启动', '首次打开会准备容器；已有登录卷不会删除。'
    if any(len(group) != 1 for group in services.values()):
        return 'error', '需检查', '同一服务存在多个容器，请到检查与修复查看。'
    rows = {key: group[0] for key, group in services.items()}
    if any(r.get('State') in ('dead', 'restarting') or
           re.search(r'Exited \((?!0\))\d+\)', r.get('Status', '')) or
           '(unhealthy)' in r.get('Status', '') for r in rows.values()):
        return 'error', '启动异常', '容器异常退出或健康检查失败；打开时可重新准备，资料保留。'
    running = [key for key, row in rows.items() if row.get('State') == 'running']
    if len(running) == 2 and '(healthy)' in rows['gateway'].get('Status', ''):
        return 'running', '运行中', '可与其他空间同时运行；关闭桌面网页不会停止容器。'
    if running or any(r.get('State') in ('created', 'paused') for r in rows.values()):
        return 'partial', '尚未就绪', '网关或沙箱未就绪；请等待刷新或检查当前空间。'
    return 'stopped', '已停止', '登录与项目资料保留；点击启动继续使用。'


def snapshot(root, run, docker):
    rows = catalog_rows(root)
    # One bounded Docker call for all accounts, even with 50 saved spaces.
    try:
        output = run([docker(), 'ps', '-a', '--filter', 'label=com.docker.compose.project',
                      '--format', '{{json .}}'], timeout=12)
        containers = [json.loads(line) for line in output.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in containers):
            raise ValueError('invalid docker status')
    except (OSError, RuntimeError, ValueError):
        for row in rows:
            row.update(state='unknown', status='未连接 Docker', detail='状态未确认，请启动 Docker 后刷新；不会自动停止任何空间。')
        return rows
    projects = {}
    for row in containers:
        labels = dict(part.split('=', 1) for part in str(row.get('Labels', '')).split(',') if '=' in part)
        project, service = labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.oneoff', '').lower() == 'true' or service not in ('gateway', 'sandbox'):
            continue
        projects.setdefault(project, {}).setdefault(service, []).append(row)
    for row in rows:
        if row['invalid']:
            state, status, detail = 'error', '配置需检查', '配置缺失或身份重复；原容器与资料未改动。'
        else:
            state, status, detail = classify(projects.get(row['project'], {}))
        row.update(state=state, status=status, detail=detail)
    return rows
