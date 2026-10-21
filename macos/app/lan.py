"""Opt-in desktop sharing; never share proxy ports or change sandbox networking."""
import copy
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path

from .storage import atomic_json

PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def private_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
        return any(address in network for network in PRIVATE)
    except (ValueError, TypeError):
        return False


def validate_access(config):
    enabled = config.get('lan_enabled', False)
    address = str(config.get('lan_address', '')).strip()
    if not isinstance(enabled, bool) or (enabled and not private_ipv4(address)):
        error = ValueError('开启局域网访问时，请填写本机的私有 IPv4 地址，例如 192.168.1.10；不使用 0.0.0.0 或公网地址。')
        error.key = 'lan_address'
        raise error
    return {'lan_enabled': enabled, 'lan_address': address}


def desktop_ports(config):
    access = validate_access(config)
    port = int(config['desktop_port'])
    result = [f'127.0.0.1:{port}:6080']
    if access['lan_enabled']:
        result.append(f'{access["lan_address"]}:{port}:6080')
    return result


def local_addresses():
    """Read interface addresses, without contacting any internet/IP lookup service."""
    values = []
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    commands = []
    if sys.platform == 'win32':
        commands.append(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                         'Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway} | ForEach-Object {$_.IPv4Address.IPAddress}'])
    elif sys.platform == 'darwin':
        commands.extend([['/usr/sbin/ipconfig', 'getifaddr', name] for name in ('en0', 'en1')])
        commands.append(['/sbin/ifconfig'])
    for command in commands:
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=8, creationflags=flags).stdout
            if command[0] == '/sbin/ifconfig':
                # Only interface addresses, never broadcast/peer/netmask values.
                values.extend(re.findall(r'\binet\s+((?:\d{1,3}\.){3}\d{1,3})\b', output))
            else:
                values.extend(re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', output))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not values:
        try:
            values.extend(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
        except OSError:
            pass
    return list(dict.fromkeys(v for v in values if private_ipv4(v)))


def access_mounts(root):
    return [{'type': 'bind', 'source': str(Path(root)/'runtime'/name), 'target': '/opt/ccbox/'+name,
             'read_only': True, 'bind': {'create_host_path': False}}
            for name in ('gateway.py', 'desktop-login.html')]


def apply_access(controller, *, rotate=False):
    """After explicit confirmation, recreate only the gateway, with rollback.

    Keep the active proxy/port/project/home identity; pending project settings are
    not applied by this operation. Bind-mounted secret inode stays stable.
    """
    controller.check_location()
    config = controller.config()
    access = validate_access(config)
    if access['lan_enabled'] and access['lan_address'] not in local_addresses():
        raise ValueError('选定的局域网 IP 不在本机网卡上。请点击「检测本机地址」重新选择。')
    if not controller.file.exists() or not controller.run(controller.command('ps', '-q', 'gateway')):
        if rotate:
            raise RuntimeError('请先启动当前空间，再重置访问口令。')
        return '访问设置已保存，下次启动当前空间时生效。'
    secret = controller.data/'active/upstream.json'
    before_secret = secret.read_bytes()
    active = json.loads(before_secret)
    if int(config['desktop_port']) != int(active['desktop_port']):
        raise ValueError('桌面端口也已修改，请先在「检查与修复」重启并应用设置，再设置局域网访问。')
    previous = json.loads(controller.file.read_text(encoding='utf-8'))
    current = copy.deepcopy(previous)
    gateway = current['services']['gateway']
    gateway['ports'] = desktop_ports(config)
    targets = {m['target'] for m in access_mounts(controller.root)}
    gateway['volumes'] = [v for v in gateway['volumes'] if not (isinstance(v, dict) and v.get('target') in targets)] + access_mounts(controller.root)
    old_cred = controller.credentials()
    cred = {**old_cred, 'desktop_token': secrets.token_urlsafe(32)} if rotate else old_cred
    active.update(access)
    active['desktop_token'] = cred['desktop_token']
    from . import vault
    args = ('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', '--wait', '--wait-timeout', '60', 'gateway')
    try:
        if rotate:
            vault.save(controller.vault_file, cred)
        secret.write_text(json.dumps(active), encoding='utf-8')
        atomic_json(controller.file, current)
        controller.run(controller.command(*args), timeout=120)
    except Exception as first:
        secret.write_bytes(before_secret)
        atomic_json(controller.file, previous)
        if rotate:
            vault.save(controller.vault_file, old_cred)
        try:
            controller.run(controller.command(*args), timeout=120)
        except Exception:
            raise RuntimeError('访问设置更新失败，旧配置已还原；请检查网关状态。沙箱与登录卷未停止或删除。') from first
        raise RuntimeError('访问设置更新失败，已恢复原网关；设置仍待应用。沙箱与登录卷保持原样。') from first
    return ('局域网访问已开启。点击「查看连接信息」取得地址与访问口令。' if access['lan_enabled'] else '局域网访问已关闭，仅本机可访问。') + '\n仅桌面网关重连，编辑器、项目和登录资料保留。'


def sharing_details(controller):
    controller.check_location()
    active_file = controller.data/'active/upstream.json'
    if not active_file.exists() or not active_file.stat().st_size:
        raise ValueError('请先启动空间并应用局域网访问设置。')
    active = json.loads(active_file.read_text(encoding='utf-8'))
    access = validate_access(active)
    if not access['lan_enabled'] or not controller.run(controller.command('ps', '-q', 'gateway')):
        raise ValueError('当前环境尚未开启局域网访问。')
    return f'http://{access["lan_address"]}:{int(active["desktop_port"])}/desktop.html', active['desktop_token']
