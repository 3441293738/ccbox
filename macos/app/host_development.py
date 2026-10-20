"""Optional host bridge. Docker stdio only; no host server, sockets or auth import."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading

from .host_tools import HostJobs, ProjectLease
from .storage import atomic_json

SCRIPT = '/home/claude/.ccbox/runtime/hostdev_runtime.py'


def projects(config):
    return [{'id': 'main', 'name': '主项目', 'path': config.get('workspace', ''), 'remote': '/workspace',
             'readonly': config.get('readonly', False)}] + [
        {**p, 'remote': '/projects/' + p['id']} for p in config.get('projects', [])]


class Preferences:
    def __init__(self, directory): self.file = Path(directory)/'development.json'

    def read(self):
        if not self.file.exists(): return {'version': 1, 'selected': 'main', 'projects': {}}
        if self.file.stat().st_size > 65536: raise ValueError('开发模式配置过大，请检查 development.json。')
        data = json.loads(self.file.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('projects'), dict)
            or not isinstance(data.get('selected'), str)
            or any(not isinstance(row, dict) or row.get('mode') not in ('host','sandbox')
                   or not isinstance(row.get('path'), str) for row in data['projects'].values())):
            raise ValueError('开发模式配置格式异常；未修改原账号设置。')
        return data

    def mode(self, project):
        row = self.read()['projects'].get(project['id'], {})
        if row.get('path') != project['path'] or project.get('readonly'): return 'sandbox'
        return 'host' if row.get('mode') == 'host' else 'sandbox'

    def save(self, project, mode):
        if mode not in ('host', 'sandbox'): raise ValueError('未知开发模式。')
        if mode == 'host' and project.get('readonly'): raise ValueError('只读项目仅支持沙箱模式，本机执行不受挂载只读限制。')
        data = self.read(); data['selected'] = project['id']
        data['projects'][project['id']] = {'path': project['path'], 'mode': mode}
        atomic_json(self.file, data)


def docker_env():
    try:
        from .host_platform import docker_environment
        return docker_environment()
    except ImportError: return dict(os.environ)


class HostSession:
    def __init__(self, controller, project, approve):
        if project.get('readonly'): raise ValueError('只读项目仅支持沙箱模式。')
        self.c, self.project, self.approve = controller, dict(project), approve
        self.process = self.jobs = self.lease = None
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.write_lock = threading.Lock()
        self.socket = '/tmp/ccbox-hostdev-' + secrets.token_hex(12) + '.sock'
        self.token = secrets.token_urlsafe(32)

    def start(self, sandbox):
        from .sandbox_editor import verify_boundary
        verify_boundary(self.c, sandbox)
        if sandbox['Config'].get('Labels', {}).get('com.docker.compose.project') != self.c.credentials()['project']:
            raise RuntimeError('账号与容器身份不匹配。')
        candidates = projects(self.c.validate_config(self.c.config()))
        if self.project not in candidates: raise ValueError('项目配置已变化，请重新选择。')
        self.lease = ProjectLease(self.c.data_root/'host-project-locks', self.project['path'])
        self.jobs = HostJobs(self.project['path'], self.c.environment_name, self.approve, self.c.log)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            # Use immutable container ID, not compose service discovery, throughout
            # this connection so recreated/account-switched containers cannot inherit it.
            prefix = [self.c.docker(), 'exec', '-i', '-u', '1000:1000', sandbox['Id']]
            writer = ("import sys,pathlib,os; p=pathlib.Path('"+SCRIPT+"'); p.parent.mkdir(parents=True,exist_ok=True); "
                      "t=p.with_suffix('.tmp'); t.write_bytes(sys.stdin.buffer.read()); t.chmod(0o700); os.replace(t,p)")
            result = subprocess.run(prefix+['python3', '-c', writer], input=(self.c.root/'runtime/hostdev_runtime.py').read_bytes(),
                                    capture_output=True, timeout=30, env=docker_env(), creationflags=flags)
            if result.returncode: raise RuntimeError('本机开发组件写入失败，原项目与登录未改动。')
            self.process = subprocess.Popen(prefix+['python3', '-u', SCRIPT, 'relay'], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            env=docker_env(), creationflags=flags)
            self.send({'socket': self.socket, 'token': self.token})
            threading.Thread(target=self.read_loop, daemon=True).start()
            if not self.ready.wait(12) or self.closed.is_set(): raise RuntimeError('本机通道未建立，请检查 Docker 状态。')
            descriptor = '/home/claude/.ccbox/hostdev/'+self.project['id']+'.json'
            payload = {'descriptor': descriptor, 'endpoint': {'socket': self.socket, 'token': self.token}}
            result = subprocess.run(prefix[:3]+['-w', self.project['remote']]+prefix[3:]+['python3', SCRIPT, 'install'],
                                    input=(json.dumps(payload)+'\n').encode(), capture_output=True, timeout=45,
                                    env=docker_env(), creationflags=flags)
            if result.returncode: raise RuntimeError('本机工具注册未完成。请确认容器 Claude CLI 已安装；若存在同名 ccbox-host 配置，请先检查它。')
            if self.closed.is_set(): raise RuntimeError('本机通道已断开，请重新打开。')
            self.c.log('本机工具已连接：'+self.project['name']+'；每次命令需在 CCBOX 确认。模型代理保持原配置。')
        except Exception:
            self.close(); raise

    def send(self, value):
        with self.write_lock:
            self.process.stdin.write(json.dumps(value, ensure_ascii=True).encode()+b'\n'); self.process.stdin.flush()

    def read_loop(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(262145)
                if not line: break
                if len(line) > 262144 or not line.endswith(b'\n'): break
                msg = json.loads(line)
                if not isinstance(msg, dict): break
                if msg.get('ready') is True: self.ready.set(); continue
                key = msg.get('id')
                if not isinstance(key, str) or len(key) != 32: break
                try: response = {'id': key, 'result': self.jobs.call(msg.get('name'), msg.get('arguments', {}))}
                except Exception as e: response = {'id': key, 'error': str(e)}
                self.send(response)
        except (OSError, ValueError): pass
        finally:
            self.close()
            self.c.log('本机执行通道已断开；未自动重试命令。容器与账号资料保留。')

    def close(self):
        if self.closed.is_set(): return
        self.closed.set(); self.ready.set()
        if self.jobs: self.jobs.close()
        if self.process:
            try:
                with self.write_lock: self.process.stdin.close()
            except OSError: pass
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
        if self.lease: self.lease.close()
