"""Explicitly approved host jobs. CWD is context, not an OS security boundary."""
import base64
import codecs
import hashlib
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import subprocess
import threading
import time

TOOLS = ('java', 'mvn', 'gradle', 'node', 'npm', 'python3', 'python', 'go', 'cargo', 'dotnet', 'git', 'docker', 'xcodebuild')
TERMINAL = {'completed', 'failed', 'denied', 'cancelled', 'timed_out'}


def host_environment():
    # Do not copy model keys, proxy credentials, SSH agents, cloud tokens or the
    # entire GUI process environment. Programs can still read user-owned files.
    allowed = {'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'COMSPEC',
               'TEMP', 'TMP', 'TMPDIR', 'PATHEXT', 'APPDATA', 'LOCALAPPDATA',
               'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA', 'JAVA_HOME',
               'JDK_HOME', 'MAVEN_HOME', 'M2_HOME', 'GRADLE_HOME', 'GOROOT', 'GOPATH',
               'CARGO_HOME', 'RUSTUP_HOME', 'DOTNET_ROOT', 'ANDROID_HOME', 'ANDROID_SDK_ROOT'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    home = Path.home()
    extra = [home/'.local/bin', home/'.cargo/bin', home/'.pyenv/shims', home/'.asdf/shims', home/'.volta/bin']
    if os.name != 'nt':
        extra += [Path('/opt/homebrew/bin'), Path('/usr/local/bin')]
        nvm = home/'.nvm/versions/node'
        if nvm.is_dir():
            # Prefer the newest installed version. Users can select another
            # version in an approved command; no shell startup scripts executed.
            import re
            extra += [p/'bin' for p in sorted(nvm.iterdir(), key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p.name)), reverse=True)]
    env['PATH'] = os.pathsep.join([env.get('PATH', os.defpath)] + [str(p) for p in extra if p.is_dir()])
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NO_COLOR='1')
    return env


def shell_command(command, env):
    if os.name == 'nt':
        shell = shutil.which('pwsh', path=env['PATH']) or shutil.which('powershell', path=env['PATH'])
        if not shell:
            raise RuntimeError('未找到 PowerShell，请安装 PowerShell 7 后重试。')
        prefix = "$ErrorActionPreference='Stop'; $OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); $global:LASTEXITCODE=0;\n"
        script = prefix + command + '\n$ccbox_ok=$?; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; if (-not $ccbox_ok) { exit 1 }; exit 0'
        return [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')]
    shell = '/bin/zsh' if platform.system() == 'Darwin' else '/bin/bash'
    return [shell, '-f', '-c', command] if shell.endswith('zsh') else [shell, '--noprofile', '--norc', '-c', command]


def inventory():
    env = host_environment()
    return {'platform': platform.system(), 'architecture': platform.machine(),
            'shell': 'PowerShell (NoProfile)' if os.name == 'nt' else 'zsh/bash (no startup scripts)',
            'tools': {name: shutil.which(name, path=env['PATH']) for name in TOOLS},
            'note': '仅检测可执行文件路径，未运行工具。版本管理器或自定义工具可在获批命令中初始化。',
            'network': '本机网络；不继承容器模型代理', 'authority': '当前系统用户；项目工作目录不是系统沙箱'}


class ProjectLease:
    """Cross-process lock, automatically released on crash; no stale PID guessing."""
    def __init__(self, directory, project):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        identity = os.path.normcase(str(Path(project).resolve()))
        file = directory / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
        self.stream = file.open('a+b')
        try:
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b'0'); self.stream.flush()
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError('该项目正被另一个本机开发连接使用。请先断开，或为另一个账号使用独立副本。') from None

    def close(self):
        if not self.stream.closed:
            self.stream.close()


class ProcessTree:
    """Windows Job Object / POSIX process group, with no detached child allowance."""
    def __init__(self, process):
        self.process, self.job, self.closed = process, None, False
        self.killed, self.kill_lock = False, threading.Lock()
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes as w
            class Basic(ctypes.Structure):
                _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64), ('Flags', w.DWORD),
                            ('MinWorking', ctypes.c_size_t), ('MaxWorking', ctypes.c_size_t), ('Active', w.DWORD),
                            ('Affinity', ctypes.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
            class Limits(ctypes.Structure):
                _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMem', ctypes.c_size_t), ('JobMem', ctypes.c_size_t),
                            ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; k.CreateJobObjectW.restype = w.HANDLE
            k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
            k.CloseHandle.argtypes = [w.HANDLE]
            self.kernel = k
            self.job = k.CreateJobObjectW(None, None)
            limits = Limits(); limits.Basic.Flags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.job or not k.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not k.AssignProcessToJobObject(self.job, w.HANDLE(process._handle)):
                if self.job: k.CloseHandle(self.job)
                self.job = None
                process.kill()
                raise RuntimeError('本机进程树保护初始化失败，命令未执行。')

    def kill(self):
        with self.kill_lock:
            if self.closed or self.killed: return
            if self.job:
                self.kernel.TerminateJobObject(self.job, 1)
            elif os.name != 'nt':
                try: os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            elif self.process.poll() is None:
                self.process.kill()
            # Once signalled, never target a possibly reaped/reused process group
            # during close/finally. Darwin can return EPERM for an exited group.
            self.killed = True

    def close(self):
        if self.closed: return
        self.kill()
        if self.job:
            self.kernel.CloseHandle(self.job); self.job = None
        self.closed = True


class HostJobs:
    def __init__(self, project, environment, approve, log=lambda _: None):
        self.project = Path(project).resolve(strict=True)
        self.environment, self.approve, self.log = environment, approve, log
        self.jobs, self.closed, self.lock = {}, False, threading.RLock()

    def snapshot(self, job):
        with self.lock:
            return {k: job[k] for k in ('job_id', 'status', 'exit_code', 'output', 'truncated')}

    def call(self, name, args):
        if not isinstance(args, dict): raise ValueError('参数应为对象。')
        if self.closed: raise RuntimeError('本机连接已断开，请从 CCBOX 重新打开。')
        if name == 'host_info':
            if args: raise ValueError('host_info 不接受参数。')
            return {**inventory(), 'project': str(self.project), 'environment': self.environment}
        if name == 'host_run':
            if set(args) - {'command', 'timeout'}: raise ValueError('仅接受 command 和 timeout。')
            command, timeout = args.get('command'), args.get('timeout', 600)
            if not isinstance(command, str) or not command.strip() or len(command) > 16000 or '\0' in command:
                raise ValueError('命令应为 1–16000 字符。')
            if type(timeout) is not int or not 1 <= timeout <= 3600: raise ValueError('超时应为 1–3600 秒。')
            with self.lock:
                if any(j['status'] not in TERMINAL for j in self.jobs.values()):
                    raise RuntimeError('当前项目已有待确认或运行任务；请先查看或停止它。')
                if len(self.jobs) >= 40: self.jobs.pop(next(iter(self.jobs)))
                job = dict(job_id=secrets.token_hex(12), status='pending_approval', command=command, timeout=timeout,
                           output='', exit_code=None, truncated=False, cancel=threading.Event(), tree=None, done=threading.Event())
                self.jobs[job['job_id']] = job
                threading.Thread(target=self.execute, args=(job,), daemon=True).start()
            return self.snapshot(job)
        if name in ('host_output', 'host_cancel'):
            if set(args) != {'job_id'} or not isinstance(args['job_id'], str): raise ValueError('请提供 job_id。')
            with self.lock:
                job = self.jobs.get(args['job_id'])
                if not job: raise ValueError('该任务不属于当前连接，或记录已过期。')
                if name == 'host_cancel' and job['status'] not in TERMINAL:
                    job['cancel'].set()
                    if job['tree']: job['tree'].kill()
            return self.snapshot(job)
        raise ValueError('未知本机工具。')

    def append(self, job, value):
        with self.lock:
            combined = job['output'] + value
            job['truncated'] |= len(combined) > 24000
            job['output'] = combined[-24000:]

    def execute(self, job):
        process, tree = None, None
        try:
            permitted = self.approve({'environment': self.environment, 'project': str(self.project),
                                     'command': job['command'], 'job_id': job['job_id']}, job['cancel'])
            if self.closed or job['cancel'].is_set():
                job['status'] = 'cancelled'; return
            if not permitted:
                job['status'] = 'denied'; return
            if self.project.resolve(strict=True) != self.project or not self.project.is_dir():
                raise RuntimeError('项目路径已变化，请重新选择项目。')
            env = host_environment()
            args = shell_command(job['command'], env)
            # Spawn a blocked shell, then assign its job/group BEFORE feeding any
            # user command. This prevents child creation before Windows Job setup.
            if os.name == 'nt':
                gate = args[:4] + ['-Command', '-']
                script = base64.b64decode(args[-1]).decode('utf-16-le')
            else:
                gate = [args[0], '-f'] if args[0].endswith('zsh') else [args[0], '--noprofile', '--norc']
                script = job['command']
            process = subprocess.Popen(gate, cwd=self.project, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                       start_new_session=os.name != 'nt')
            tree = ProcessTree(process)
            with self.lock:
                job['tree'] = tree
                if self.closed or job['cancel'].is_set(): tree.kill()
                else: job['status'] = 'running'
            def read():
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                try:
                    while True:
                        block = os.read(process.stdout.fileno(), 4096)
                        if not block: break
                        self.append(job, decoder.decode(block))
                    self.append(job, decoder.decode(b'', final=True))
                except OSError: pass
            reader = threading.Thread(target=read, daemon=True); reader.start()
            # PS -Command - needs ASCII-safe source so Windows PowerShell 5's
            # stdin codepage does not corrupt Chinese commands.
            if os.name == 'nt':
                encoded = base64.b64encode(script.encode('utf-16-le')).decode()
                script = "& ([ScriptBlock]::Create([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('"+encoded+"'))))\n"
            try:
                process.stdin.write((script+'\n').encode('utf-8')); process.stdin.close()
            except (BrokenPipeError, OSError): pass
            deadline = time.monotonic() + job['timeout']
            while process.poll() is None:
                if job['cancel'].wait(.1) or self.closed:
                    tree.kill(); job['status'] = 'cancelled'; break
                if time.monotonic() >= deadline:
                    tree.kill(); job['status'] = 'timed_out'; break
            process.wait(timeout=5)
            tree.close(); reader.join(timeout=3)
            job['exit_code'] = process.returncode
            if job['cancel'].is_set() or self.closed: job['status'] = 'cancelled'
            elif job['status'] not in TERMINAL: job['status'] = 'completed' if process.returncode == 0 else 'failed'
        except Exception as e:
            self.append(job, '\n'+str(e)); job['status'] = 'cancelled' if job['cancel'].is_set() else 'failed'
        finally:
            if tree: tree.close()
            if process:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill()
                for stream in (process.stdin, process.stdout):
                    if stream: stream.close()
            with self.lock: job['tree'] = None
            self.log('本机任务 '+job['job_id'][:8]+'：'+job['status']+'（命令与输出不写入持久日志）')
            job['done'].set()

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                job['cancel'].set()
                if job['tree']: job['tree'].kill()
            jobs = list(self.jobs.values())
        # Retain the project lease until managed tasks have observed cancellation.
        for job in jobs:
            job['done'].wait(6)
