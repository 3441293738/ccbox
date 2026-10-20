"""Loopback TCP <-> named-volume Unix sockets. No external workload interface."""
import asyncio
import os
from pathlib import Path
from transport import relay

from clipboard_transfer import PROTOCOL, TYPES, validate as validate_clipboard

clipboard_lock = asyncio.Lock()
clipboard_slots = asyncio.Semaphore(4)


async def proxy(r, w):
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_unix_connection('/ipc/proxy.sock'), 5)
        await relay(r, w, ur, uw)
    except Exception:
        w.close()  # deliberately no direct fallback


async def desktop(r, w):
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 6081), 5)
        await relay(r, w, ur, uw)
    except Exception:
        w.close()


async def clipboard_control(r, w):
    """Write explicit user data only. Serialize X11 ownership changes."""
    process = None
    try:
        async with clipboard_slots:
            if await asyncio.wait_for(r.readline(), 5) != PROTOCOL:
                raise ValueError('invalid clipboard protocol')
            kind = (await asyncio.wait_for(r.readline(), 5)).decode('ascii').strip()
            line = await asyncio.wait_for(r.readline(), 5)
            if len(line) > 12 or kind not in TYPES:
                raise ValueError('invalid clipboard type or size')
            size = int(line)
            if not 0 < size <= TYPES[kind][1]:
                raise ValueError('clipboard data too large')
            payload = await asyncio.wait_for(r.readexactly(size), 20)
            target = validate_clipboard(kind, payload)
            async with clipboard_lock:
                process = await asyncio.create_subprocess_exec(
                    '/usr/bin/xclip', '-selection', 'clipboard', '-t', target, '-i',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={**os.environ, 'DISPLAY': ':99'})
                await asyncio.wait_for(process.communicate(payload), 10)
                if process.returncode:
                    raise RuntimeError('xclip failed')
            w.write(b'OK\n')
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, UnicodeError, OSError, RuntimeError):
        w.write(b'ERR\n')
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        try:
            await w.drain()
        finally:
            w.close()


async def main():
    p = Path('/desktop/web.sock')
    control_path = Path('/desktop/control.sock')
    p.unlink(missing_ok=True)
    control_path.unlink(missing_ok=True)
    a = await asyncio.start_server(proxy, '127.0.0.1', 18080)
    b = await asyncio.start_unix_server(desktop, str(p))
    c = await asyncio.start_unix_server(clipboard_control, str(control_path), limit=1024)
    os.chmod(p, 0o666)
    os.chmod(control_path, 0o666)
    async with a, b, c:
        await asyncio.gather(a.serve_forever(), b.serve_forever(), c.serve_forever())


if __name__ == '__main__':
    asyncio.run(main())
