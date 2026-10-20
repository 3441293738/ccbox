"""A dedicated VS Code client profile attaches to the existing sandbox, not a new one."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

from .storage import atomic_json

EXTENSION = 'ms-vscode-remote.remote-containers'


def locate_code():
    found = shutil.which('code')
    candidates = [Path(found).resolve().parent.parent / 'Code.exe'] if found else []
    candidates += [Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Microsoft VS Code/Code.exe',
                   Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Microsoft VS Code/Code.exe']
    for exe in candidates:
        if not exe.is_file():
            continue
        cli = exe.parent / 'resources/app/out/cli.js'
        if not cli.is_file():
            # Recent VS Code Windows updates keep resources in a commit-named directory.
            options = list(exe.parent.glob('*/resources/app/out/cli.js'))
            if not options:
                continue
            cli = max(options, key=lambda p: p.stat().st_mtime)
        return exe, cli
    raise RuntimeError('未找到 VS Code。请先安装 Windows 版 VS Code，并启用添加到 PATH。')


def attach_uri(name, path='/workspace'):
    if not name.startswith('/') or any(c not in '/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in name):
        raise ValueError('容器名称格式异常')
    encoded = json.dumps({'containerName': name}, separators=(',', ':')).encode().hex()
    if not path.startswith('/') or '..' in path.split('/'):
        raise ValueError('容器工作区路径格式异常')
    return 'vscode-remote://attached-container+' + encoded + quote(path, safe='/')


def configure_client(base, name, label='Container'):
    base = Path(base).resolve()
    user, extensions = base / 'user-data', base / 'extensions'
    settings_file = user / 'User/settings.json'
    settings = json.loads(settings_file.read_text(encoding='utf-8')) if settings_file.exists() else {}
    settings.update({
        'window.title': '${rootName}${separator}CCBOX · ' + label + '${separator}${appName}',
        'dev.containers.copyGitConfig': False,
        'dev.containers.gitCredentialHelperConfigLocation': 'none',
        'dev.containers.mountWaylandSocket': False,
        'remote.autoForwardPorts': False,
        'git.autofetch': False,
        'workbench.startupEditor': 'none',
    })
    settings.setdefault('remote.extensionKind', {})['anthropic.claude-code'] = ['workspace']
    atomic_json(settings_file, settings)
    config = {
        'workspaceFolder': '/workspace', 'remoteUser': 'claude', 'userEnvProbe': 'none',
        'extensions': ['anthropic.claude-code'], 'forwardPorts': [],
        'remoteEnv': {'HTTP_PROXY': 'http://127.0.0.1:18080', 'HTTPS_PROXY': 'http://127.0.0.1:18080',
                      'http_proxy': 'http://127.0.0.1:18080', 'https_proxy': 'http://127.0.0.1:18080',
                      'BROWSER': '/usr/local/bin/ccbox-browser', 'SSH_AUTH_SOCK': ''},
        'settings': {'http.proxy': 'http://127.0.0.1:18080', 'http.proxySupport': 'override',
                     'remote.autoForwardPorts': False, 'git.autofetch': False},
    }
    # Verified against Dev Containers' named-configuration lookup (leading slash omitted).
    file = user / 'User/globalStorage' / EXTENSION / 'nameConfigs' / (name.lstrip('/') + '.json')
    atomic_json(file, config)
    return user, extensions


def open_client(controller, name, remote_file=None):
    exe, cli = locate_code()
    base = controller.vscode_directory() / 'client'
    user, extensions = configure_client(base, name, getattr(controller, 'environment_name', 'Container'))
    args = ['--user-data-dir', str(user), '--extensions-dir', str(extensions)]
    env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
    env.pop('VSCODE_DEV', None)
    env.pop('SSH_AUTH_SOCK', None)
    for key in list(env):
        if key.upper().startswith(('ANTHROPIC_', 'CLAUDE_')):
            env.pop(key)
    def run_cli(extra, timeout=180):
        result = subprocess.run([str(exe), str(cli), *args, *extra], env=env, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('VS Code 依赖准备失败：' + (result.stderr or result.stdout).strip()[-1500:])
        return result.stdout
    installed = run_cli(['--list-extensions']).lower().splitlines()
    if EXTENSION not in installed:
        controller.log('首次连接：为 CCBOX 专用窗口安装 Microsoft Dev Containers，使用宿主机下载网络。')
        run_cli(['--install-extension', EXTENSION], timeout=600)
    controller.log('正在连接原沙箱并打开 /workspace；首次下载 VS Code Server 和 Claude 扩展可能需要几分钟。')
    # Open the GUI using its CLI with argument arrays; paths never enter cmd.exe.
    run_cli(['--new-window', '--file-uri' if remote_file else '--folder-uri', attach_uri(name, remote_file or '/workspace')])
    return ('已发起 VS Code 连接；打开当前环境的' + ('多项目工作区。\n' if remote_file else ' /workspace。\n') +
            '这是 CCBOX 专用配置窗口，不改动宿主机原有 VS Code 登录与设置。\n'
            '首次初始化完成后打开 Claude 面板；用「检查编辑器连接」确认后端实际运行位置。')
