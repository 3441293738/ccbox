"""Host orchestration only; all native editor code and state stay in Docker."""
import json
import os
import subprocess
import webbrowser
from urllib.parse import urlencode

from .storage import atomic_json
from .host_platform import docker_environment

REVISION = '3'
LABEL = 'io.ccbox.desktop-editor'
SCRIPT = '/home/claude/.ccbox/runtime/sandbox_editor.py'


def ready(controller):
    try:
        saved = json.loads((controller.data/'sandbox-editor-ready.json').read_text())
        return saved['revision'] == REVISION and saved.get('restore_toolbar') == 1
    except (OSError, ValueError, KeyError):
        return False


def capable(controller, sandbox):
    if sandbox['Config'].get('Labels', {}).get(LABEL) != REVISION:
        return False
    ids = controller.run(controller.command('ps', '-q', 'gateway'))
    if len(ids.splitlines()) != 1:
        return False
    gateway = json.loads(controller.run([controller.docker(), 'inspect', ids]))[0]
    return gateway['Config'].get('Labels', {}).get(LABEL) == REVISION


def upgrade(controller):
    """Build first, then replace containers without changing any volume identity.

    Recoverable old image tags remain after a successful update. A build failure
    does not stop the running workspace. Runtime failures restore the old images.
    """
    services = json.loads(controller.file.read_text(encoding='utf-8'))['services']
    backups = {}
    for service in ('sandbox', 'gateway'):
        image = services[service]['image'] + ':latest'
        image_id = controller.run([controller.docker(), 'image', 'inspect', '--format', '{{.Id}}', image])
        backup = services[service]['image'] + ':before-desktop-v3'
        controller.run([controller.docker(), 'tag', image_id, backup])
        backups[image] = backup
    controller.log('1/4 准备桌面依赖镜像；当前环境暂时继续运行。镜像构建使用 Docker 的下载网络。')
    try:
        controller.build_images(['sandbox', 'gateway'])
    except Exception:
        for image, backup in backups.items():
            controller.run([controller.docker(), 'tag', backup, image])
        raise
    controller.log('依赖已准备。重启当前沙箱；保留 home 卷、项目、代理配置及登录资料。')
    try:
        controller.stop()
        controller.start()
    except Exception as first:
        try:
            controller.stop()
            for image, backup in backups.items():
                controller.run([controller.docker(), 'tag', backup, image])
            controller.start()
        except Exception:
            raise RuntimeError('桌面升级未完成，自动恢复也未完成。原镜像 before-desktop-v3 和登录卷均已保留，请检查 Docker 日志。') from first
        raise RuntimeError('桌面升级启动失败，已恢复原镜像与环境；登录卷保持不变。') from first


def stream(controller, args):
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding='utf-8', errors='replace', env=docker_environment(),
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as process:
        tail = []
        for line in process.stdout:
            value = line.rstrip()
            controller.log(value)
            tail = (tail + [value])[-8:]
        if process.wait():
            raise RuntimeError('沙箱编辑器准备未完成，可检查代理后重试。\n' + '\n'.join(tail))


def deploy_runtime(controller):
    """Update our helper atomically, without a container restart or host mount."""
    for name in ('window_restore.py', 'sandbox_editor.py'):
        source = (controller.root/'runtime'/name).read_bytes()
        destination = str(__import__('pathlib').PurePosixPath(SCRIPT).with_name(name))
        writer = ("import pathlib,sys,os; p=pathlib.Path('" + destination + "'); "
                  "p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix('.tmp'); "
                  "t.write_bytes(sys.stdin.buffer.read()); t.chmod(0o700); os.replace(t,p)")
        result = subprocess.run(controller.command('exec', '-T', 'sandbox', 'python3', '-c', writer),
                                input=source, capture_output=True, timeout=30, env=docker_environment(),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('沙箱编辑器运行组件写入失败，请检查容器的 home 卷权限。')


def ensure_viewer_mount(controller, allow_update=False):
    """Old gateways need a one-time, gateway-only recreation for the HTML bind.

    Preserve secrets, image identities and every sandbox/home volume. Subsequent
    toolbar changes come from this single read-only static file, not an exec API.
    """
    import copy
    target = '/opt/ccbox/desktop.html'
    desired = {'type':'bind', 'source':str(controller.root/'runtime/desktop.html'),
               'target':target, 'read_only':True, 'bind':{'create_host_path':False}}
    previous = json.loads(controller.file.read_text(encoding='utf-8'))
    ids = controller.run(controller.command('ps', '-q', 'gateway'))
    if len(ids.splitlines()) != 1:
        raise RuntimeError('请先启动当前环境的网关。')
    info = json.loads(controller.run([controller.docker(), 'inspect', ids]))[0]
    existing = previous['services']['gateway']['volumes']
    mounted = any(m.get('Destination') == target and m.get('Type') == 'bind' and not m.get('RW')
                  for m in info.get('Mounts', []))
    if desired in existing and mounted:
        return
    if not allow_update:
        raise RuntimeError('桌面工具栏需要更新，请在 CCBOX 点击「准备 / 修复」确认短暂重连。')
    current = copy.deepcopy(previous)
    volumes = current['services']['gateway']['volumes']
    volumes[:] = [v for v in volumes if not (isinstance(v, dict) and v.get('target') == target)]
    volumes.append(desired)
    atomic_json(controller.file.with_name('compose.before-restore-toolbar.json'), previous)
    atomic_json(controller.file, current)
    args = ('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '60', 'gateway')
    controller.log('更新桌面工具栏：仅网关短暂重连，VS Code、终端和登录资料保持原样。')
    try:
        controller.run(controller.command(*args), timeout=120)
    except Exception as first:
        atomic_json(controller.file, previous)
        try:
            controller.run(controller.command(*args), timeout=120)
        except Exception:
            raise RuntimeError('工具栏更新未完成，已还原连接配置；请检查网关状态。沙箱未停止。') from first
        raise RuntimeError('工具栏更新未完成，已恢复原网关配置；沙箱未停止。') from first


def open_viewer(controller):
    active = json.loads((controller.data/'active/upstream.json').read_text(encoding='utf-8'))
    query = urlencode({'environment':controller.environment_name, 'access':active['desktop_token']})
    # Local-only picture/input endpoint; never open a host VS Code or OAuth URL.
    return webbrowser.open(f'http://127.0.0.1:{int(active["desktop_port"])}/desktop.html?' + query)


def open_editor(controller, allow_upgrade=False, *, before_launch=None, selected_workspace=None):
    controller.log('1/4 检查当前账号环境和项目挂载…')
    sandbox, remote_file = controller.editor_context()
    verify_boundary(controller,sandbox)
    if not capable(controller, sandbox):
        if not allow_upgrade:
            raise RuntimeError('沙箱需要更新桌面组件。请使用「准备 / 修复」并确认重启当前环境。')
        upgrade(controller)
        sandbox, remote_file = controller.editor_context()
        verify_boundary(controller,sandbox)
        if not capable(controller, sandbox):
            raise RuntimeError('镜像更新后仍缺少桌面组件；已保留资料，请检查运行时目录。')
    controller.log('2/4 准备原生 Linux VS Code 和官方 Claude 扩展；下载经过当前沙箱代理。')
    deploy_runtime(controller)
    if selected_workspace:
        from .host_development import projects
        if selected_workspace not in {p['remote'] for p in projects(controller.config())}:
            raise ValueError('所选项目不属于当前账号环境。')
    if before_launch:
        before_launch(sandbox)
    stream(controller, controller.command('exec', '-T', 'sandbox', 'python3', '-u', SCRIPT, 'open',
           '--label', controller.environment_name, '--workspace', selected_workspace or remote_file or '/workspace'))
    controller.log('3/4 编辑器界面已在容器运行；不启动宿主机 VS Code。')
    ensure_viewer_mount(controller, allow_upgrade)
    atomic_json(controller.data/'sandbox-editor-ready.json', {'revision':REVISION, 'restore_toolbar':1})
    controller.log('4/4 打开本机的沙箱画面窗口…')
    if not open_viewer(controller):
        return '沙箱 VS Code 已启动；桌面窗口未自动打开，请点击「沙箱浏览器」进入同一个桌面。'
    return ('已打开沙箱 VS Code。项目、界面和插件均在当前容器中运行。\n'
            '首次请自行确认工作区信任，然后点击顶部「Claude 面板」。已有容器登录资料继续使用。\n'
            '窗口最小化：点击页面顶部「恢复 VS Code」。窗口已关闭：回到 CCBOX 点「打开 / 恢复 VS Code」。')


def verify_boundary(controller, sandbox):
    config=sandbox['HostConfig']
    if config.get('Privileged',False):
        raise RuntimeError('检测到特权容器，已停止启动编辑器。')
    if (not config.get('ReadonlyRootfs') or 'ALL' not in config.get('CapDrop',[]) or
            not set(config.get('SecurityOpt',[])).intersection({'no-new-privileges','no-new-privileges:true'})):
        raise RuntimeError('容器的只读根、能力限制或提权限制与预期不符。请检查隔离配置。')
    project=controller.credentials()['project']
    from .projects import mounts
    allowed={'/home/claude','/ipc','/desktop','/etc/resolv.conf','/usr/lib/firefox-esr/distribution/policies.json'}
    allowed.update(m['target'] for m in mounts(controller.config()))
    if any(m['Destination'] not in allowed for m in sandbox['Mounts']):
        raise RuntimeError('发现配置外的额外挂载，已停止启动沙箱编辑器。')
    for target,name,rw in [('/home/claude','home',True),('/ipc','ipc',False),('/desktop','desktop',True)]:
        mount=next((m for m in sandbox['Mounts'] if m['Destination']==target),{})
        if mount.get('Type')!='volume' or mount.get('Name')!=project+'_'+name or bool(mount.get('RW'))!=rw:
            raise RuntimeError('登录卷或通信卷不属于当前环境，已停止连接。')


def status(controller):
    # Inspection must not start a stopped container or initialize another account.
    ids = controller.run(controller.command('ps', '-q', 'sandbox'))
    if not ids:
        return '当前环境已停止；启动沙箱 VS Code 后再检查。'
    info = json.loads(controller.run([controller.docker(), 'inspect', ids]))[0]
    if info['Config'].get('Labels', {}).get(LABEL) != REVISION:
        return '当前环境尚未准备沙箱 VS Code。请点击「打开沙箱 VS Code」。'
    path = SCRIPT if controller.run(controller.command('exec', '-T', 'sandbox', 'python3', '-c',
        "from pathlib import Path;print(Path('"+SCRIPT+"').is_file())")) == 'True' else '/opt/ccbox/sandbox_editor.py'
    result = json.loads(controller.run(controller.command('exec', '-T', 'sandbox', 'python3', path, 'status')))
    labels = {'desktop_main':'VS Code 主程序', 'desktop_renderer':'VS Code 界面 / Webview',
              'desktop_extension_host':'沙箱编辑器扩展宿主', 'desktop_claude_backend':'沙箱编辑器 Claude 后端'}
    lines = [name+'：'+('已在容器检测到' if result.get(key) else '尚未检测到') for key,name in labels.items()]
    lines += ['Claude 扩展：'+('已安装' if result.get('extension_installed') else '尚未安装'),
              '主程序代理配置：'+('已检测到显式代理参数' if result.get('desktop_proxy_argument') else '待检查'),
              '容器网络：'+info['HostConfig']['NetworkMode'],
              '配置与进程检测不等于实际模型请求审计；本操作不会发送模型消息。']
    return '\n'.join(lines)
