"""Restore only this container's managed editor through its local X11 display.

The viewer sends a fixed Openbox shortcut over the existing authenticated RFB
connection. No HTTP command endpoint, network listener or host process access.
"""
import ctypes as C
import os
from pathlib import Path
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET

KEY = 'C-F8'
NS = 'http://openbox.org/3.4/rc'
BASE = Path.home() / '.ccbox/sandbox-editor'


def install_binding(home=None, template=Path('/etc/xdg/openbox/rc.xml')):
    home = Path(home) if home else Path.home()
    target = home / '.config/openbox/rc.xml'
    previous = target.read_bytes() if target.exists() else None
    root = ET.fromstring(previous if previous is not None else template.read_bytes())
    if root.tag != '{' + NS + '}openbox_config':
        raise RuntimeError('Openbox 配置格式不匹配，原配置保持不变。')
    ET.register_namespace('', NS)
    tag = lambda name: '{' + NS + '}' + name
    keyboard = root.find(tag('keyboard'))
    if keyboard is None:
        keyboard = ET.SubElement(root, tag('keyboard'))
    for old in list(keyboard.findall(tag('keybind'))):
        if old.get('key') in ('C-A-S-F8', 'C-A-F8') and old.get('data-ccbox') == 'restore-editor':
            keyboard.remove(old)
    bindings = keyboard.findall(tag('keybind'))
    binding = next((b for b in bindings if b.get('key') == KEY), None)
    if binding is not None and binding.get('data-ccbox') != 'restore-editor':
        raise RuntimeError('恢复窗口快捷键已被其他设置占用，原设置保持不变。')
    if binding is None:
        binding = ET.SubElement(keyboard, tag('keybind'), {'key': KEY, 'data-ccbox': 'restore-editor'})
    binding[:] = []
    action = ET.SubElement(binding, tag('action'), {'name': 'Execute'})
    ET.SubElement(action, tag('command')).text = '/usr/bin/python3 ' + shlex.quote(str(Path(__file__).resolve()))
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name('rc.xml.before-ccbox-restore')
    if previous is not None and not backup.exists():
        backup.write_bytes(previous)
    temp = target.with_name('rc.xml.ccbox-tmp')
    temp.write_bytes(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    temp.replace(target)
    try:
        result = subprocess.run(['openbox', '--reconfigure'], env={**os.environ, 'DISPLAY': ':99'},
                                capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('桌面快捷键尚未应用，请重新打开沙箱 VS Code。')
    except Exception:
        if previous is not None:
            target.write_bytes(previous)
        else:
            target.unlink()
        raise


def managed_pid(pid, proc=Path('/proc'), base=None):
    """Do not use window titles (project-controlled), or expose process args."""
    base = BASE if base is None else Path(base)
    try:
        folder = proc / str(int(pid))
        if folder.stat().st_uid != os.getuid():
            return False
        if os.readlink(folder / 'exe') != str(base / 'install/code'):
            return False
        args = (folder / 'cmdline').read_bytes().split(b'\0')
        args = [a.decode(errors='replace') for a in args if a]
        # Electron may rewrite /proc/cmdline into a single process-title string.
        # Keep exact token/profile comparisons after parsing that representation.
        rewritten = len(args) == 1
        if rewritten:
            args = shlex.split(args[0])
        if any(a.startswith('--type=') for a in args):
            return False
        for flag, value in [('--user-data-dir', str(base / 'user-data')),
                            ('--extensions-dir', str(base / 'extensions'))]:
            # Electron also groups positional values after all switches in its
            # process title. Require whole tokens, never substring matching.
            if rewritten and flag in args and value in args:
                continue
            if not any(a == flag and i + 1 < len(args) and args[i + 1] == value
                       or a == flag + '=' + value for i, a in enumerate(args)):
                return False
        return True
    except (OSError, ValueError):
        return False


class MessageData(C.Union):
    _fields_ = [('b', C.c_char * 20), ('s', C.c_short * 10), ('l', C.c_long * 5)]


class ClientMessage(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong), ('message_type', C.c_ulong),
                ('format', C.c_int), ('data', MessageData)]


class Event(C.Union):
    _fields_ = [('client', ClientMessage), ('padding', C.c_long * 24)]


class X11:
    def __init__(self, display=':99'):
        self.lib = x = C.CDLL('libX11.so.6')
        signatures = {
            'XOpenDisplay': ([C.c_char_p], C.c_void_p),
            'XDefaultRootWindow': ([C.c_void_p], C.c_ulong),
            'XInternAtom': ([C.c_void_p, C.c_char_p, C.c_int], C.c_ulong),
            'XGetWindowProperty': ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_long, C.c_long,
                C.c_int, C.c_ulong, C.POINTER(C.c_ulong), C.POINTER(C.c_int),
                C.POINTER(C.c_ulong), C.POINTER(C.c_ulong), C.POINTER(C.c_void_p)], C.c_int),
            'XSendEvent': ([C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(Event)], C.c_int),
            'XMapRaised': ([C.c_void_p, C.c_ulong], C.c_int),
            'XSync': ([C.c_void_p, C.c_int], C.c_int),
            'XFree': ([C.c_void_p], C.c_int),
            'XCloseDisplay': ([C.c_void_p], C.c_int),
            'XSetErrorHandler': ([C.c_void_p], C.c_void_p),
        }
        for name, (args, returns) in signatures.items():
            fn = getattr(x, name)
            fn.argtypes, fn.restype = args, returns
        self.display = x.XOpenDisplay(display.encode())
        if not self.display:
            raise RuntimeError('沙箱桌面连接尚未就绪。')
        # A window can close between listing and querying it. Ignore X errors,
        # then report the actual final active window rather than assuming success.
        self.handler = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p)(lambda *_: 0)
        self.old_handler = x.XSetErrorHandler(C.cast(self.handler, C.c_void_p))
        self.root = x.XDefaultRootWindow(self.display)

    def atom(self, name):
        return self.lib.XInternAtom(self.display, name.encode(), 0)

    def values(self, window, name):
        actual, fmt, count, rest, data = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        result = self.lib.XGetWindowProperty(self.display, window, self.atom(name), 0, 4096, 0, 0,
                    C.byref(actual), C.byref(fmt), C.byref(count), C.byref(rest), C.byref(data))
        try:
            if result or fmt.value != 32 or not data.value or rest.value:
                return []
            return list(C.cast(data, C.POINTER(C.c_ulong))[:count.value])
        finally:
            if data.value:
                self.lib.XFree(data)

    def send(self, window, name, *values):
        event = Event()
        event.client = ClientMessage(33, 0, 1, self.display, window, self.atom(name), 32)
        for i, value in enumerate(values):
            event.client.data.l[i] = value
        self.lib.XSendEvent(self.display, self.root, 0, (1 << 19) | (1 << 20), C.byref(event))

    def close(self):
        self.lib.XSync(self.display, 0)
        self.lib.XSetErrorHandler(self.old_handler)
        self.lib.XCloseDisplay(self.display)


def restore(display=':99', matches=managed_pid):
    x = X11(display)
    try:
        windows = x.values(x.root, '_NET_CLIENT_LIST_STACKING') or x.values(x.root, '_NET_CLIENT_LIST')
        for window in reversed(windows):
            pids = x.values(window, '_NET_WM_PID')
            if not pids or not matches(pids[0]):
                continue
            desktop = x.values(window, '_NET_WM_DESKTOP')
            if desktop and desktop[0] != 0xffffffff:
                x.send(x.root, '_NET_CURRENT_DESKTOP', desktop[0], 0)
            x.send(window, '_NET_WM_STATE', 0, x.atom('_NET_WM_STATE_SHADED'), 0, 2)
            x.lib.XMapRaised(x.display, window)
            x.send(window, '_NET_ACTIVE_WINDOW', 2, 0, 0)
            x.lib.XSync(x.display, 0)
            return True  # Request sent; the window manager applies it asynchronously.
        return False
    finally:
        x.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--install']:
        install_binding()
    elif sys.argv[1:]:
        raise SystemExit('Unexpected arguments')
    else:
        print('Restore requested' if restore() else 'No managed editor window')
