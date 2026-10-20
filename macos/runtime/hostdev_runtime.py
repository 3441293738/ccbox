"""Container-only MCP adapter and Docker stdio relay. No network listeners.

This is intentionally independent of Claude account credentials and the proxy.
The host owns authorization, CWD, job lifecycle and command approval.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading

MAX_FRAME = 262144
NAME = 'ccbox-host'


def frame(stream):
    line = stream.readline(MAX_FRAME + 1)
    if not line: raise EOFError()
    if len(line) > MAX_FRAME or not line.endswith(b'\n'): raise ValueError('Frame too large')
    result = json.loads(line)
    if not isinstance(result, dict): raise ValueError('Object required')
    return result


def packed(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'


def relay():
    hello = frame(sys.stdin.buffer)
    path, token = hello['socket'], hello['token']
    if not path.startswith('/tmp/ccbox-hostdev-') or len(token) < 32: raise ValueError('Invalid endpoint')
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path); os.chmod(path, 0o600); listener.listen(8)
    output_lock, pending_lock = threading.Lock(), threading.Lock()
    pending, slots = {}, threading.BoundedSemaphore(8)
    def emit(value):
        with output_lock:
            sys.stdout.buffer.write(packed(value)); sys.stdout.buffer.flush()
    def handle(conn):
        key = None
        try:
            with conn:
                conn.settimeout(35)
                request = frame(conn.makefile('rb'))
                supplied = request.get('token')
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
                    conn.sendall(packed({'error': 'Connection revoked or invalid'})); return
                key, event, box = secrets.token_hex(16), threading.Event(), []
                with pending_lock: pending[key] = (event, box)
                emit({'id': key, 'name': request.get('name'), 'arguments': request.get('arguments', {})})
                if event.wait(25): conn.sendall(packed(box[0]))
                else: conn.sendall(packed({'error': 'Host response timeout; do not retry side effects automatically'}))
        except (OSError, ValueError, EOFError): pass
        finally:
            if key:
                with pending_lock: pending.pop(key, None)
            slots.release()
    def accept():
        while True:
            try: conn, _ = listener.accept()
            except OSError: return
            if slots.acquire(blocking=False): threading.Thread(target=handle, args=(conn,), daemon=True).start()
            else: conn.close()
    threading.Thread(target=accept, daemon=True).start()
    emit({'ready': True})
    try:
        while True:
            response = frame(sys.stdin.buffer)
            with pending_lock:
                waiting = pending.get(response.get('id'))
                if waiting:
                    waiting[1].append(response); waiting[0].set()
    except EOFError: pass
    finally:
        listener.close(); Path(path).unlink(missing_ok=True)


def request(descriptor, name, arguments):
    # Resolve per call so an already-running MCP process can reconnect without
    # starting an additional model request or retaining an old capability.
    endpoint = json.loads(Path(descriptor).read_text(encoding='utf-8'))
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(30); conn.connect(endpoint['socket'])
        conn.sendall(packed({'token': endpoint['token'], 'name': name, 'arguments': arguments}))
        return frame(conn.makefile('rb'))


def tool(name, description, properties=None, required=()):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {},
                            'required': list(required), 'additionalProperties': False}}


TOOLS = [
    tool('host_info', 'Inspect the explicitly connected HOST project/tool paths. Native Bash remains in the container. No commands are executed.'),
    tool('host_run', 'Run build/test/dev commands on the HOST using its installed toolchain, in the connected project. '
         'Returns a job_id immediately; user approves in CCBOX before execution. Windows: PowerShell; macOS: zsh. '
         'This has host user privileges and HOST networking, NOT container proxy networking. '
         'Use relative paths, never Linux /workspace paths on the host. Never request secrets. '
         'Do not resubmit a job after an ambiguous error; query host_output. Background descendants end with the job.',
         {'command': {'type': 'string', 'minLength': 1, 'maxLength': 16000},
          'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 600}}, ('command',)),
    tool('host_output', 'Poll the job_id from host_run, at least 2 seconds apart. Status may be pending_approval, running, completed, failed, denied, cancelled or timed_out. Output is a bounded tail.',
         {'job_id': {'type': 'string'}}, ('job_id',)),
    tool('host_cancel', 'Cancel a HOST job, including pending approval and managed child processes. This does not undo file or network changes.',
         {'job_id': {'type': 'string'}}, ('job_id',)),
]


def mcp(descriptor):
    initialized = False
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME+1)
        if not line: break
        if len(line)>MAX_FRAME: break
        msg = {}
        try:
            if len(line) > MAX_FRAME: raise ValueError('Message too large')
            msg = json.loads(line)
            if not isinstance(msg, dict): raise ValueError('Object required')
            if 'id' not in msg: continue
            method, params = msg.get('method'), msg.get('params') or {}
            if method == 'initialize':
                requested = params.get('protocolVersion')
                version = requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2025-06-18'
                result = {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
                          'serverInfo': {'name': 'ccbox-host', 'version': '1.0.0'},
                          'instructions': 'Optional host development mode. Files remain mounted in the container. '
                          'For host builds/tests use host_info then host_run, not native Bash. '
                          'The user must approve in the CCBOX window. Do not claim host commands use the model proxy.'}
                initialized = True
            elif method == 'ping': result = {}
            elif not initialized: raise ValueError('Initialize first')
            elif method == 'tools/list': result = {'tools': TOOLS}
            elif method == 'tools/call':
                if params.get('name') not in {t['name'] for t in TOOLS}: raise ValueError('Unknown tool')
                try:
                    response = request(descriptor, params['name'], params.get('arguments', {}))
                    value = response.get('result', response.get('error'))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': 'error' in response}
                except (OSError, ValueError, EOFError):
                    result = {'content': [{'type': 'text', 'text': '本机连接未就绪。请保持 CCBOX 打开，在对应账号和项目点击开始开发；不自动重试已提交的命令。'}], 'isError': True}
            else:
                sys.stdout.buffer.write(packed({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32601, 'message': 'Method not found'}})); sys.stdout.buffer.flush(); continue
            response = {'jsonrpc': '2.0', 'id': msg['id'], 'result': result}
        except (ValueError, KeyError, TypeError) as e:
            response = {'jsonrpc': '2.0', 'id': msg.get('id') if isinstance(msg, dict) else None,
                        'error': {'code': -32602, 'message': str(e)}}
        sys.stdout.buffer.write(packed(response)); sys.stdout.buffer.flush()


def install():
    """Use official local-scope CLI, never overwrite account JSON or project files."""
    data = frame(sys.stdin.buffer)
    target = Path(data['descriptor'])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = target.with_suffix('.tmp'); temp.write_text(json.dumps(data['endpoint']), encoding='utf-8')
    temp.chmod(0o600); os.replace(temp, target)
    config = {'type': 'stdio', 'command': 'python3', 'args': [str(Path(__file__).resolve()), 'mcp', '--descriptor', str(target)]}
    # Read only to detect idempotence/conflicts; writes are delegated to the CLI.
    authfile = Path.home()/'.claude.json'
    existing = json.loads(authfile.read_text()) if authfile.exists() else {}
    entry = existing.get('projects', {}).get(os.getcwd(), {}).get('mcpServers', {}).get(NAME)
    if entry != config:
        if entry:
            raise RuntimeError('当前项目已有同名 ccbox-host 配置。请先确认其来源；未覆盖。')
        run = subprocess.run(['claude', 'mcp', 'add-json', '--scope', 'local', NAME, json.dumps(config)], capture_output=True, timeout=30)
        if run.returncode: raise RuntimeError('Claude MCP 注册失败，请检查容器 CLI 版本和项目配置。')
    print(json.dumps({'installed': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('relay', 'mcp', 'install'))
    parser.add_argument('--descriptor')
    args = parser.parse_args()
    if args.action == 'relay': relay()
    elif args.action == 'install': install()
    else: mcp(args.descriptor)
