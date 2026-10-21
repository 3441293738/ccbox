"""Small fail-stop supervisor; all children stay inside network:none."""
import os
import signal
import subprocess
import time
from pathlib import Path

children = []
stopping = False


def stop(*_):
    global stopping
    stopping = True


def launch(args, wait=0):
    children.append(subprocess.Popen(args))
    if wait:
        time.sleep(wait)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    home = Path.home()
    for name in ('.claude', '.cache', '.config', 'browser'):
        (home / name).mkdir(exist_ok=True)
    # Locale is explicit, never read from the Windows host.
    languages = os.getenv('CCBOX_LANGUAGES', 'en-US,en')
    import json
    (home / 'browser/user.js').write_text(
        'user_pref("intl.accept_languages", ' + json.dumps(languages) + ');\n')
    launch(['python3', '/opt/ccbox/bridge.py'])
    # One X/VNC server supports native SetDesktopSize: no stretched fixed canvas.
    # VNC stays on container loopback, behind the authenticated desktop gateway.
    # Clipboard transfer remains an explicit, bounded action via control.sock.
    launch(['Xtigervnc', ':99', '-geometry', '1440x900', '-depth', '24',
            '-localhost', '-rfbport', '5900', '-SecurityTypes', 'None',
            '-AlwaysShared', '-AcceptSetDesktopSize=1',
            '-AcceptCutText=0', '-SendCutText=0', '-nolisten', 'tcp'], 1)
    launch(['openbox'], .5)
    launch(['websockify', '--web=/usr/share/novnc', '127.0.0.1:6081', '127.0.0.1:5900'])
    launch(['firefox-esr', '--profile', str(home / 'browser'), 'file:///opt/ccbox/browser-check.html'])
    print('sandbox ready; network=none; browser at remote desktop', flush=True)
    try:
        while not stopping:
            # Closing Firefox is not fatal; the browser button can reopen it.
            if any(p.poll() is not None for p in children[:-1]):
                raise RuntimeError('Required sandbox service exited; failing closed')
            time.sleep(.5)
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
        for p in children:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == '__main__':
    main()
