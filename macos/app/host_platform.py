"""Host integration only. Secrets and shell interpolation stay out of subprocess argv."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def resource_root():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) if sys.platform == 'darwin' else Path(sys.executable).parent
    return Path(__file__).resolve().parents[1]


def control_directory(root):
    # Explicit fixture roots never touch a real user's Application Support.
    if sys.platform == 'darwin' and Path(root).resolve() == resource_root().resolve():
        return Path.home() / 'Library/Application Support/CCBOX'
    return Path(root)


def find_docker():
    found = shutil.which('docker')
    if found:
        return found
    candidates = ([Path('/Applications/Docker.app/Contents/Resources/bin/docker'),
                   Path.home()/'.docker/bin/docker', Path('/usr/local/bin/docker'), Path('/opt/homebrew/bin/docker')]
                  if sys.platform == 'darwin' else
                  [Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Docker/Docker/resources/bin/docker.exe',
                   Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Programs/DockerDesktop/resources/bin/docker.exe'])
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError('请先安装并启动 Docker Desktop，再重试。M 系列 Mac 请选择 Apple silicon 版本。')


def docker_environment():
    env = dict(os.environ)
    if sys.platform == 'darwin':
        # Finder-launched apps do not inherit a login shell's PATH. Docker needs
        # docker-credential-desktop alongside the CLI to pull images.
        bins = [str(Path(find_docker()).resolve().parent), '/usr/local/bin', '/opt/homebrew/bin']
        env['PATH'] = os.pathsep.join(bins + [env.get('PATH', '/usr/bin:/bin')])
    return env


def open_local(path):
    path = Path(path).resolve()
    if sys.platform == 'darwin':
        subprocess.run(['/usr/bin/open', str(path)], check=True, timeout=15)
    elif os.name == 'nt':
        os.startfile(str(path))
    else:
        subprocess.run(['xdg-open', str(path)], check=True, timeout=15)


def terminal_text(args):
    if any('\0' in str(arg) or '\n' in str(arg) or '\r' in str(arg) for arg in args):
        raise ValueError('终端参数包含无效控制字符。')
    return ' '.join(shlex.quote(str(arg)) for arg in args)


def open_terminal(args):
    if sys.platform != 'darwin':
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
        return
    # Constant AppleScript, shell arguments safely quoted as a single data value.
    # No credentials/OAuth URL are included. macOS consent remains user-controlled.
    script = ('on run argv\n'
              'tell application "Terminal"\nactivate\ndo script (item 1 of argv)\nend tell\nend run')
    command = terminal_text(args)
    path = docker_environment()['PATH']
    command = 'export PATH=' + shlex.quote(path) + '; ' + command
    result = subprocess.run(['/usr/bin/osascript', '-e', script, command], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('终端未打开。请按 macOS 提示自行确认终端自动化权限，或在沙箱 VS Code 的终端运行 claude。')


def acquire_instance_lock(root):
    if sys.platform != 'darwin':
        return None
    import fcntl
    folder = control_directory(root)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    stream = (folder/'.ccbox.lock').open('a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('CCBOX 已经打开，请使用现有窗口。') from None
    return stream
