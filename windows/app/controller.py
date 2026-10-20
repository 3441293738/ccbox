"""Local orchestrator. Shell-free Docker calls, separate credentials, explicit state."""
import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from . import vault
from .editor_paths import normalize_editor_directory, write_editor_configs
from .storage import data_location, migration_target, copy_verified, atomic_json, POINTER
from .environments import Environments
from .projects import normalize_projects, overlaps, mounts, signature, workspace_document, same_bind_source

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from transport import parse_proxy, fetch_json

ZONES = ['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
         'Europe/London', 'Europe/Berlin', 'Asia/Tokyo', 'Asia/Singapore', 'Australia/Sydney', 'UTC']
LANGUAGES = {'en_US.UTF-8': 'en-US,en', 'en_GB.UTF-8': 'en-GB,en', 'ja_JP.UTF-8': 'ja,en-US,en'}
DEFAULT = dict(workspace='', vscode_dir='', readonly=False, timezone='America/New_York', locale='en_US.UTF-8',
               desktop_port=6080, cpus=2, memory_gb=4, claude_version='latest')


def validate(config):
    c = {**DEFAULT, **config}
    if not isinstance(c['workspace'], str) or not Path(c['workspace']).expanduser().is_absolute():
        raise ValueError('请选择项目文件夹的完整绝对路径。')
    p = Path(c['workspace']).expanduser().resolve()
    if not c['workspace'] or not p.is_dir():
        raise ValueError('请选择已存在的项目目录')
    if p == Path(p.anchor) or p == Path.home() or p == ROOT or p in ROOT.parents or ROOT in p.parents:
        raise ValueError('请选择独立项目目录，不要挂载整块磁盘、用户目录或工具目录')
    if ROOT / 'data' == p or ROOT / 'data' in p.parents:
        raise ValueError('凭据目录不应挂载到沙箱')
    if c['timezone'] not in ZONES or c['locale'] not in LANGUAGES:
        raise ValueError('请选择列表中的时区与语言')
    for key, lo, hi in [('desktop_port', 1024, 65535), ('cpus', 1, 32), ('memory_gb', 2, 64)]:
        c[key] = int(c[key])
        if not lo <= c[key] <= hi:
            raise ValueError(f'{key} 应为 {lo}–{hi}')
    if not re.fullmatch(r'(latest|\d+\.\d+\.\d+)', c['claude_version']):
        raise ValueError('CLI 版本填写 latest 或 x.y.z')
    c['workspace'] = str(p)
    c['readonly'] = bool(c['readonly'])
    c['vscode_dir'] = normalize_editor_directory(c['vscode_dir'])
    c['projects'] = normalize_projects(c.get('projects', []), p, [ROOT])
    return c


def make_compose(root, data, config, project):
    """Pure function: generated config contains no proxy credentials or desktop token."""
    c = validate(config)
    root, data = Path(root), Path(data)
    def bind(src, dst, ro=True):
        return {'type': 'bind', 'source': str(src), 'target': dst, 'read_only': ro,
                'bind': {'create_host_path': False}}
    common = {'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'], 'read_only': True,
              'init': True, 'restart': 'no', 'logging': {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}}
    return {
        'name': project,
        'services': {
            'gateway': {**common, 'build': {'context': str(root / 'runtime'), 'dockerfile': 'Dockerfile.gateway'},
                        'image': f'{project}-gateway', 'pull_policy': 'never', 'mem_limit': '256m', 'pids_limit': 128,
                        'ports': [f'127.0.0.1:{c["desktop_port"]}:6080'],
                        'tmpfs': ['/tmp:rw,nosuid,nodev,noexec,size=32m'],
                        'volumes': ['ipc:/ipc', 'desktop:/desktop:ro',
                                    bind(root / 'runtime/desktop.html', '/opt/ccbox/desktop.html'),
                                    bind(data / 'active/upstream.json', '/run/secrets/upstream.json')],
                        'healthcheck': {'test': ['CMD', 'python3', '-c',
                                                'import socket;s=socket.socket(socket.AF_UNIX);s.settimeout(2);s.connect("/ipc/proxy.sock");s.close()'],
                                        'interval': '5s', 'timeout': '3s', 'retries': 6}},
            'sandbox': {**common, 'build': {'context': str(root / 'runtime'), 'dockerfile': 'Dockerfile.sandbox',
                                         'args': {'CLAUDE_VERSION': c['claude_version']}},
                        'image': f'{project}-sandbox', 'pull_policy': 'never', 'hostname': 'workspace', 'network_mode': 'none',
                        'user': '1000:1000', 'working_dir': '/workspace', 'shm_size': '512m',
                        'labels': {'io.ccbox.mounts': signature(c)},
                        'cpus': c['cpus'], 'mem_limit': f'{c["memory_gb"]}g', 'pids_limit': 1024,
                        'environment': {'TZ': c['timezone'], 'LANG': c['locale'], 'LC_ALL': c['locale'],
                                        'CCBOX_LANGUAGES': LANGUAGES[c['locale']]},
                        'tmpfs': ['/tmp:rw,nosuid,nodev,size=1g,mode=1777', '/run:rw,nosuid,nodev,size=32m,uid=1000,gid=1000'],
                        'volumes': ['home:/home/claude', 'ipc:/ipc:ro', 'desktop:/desktop',
                                    *[bind(m['source'], m['target'], m['read_only']) for m in mounts(c)],
                                    bind(data / 'resolv.conf', '/etc/resolv.conf'),
                                    bind(data / 'firefox-policies.json', '/usr/lib/firefox-esr/distribution/policies.json')],
                        'depends_on': {'gateway': {'condition': 'service_healthy'}}}},
        'volumes': {'home': {}, 'ipc': {}, 'desktop': {}}
    }


class Controller:
    def __init__(self, root=None, environment_id=None):
        self.root = (Path(root) if root else ROOT).resolve()
        catalog = Environments(self.root)
        self.environment_id = environment_id or catalog.read()['selected']
        self._set_data(catalog.path(self.environment_id))
        self.log = lambda text: None

    @property
    def environment_name(self):
        return next(i['name'] for i in Environments(self.root).read()['items'] if i['id'] == self.environment_id)

    def _set_data(self, path):
        self.data = Path(path)
        self.data_root = self.data if self.environment_id == 'default' else self.data.parent.parent
        self.file = self.data / 'compose.json'
        self.config_file = self.data / 'settings.json'
        self.vault_file = self.data / 'credentials.dpapi'

    def check_location(self):
        if data_location(self.root) != self.data_root:
            raise RuntimeError('数据位置已由其他窗口更改，请重新打开 CCBOX 后继续。')

    def validate_config(self, config):
        c = validate(config)
        protected = [self.data_root, self.root]
        catalog = Environments(self.root)
        for item in catalog.read()['items']:
            other = self if item['id'] == self.environment_id else Controller(self.root, item['id'])
            editor = c['vscode_dir'] if other is self else other.config().get('vscode_dir')
            if editor:
                protected.append(Path(editor))
        if any(overlaps(c['workspace'], p) for p in protected):
            raise ValueError('项目目录与工具、数据或编辑器资料目录重叠；请选择独立项目目录。')
        c['projects'] = normalize_projects(c.get('projects', []), c['workspace'], protected)
        # Never place an editor profile inside any environment's shared projects.
        if c['vscode_dir']:
            for item in catalog.read()['items']:
                other_config = c if item['id'] == self.environment_id else Controller(self.root, item['id']).config()
                for m in mounts(other_config):
                    if m['source'] and overlaps(c['vscode_dir'], m['source']):
                        raise ValueError('VS Code 资料目录与某个环境的项目重叠，请选择独立位置。')
        selected_base = Path(c['vscode_dir']) if c['vscode_dir'] else self.data / 'vscode'
        selected_dir = selected_base / 'environments' / self.environment_id if c['vscode_dir'] and self.environment_id != 'default' else selected_base
        for item in catalog.read()['items']:
            if item['id'] != self.environment_id:
                other = Controller(self.root, item['id'])
                if overlaps(selected_dir / 'client', other.vscode_directory() / 'client'):
                    raise ValueError('此路径会与另一个账号环境共用 VS Code 资料，请更换基础目录。')
        return c

    def check_allocation(self, config, project=None):
        for item in Environments(self.root).read()['items']:
            if item['id'] == self.environment_id:
                continue
            other = Controller(self.root, item['id'])
            if int(other.config()['desktop_port']) == int(config['desktop_port']):
                raise ValueError('桌面端口已分配给其他环境，请使用不同端口。')
            if project and other.vault_file.exists() and other.credentials().get('project') == project:
                raise RuntimeError('两个环境指向同一 Docker 项目标识，请恢复独立保险库。')

    def config(self):
        if self.config_file.exists():
            return {**DEFAULT, **json.loads(self.config_file.read_text(encoding='utf-8'))}
        if self.vault_file.exists():
            raise RuntimeError('已存在账号环境的设置文件缺失，请恢复 settings.json 后继续。')
        return dict(DEFAULT)

    def credentials(self):
        if not self.vault_file.exists() and self.file.exists():
            raise RuntimeError('已有容器配置的保险库缺失，请恢复 credentials.dpapi，避免更换原登录卷。')
        return vault.load(self.vault_file) if self.vault_file.exists() else {}

    def save(self, config, proxy):
        self.check_location()
        c = self.validate_config(config)
        # Validate independently of the GUI; no command-line strings generated from it.
        url = f'{proxy["scheme"]}://{quote(proxy["username"], safe="")}:{quote(proxy["password"], safe="")}@'
        host = f'[{proxy["host"]}]' if ':' in proxy['host'] else proxy['host']
        proxy = parse_proxy(url + host + ':' + str(proxy['port']))
        vault.protect_directory(self.data)
        try:
            old = self.credentials()
        except Exception:
            raise RuntimeError('原凭据读取失败，已保留原配置与环境标识。请恢复保险库后再保存，避免创建空白环境。') from None
        project = old.get('project', 'ccbox2-' + secrets.token_hex(4))
        self.check_allocation(c, project)
        vault.save(self.vault_file, {'proxy': proxy, 'project': project,
                                    'desktop_token': old.get('desktop_token', secrets.token_urlsafe(32))})
        atomic_json(self.config_file, c)
        return ('设置已保存；凭据由 Windows 当前用户 DPAPI 加密。\n'
                '仅修改 VS Code 配置目录：下次生成即生效，无需重启容器。\n'
                '修改代理、项目或容器环境：停止后重新启动以应用更改。')

    def docker(self):
        path = shutil.which('docker')
        if not path:
            candidates = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Docker/Docker/resources/bin/docker.exe',
                          Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) /
                          'Programs/DockerDesktop/resources/bin/docker.exe']
            path = next((str(p) for p in candidates if p.is_file()), None)
        if not path:
            raise RuntimeError('未找到 Docker Desktop。请安装、启动并切换到 Linux containers。')
        return path

    def run(self, args, timeout=90):
        try:
            p = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace',
                               timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired:
            raise RuntimeError('命令超时；请检查 Docker Desktop 状态') from None
        if p.returncode:
            # Commands never include credentials; Docker error paths may expose local usernames only.
            message = (p.stderr or p.stdout).strip()[-4000:]
            raise RuntimeError(message or f'命令失败，退出码 {p.returncode}')
        return p.stdout.strip()

    def command(self, *args):
        self.check_location()
        if not self.file.exists():
            raise RuntimeError('请先保存配置，然后构建或启动')
        project = self.credentials().get('project', '')
        if not re.fullmatch(r'ccbox2-[a-z0-9-]{4,64}', project) or json.loads(self.file.read_text(encoding='utf-8')).get('name') != project:
            raise RuntimeError('Compose 与当前环境身份不一致，已阻止操作；请恢复对应配置和保险库。')
        return [self.docker(), 'compose', '-p', project, '-f', str(self.file), *args]

    def doctor(self):
        exe = self.docker()
        version = self.run([exe, 'version', '--format', '{{.Server.Version}}'])
        kind = self.run([exe, 'info', '--format', '{{.OSType}}'])
        compose = self.run([exe, 'compose', 'version', '--short'])
        if kind != 'linux':
            raise RuntimeError('Docker 当前不是 Linux containers 模式')
        return f'Docker Engine {version} / Compose {compose} / {kind}'

    def prepare(self):
        self.check_location()
        c = self.validate_config(self.config())
        cred = self.credentials()
        if not cred:
            raise RuntimeError('请先配置代理并保存')
        self.check_allocation(c, cred['project'])
        # Do not rotate or rewrite secrets under active bind mounts.
        if self.file.exists() and self.run(self.command('ps', '-q')):
            raise RuntimeError('请先停止现有容器，再构建或启动新配置')
        vault.protect_directory(self.data / 'active')
        secret_file = self.data / 'active/upstream.json'
        secret_file.write_text(json.dumps({'proxy': cred['proxy'], 'desktop_token': cred['desktop_token'],
                                          'desktop_port': c['desktop_port']}), encoding='utf-8')
        (self.data / 'resolv.conf').write_text('nameserver 127.0.0.1\noptions timeout:1 attempts:1\n', encoding='ascii')
        policies = json.loads((self.root / 'runtime/firefox-policies.json').read_text(encoding='utf-8'))
        policies['policies']['Preferences']['intl.accept_languages']['Value'] = LANGUAGES[c['locale']]
        (self.data / 'firefox-policies.json').write_text(json.dumps(policies), encoding='utf-8')
        compose = make_compose(self.root, self.data, c, cred['project'])
        self.file.write_text(json.dumps(compose, ensure_ascii=False, indent=2), encoding='utf-8')
        return c

    def missing_images(self):
        """An empty successful listing means missing; daemon errors must propagate."""
        services = json.loads(self.file.read_text(encoding='utf-8'))['services']
        missing = []
        for name, service in services.items():
            # These are locally generated, untagged project images, not registry names.
            ref = service['image'] + ':latest'
            found = self.run([self.docker(), 'image', 'ls', '--quiet', '--filter', f'reference={ref}'])
            if not found:
                missing.append(name)
        return missing

    def build_images(self, services=()):
        """Build prepared config without preparing twice or deleting startup secrets."""
        self.log('首次构建需要下载 Linux 镜像、浏览器和 Claude Code；这是 Docker 宿主机的构建网络。')
        # Drain stdout continuously so progress appears without blocking the GUI.
        with subprocess.Popen(self.command('--progress', 'plain', 'build', *services), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as p:
            for line in p.stdout:
                self.log(line.rstrip())
            if p.wait():
                raise RuntimeError('镜像构建失败。请查看操作记录中的下载或构建错误，修正后重新启动。')
        missing = self.missing_images()
        if missing:
            raise RuntimeError('构建结束但本地镜像仍缺失：' + '、'.join(missing) + '。请检查 Docker 镜像存储与构建日志。')

    def build(self):
        self.doctor()
        self.prepare()
        try:
            self.build_images()
        finally:
            self.clear_runtime_secret()
        return '镜像构建完成。点击启动环境。'

    def start(self):
        self.doctor()
        self.prepare()
        try:
            self.log('正在检查本地工作区镜像…')
            missing = self.missing_images()
            if missing:
                self.log('缺少本地镜像：' + '、'.join(missing) + '。自动构建后继续启动，请等待。')
                self.build_images(missing)
            else:
                self.log('本地镜像已就绪，跳过构建。')
            # Project images exist only locally. Never try pulling them from Docker Hub.
            self.log('正在启动容器并等待网关健康检查…')
            self.run(self.command('up', '-d', '--no-build', '--pull', 'never',
                                  '--wait', '--wait-timeout', '120'), timeout=180)
            # Server health does not prove proxy egress or browser isolation.
            return '环境已启动；未改配置时复用原容器，项目和账号资料持续保留。'
        except Exception:
            # Preserve secret if any container remains alive; stop button can recover.
            try:
                self.stop()
            except Exception:
                pass
            raise

    def stop(self):
        self.check_location()
        self.run(self.command('stop', '--timeout', '15'), timeout=120)
        self.clear_runtime_secret()
        return '环境已停止并保留容器、项目和登录卷。运行时明文凭据已清空；下次启动继续使用。'

    def clear_runtime_secret(self):
        # Keep the bind-mounted file inode stable for stop/start reuse.
        secret = self.data / 'active/upstream.json'
        if secret.is_file():
            secret.write_text('', encoding='utf-8')

    def migrate_data(self, target):
        self.check_location()
        if len(Environments(self.root).read()['items']) > 1:
            from .environment_migration import migrate_all
            return migrate_all(self, target)
        old = self.data
        c = self.config()
        target = migration_target(target, old, c.get('workspace'), self.root)
        credential_before = self.credentials()  # Validate DPAPI before making changes.
        if not credential_before or not credential_before.get('project'):
            raise RuntimeError('请先保存一份完整配置，再迁移数据。')
        running = bool(self.file.exists() and self.run(self.command('ps', '-q')))
        if running:
            self.stop()
        old_pointer = (self.root / POINTER).read_bytes() if (self.root / POINTER).exists() else None
        try:
            self.log('1/3 复制并校验配置、保险库、编辑器资料；原目录保留。')
            count = copy_verified(old, target)
            self._set_data(target)
            if self.credentials() != credential_before:
                raise RuntimeError('迁移后凭据校验失败。')
            moved_config = self.config()
            explicit = moved_config.get('vscode_dir')
            if explicit and Path(explicit).resolve().is_relative_to(old):
                moved_config['vscode_dir'] = str(target / Path(explicit).resolve().relative_to(old))
                atomic_json(self.config_file, moved_config)
            for config_file in target.rglob('devcontainer.json'):
                if config_file.parent.name != '.devcontainer':
                    continue
                try:
                    generated = json.loads(config_file.read_text(encoding='utf-8'))
                    if generated.get('name') == 'CCBOX isolated workspace' and generated.get('dockerComposeFile') == str(old / 'compose.json'):
                        generated['dockerComposeFile'] = str(self.file)
                        atomic_json(config_file, generated)
                except (ValueError, AttributeError):
                    pass
            # Rewrite generated paths, but keep the same project ID and Docker volumes.
            compose = make_compose(self.root, target, self.validate_config(self.config()), credential_before['project'])
            atomic_json(self.file, compose)
            self.log('2/3 提交新位置；Docker 镜像和账号卷标识不变。')
            atomic_json(self.root / POINTER, {'version': 1, 'data_dir': str(target), 'previous_data_dir': str(old)})
            self.log('3/3 校验位置并恢复原运行状态。')
            if running:
                self.start()
            return f'数据已迁移到 {target}\n原目录保留为备份：{old}\n已复制并校验 {count} 个文件；项目和登录卷保持不变。请关闭旧 VS Code 窗口后重新连接。'
        except Exception:
            if self.data == target and data_location(self.root) == target and running:
                try:
                    self.stop()
                except Exception as cleanup:
                    self.log('新位置环境清理需检查：' + str(cleanup))
            self._set_data(old)
            pointer = self.root / POINTER
            if old_pointer is None:
                pointer.unlink(missing_ok=True)
            else:
                atomic_json(pointer, json.loads(old_pointer))
            if running:
                try:
                    self.start()
                except Exception as recovery:
                    self.log('原环境恢复需手动处理：' + str(recovery))
            raise

    def state(self):
        if not self.file.exists():
            return '尚未创建环境'
        result = self.run(self.command('ps', '-a', '--format', 'json'))
        if not result:
            return '已停止'
        try:
            if result.lstrip().startswith('['):
                rows = json.loads(result)
            else:
                rows = [json.loads(line) for line in result.splitlines()]
            return ' / '.join(f'{r.get("Service", "container")}：{r.get("State", "unknown")}' for r in rows)
        except (ValueError, TypeError):
            return result[:500]

    def audit(self):
        value = self.run(self.command('exec', '-T', 'sandbox', 'python3', '/opt/ccbox/audit.py'), timeout=100)
        rows = json.loads(value)
        report = {'at': time.strftime('%Y-%m-%d %H:%M:%S %z'), 'checks': rows}
        (self.data / 'last-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return '\n'.join(f'[{r["result"]}] {r["check"]}：{r["detail"]}' for r in rows)

    def outage_test(self):
        self.run(self.command('stop', 'gateway'))
        try:
            value = self.audit()
            # Proxy failure is EXPECTED in this deliberately disconnected mode.
            return '【断网测试：代理出口应为 FAIL，直连/UDP 阻断应为 PASS】\n' + value
        finally:
            self.run(self.command('start', 'gateway'), timeout=90)

    def browser(self):
        active = self.data / 'active/upstream.json'
        if not active.exists() or not active.stat().st_size:
            raise RuntimeError('请先启动环境')
        config = json.loads(active.read_text(encoding='utf-8'))
        # Actual sites are visited only by Firefox inside the container.
        webbrowser.open(f'http://127.0.0.1:{config["desktop_port"]}/vnc.html?autoconnect=1&resize=scale&access={config["desktop_token"]}')
        return '已打开沙箱桌面。此窗口显示远程画面，网页在容器 Firefox 中运行。'

    def open_url(self, url):
        if not url.startswith(('https://', 'http://', 'file:///opt/ccbox/')):
            raise ValueError('请输入 http(s) URL')
        self.run(self.command('exec', '-d', 'sandbox', '/usr/local/bin/ccbox-browser', url))
        return self.browser()

    def terminal(self, command='claude'):
        if command not in ('claude', 'bash'):
            raise ValueError('unsupported terminal command')
        # Docker receives argv directly; the new console is intentionally interactive.
        subprocess.Popen(self.command('exec', 'sandbox', command),
                         creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
        return '已打开独立终端。首次登录跟随官方 CLI 提示；授权网页在沙箱浏览器中打开。'

    def vscode_directory(self):
        selected = normalize_editor_directory(self.config().get('vscode_dir', ''))
        if not selected:
            return self.data / 'vscode'
        base = Path(selected)
        # Legacy environment keeps its current profile/login/window continuity.
        return base if self.environment_id == 'default' else base / 'environments' / self.environment_id

    def generate_devcontainer(self):
        if not self.file.exists():
            raise RuntimeError('请先构建并启动环境')
        # Separate bootstrap folder; never overwrite the user's .devcontainer.
        folder = self.vscode_directory()
        config = {
            'name': 'CCBOX isolated workspace', 'dockerComposeFile': str(self.file), 'service': 'sandbox',
            'workspaceFolder': '/workspace', 'shutdownAction': 'none', 'overrideCommand': False,
            'remoteUser': 'claude', 'updateRemoteUserUID': False, 'forwardPorts': [],
            'remoteEnv': {'HTTP_PROXY': 'http://127.0.0.1:18080', 'HTTPS_PROXY': 'http://127.0.0.1:18080',
                          'http_proxy': 'http://127.0.0.1:18080', 'https_proxy': 'http://127.0.0.1:18080',
                          'BROWSER': '/usr/local/bin/ccbox-browser', 'SSH_AUTH_SOCK': ''},
            'customizations': {'vscode': {'extensions': ['anthropic.claude-code'], 'settings': {
                'http.proxy': 'http://127.0.0.1:18080', 'http.proxySupport': 'override',
                'remote.autoForwardPorts': False, 'git.autofetch': False}}}}
        # Verify these controls in the installed Dev Containers version; see README.
        settings = {
            'dev.containers.copyGitConfig': False, 'dev.containers.gitCredentialHelperConfigLocation': 'none',
            'dev.containers.mountWaylandSocket': False, 'remote.autoForwardPorts': False}
        return write_editor_configs(folder, self.file, config, settings)

    def devcontainer(self):
        folder = self.generate_devcontainer()
        os.startfile(str(folder))
        return f'已生成 {folder}\n用 VS Code 打开该文件夹，再执行 Dev Containers: Reopen in Container。\n请确认扩展安装在 CCBOX 容器端；宿主机扩展与宿主浏览器不受本工具约束。\n插件登录若跳到宿主浏览器，请把授权链接复制到沙箱浏览器继续。'

    def editor_context(self):
        """Validate identity and actual mounts before either editor entry point."""
        self.check_location()
        ids = self.run(self.command('ps', '-q', 'sandbox')) if self.file.exists() else ''
        if not ids:
            self.start()
            ids = self.run(self.command('ps', '-q', 'sandbox'))
        if len(ids.splitlines()) != 1:
            raise RuntimeError('未找到唯一的工作容器；请先检查环境状态。')
        info = json.loads(self.run([self.docker(), 'inspect', ids.strip()]))[0]
        if info['Config'].get('Labels', {}).get('com.docker.compose.project') != self.credentials()['project']:
            raise RuntimeError('容器不属于当前账号环境，已停止连接。')
        if info['HostConfig']['NetworkMode'] != 'none' or info['Config']['User'] not in ('1000:1000', 'claude'):
            raise RuntimeError('工作容器的网络或用户边界与预期不符，请先运行隔离检查。')
        if not any(m['Destination'] == '/workspace' for m in info['Mounts']):
            raise RuntimeError('容器未挂载项目目录，请先检查环境配置。')
        # Docker Desktop may translate Windows bind sources; authoritative saved Compose is also checked.
        current = json.loads(self.file.read_text(encoding='utf-8'))
        config = self.validate_config(self.config())
        expected = mounts(config)
        recorded = [{k: m[k] for k in ('source', 'target', 'read_only')} for m in current['services']['sandbox']['volumes']
                    if isinstance(m, dict) and (m['target'] == '/workspace' or m['target'].startswith('/projects/'))]
        if recorded != expected:
            raise RuntimeError('项目挂载已修改但尚未应用，请停止后重新启动当前环境再连接。')
        if config['projects'] and info['Config'].get('Labels', {}).get('io.ccbox.mounts') != signature(config):
            raise RuntimeError('运行容器仍使用旧挂载，请停止后重新启动当前环境。')
        for m in expected:
            actual = next((a for a in info['Mounts'] if a['Destination'] == m['target']), None)
            if not actual or actual['Type'] != 'bind' or bool(actual['RW']) == m['read_only'] or not same_bind_source(actual['Source'], m['source']):
                raise RuntimeError('项目挂载的路径或读写权限与配置不符，请重启当前环境。')
        remote_file = None
        if config['projects']:
            remote_file = '/home/claude/.ccbox/workspaces/projects.code-workspace'
            payload = base64.b64encode(json.dumps(workspace_document(config), ensure_ascii=False).encode()).decode()
            script = ("import pathlib,base64,os; p=pathlib.Path('/home/claude/.ccbox/workspaces/projects.code-workspace'); "
                      "p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix('.tmp'); "
                      "t.write_bytes(base64.b64decode('" + payload + "')); os.replace(t,p)")
            self.run(self.command('exec', '-T', 'sandbox', 'python3', '-c', script))
        return info, remote_file

    def open_vscode(self):
        from .editor_launch import open_client
        info, remote_file = self.editor_context()
        return open_client(self, info['Name'], remote_file)

    def open_sandbox_vscode(self, allow_upgrade=False):
        from .sandbox_editor import open_editor
        return open_editor(self, allow_upgrade)

    def sandbox_editor_status(self):
        from .sandbox_editor import status
        return status(self)

    def editor_status(self):
        script = """import pathlib,json
rows=[]
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  if int(p.parent.name)==__import__('os').getpid(): continue
  args=p.read_bytes().replace(b'\\0',b' ').decode(errors='replace')
  if 'extensionHost' in args or ('anthropic.claude-code-' in args and '/claude' in args): rows.append(args)
 except OSError: pass
print(json.dumps({'extension_host':any('extensionHost' in x for x in rows),'claude_backend':any('anthropic.claude-code-' in x and '/claude' in x for x in rows),'extension_installed':bool(list(pathlib.Path('/home/claude/.vscode-server/extensions').glob('anthropic.claude-code-*')))}))"""
        result = json.loads(self.run(self.command('exec', '-T', 'sandbox', 'python3', '-c', script)))
        return '\n'.join(('容器端 VS Code Server：' + ('运行中' if result['extension_host'] else '未检测到'),
                          '容器端 Claude 扩展：' + ('已安装' if result['extension_installed'] else '等待安装'),
                          '容器端 Claude 后端：' + ('运行中' if result['claude_backend'] else '请打开 Claude 面板后再检查'),
                          '本检查不代表账号授权已完成；代理连通请同时运行隔离检查。'))

    @staticmethod
    def test_proxy(proxy):
        async def go():
            start = time.monotonic()
            ip = await fetch_json(proxy, 'api.ipify.org', '/?format=json')
            result = f'代理连通成功 / {proxy["scheme"]} / 出口 {ip["ip"]} / {time.monotonic() - start:.1f}s'
            # Optional geo lookup through the SAME upstream, never as a direct fallback.
            try:
                geo = await fetch_json(proxy, 'ipwho.is', '/' + ip['ip'])
                if geo.get('success'):
                    result += f'\n地理数据库：{geo.get("country", "")} / {geo.get("city", "")} / {geo.get("timezone", {}).get("id", "")}'
            except Exception:
                result += '\n地理数据库查询未完成；不影响本次连通性结果。'
            return result + '\n仅验证上游代理；不代表容器泄漏检查通过或账号状态保证。'
        return asyncio.run(go())
