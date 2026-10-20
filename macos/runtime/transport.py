"""Upstream-only HTTP CONNECT / SOCKS5 transport. No destination DNS or fallback."""
import asyncio
import base64
import ipaddress
import json
import ssl
import struct
import sys
from urllib.parse import urlsplit, unquote

TIMEOUT = 20


def bundled_ca_file():
    """A frozen app must carry its own CA roots, not a build-machine path."""
    try:
        import certifi
    except ImportError:
        # The Linux gateway image has system ca-certificates and no pip deps.
        if getattr(sys, 'frozen', False) or sys.platform == 'darwin':
            raise RuntimeError('应用缺少 CA 证书包，请重新下载完整的 CCBOX.app。') from None
        return None
    return certifi.where()


def client_tls_context():
    """Keep system trust and add packaged public roots; never disable TLS checks."""
    context = ssl.create_default_context()
    bundle = bundled_ca_file()
    if bundle:
        try:
            context.load_verify_locations(cafile=bundle)
        except (OSError, ssl.SSLError):
            raise RuntimeError('应用内 CA 证书包缺失或损坏，请重新下载完整的 CCBOX.app。') from None
    return context


def tls_diagnostics():
    """Offline self-test: prove the bundle works without ANY system CA store."""
    bundle = bundled_ca_file()
    isolated = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if bundle:
        isolated.load_verify_locations(cafile=bundle)
    context = client_tls_context()
    return {'bundled_ca': bool(bundle), 'bundled_ca_count': isolated.cert_store_stats()['x509_ca'],
            'certificate_required': context.verify_mode == ssl.CERT_REQUIRED,
            'hostname_check': context.check_hostname}


class ProxyTLSVerificationError(ConnectionError):
    def __init__(self, stage, error):
        self.stage = stage
        self.verify_code = getattr(error, 'verify_code', None)
        location = 'HTTPS 代理服务器' if stage == 'proxy' else '代理隧道内的目标网站'
        reason = {9: '证书尚未生效', 10: '证书已过期', 18: '自签名证书',
                  19: '自签名证书链', 20: '证书链缺少可信签发者',
                  21: '证书链不完整', 62: '证书域名不匹配', 64: '证书 IP 不匹配'}.get(
                      self.verify_code, '证书校验未通过')
        super().__init__(f'{location} TLS 校验失败：{reason}。'
                         '请检查系统日期、代理协议及服务商证书链。'
                         '连接已终止，未切换直连；证书校验保持开启。')


def parse_proxy(value):
    if "://" not in value:
        raise ValueError("请选择 http / https / socks5h 协议")
    p = urlsplit(value.strip())
    if p.scheme not in ("http", "https", "socks5", "socks5h") or not p.hostname:
        raise ValueError("代理 URL 格式错误")
    port = p.port if p.port is not None else {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}[p.scheme]
    if not 1 <= port <= 65535:
        raise ValueError("代理端口应为 1–65535")
    if p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("代理 URL 不应包含路径、查询参数或片段")
    user, password = unquote(p.username or ""), unquote(p.password or "")
    if any(ord(c) < 32 or ord(c) == 127 for c in user + password + p.hostname):
        raise ValueError("代理字段包含控制字符")
    if len(user.encode()) > 255 or len(password.encode()) > 255:
        raise ValueError("代理认证字段超过 255 字节")
    return dict(scheme=p.scheme, host=p.hostname, port=port, username=user, password=password)


def authority(host, port):
    if not host or any(ord(c) < 33 or ord(c) == 127 or c.isspace() or c in "/\\@?#" for c in host):
        raise ValueError("invalid destination")
    if not 1 <= int(port) <= 65535:
        raise ValueError("invalid port")
    return f"[{host}]:{port}" if ":" in host else f"{host.encode('idna').decode()}:{port}"


async def header(reader):
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)


async def connect_proxy(proxy, host, port):
    """Only the proxy endpoint is resolved locally; destination goes over the tunnel."""
    target = authority(host, port)
    tls = client_tls_context() if proxy["scheme"] == "https" else None
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(
            proxy["host"], proxy["port"], ssl=tls,
            server_hostname=proxy["host"] if tls else None, limit=65536), TIMEOUT)
    except ssl.SSLCertVerificationError as error:
        raise ProxyTLSVerificationError('proxy', error) from None
    try:
        if proxy["scheme"] in ("socks5", "socks5h"):
            # Both schemes use remote destination DNS intentionally.
            authenticated = bool(proxy["username"])
            w.write(b"\x05\x01" + (b"\x02" if authenticated else b"\x00"))
            await w.drain()
            chosen = await asyncio.wait_for(r.readexactly(2), TIMEOUT)
            if chosen != (b"\x05\x02" if authenticated else b"\x05\x00"):
                raise ConnectionError("SOCKS authentication method rejected")
            if authenticated:
                u, p = proxy["username"].encode(), proxy["password"].encode()
                w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                await w.drain()
                if await asyncio.wait_for(r.readexactly(2), TIMEOUT) != b"\x01\x00":
                    raise ConnectionError("SOCKS authentication rejected")
            try:
                ip = ipaddress.ip_address(host)
                addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
            except ValueError:
                name = host.encode("idna")
                if len(name) > 255:
                    raise ValueError("destination too long")
                addr = b"\x03" + bytes([len(name)]) + name
            w.write(b"\x05\x01\x00" + addr + struct.pack("!H", port))
            await w.drain()
            reply = await asyncio.wait_for(r.readexactly(4), TIMEOUT)
            if reply[:3] != b"\x05\x00\x00":
                raise ConnectionError("SOCKS upstream connection rejected")
            length = {1: 4, 4: 16}.get(reply[3])
            if reply[3] == 3:
                length = (await asyncio.wait_for(r.readexactly(1), TIMEOUT))[0]
            if length is None:
                raise ConnectionError("invalid SOCKS reply")
            await asyncio.wait_for(r.readexactly(length + 2), TIMEOUT)
        else:
            auth = ""
            if proxy["username"]:
                token = base64.b64encode((proxy["username"] + ":" + proxy["password"]).encode()).decode()
                auth = f"Proxy-Authorization: Basic {token}\r\n"
            w.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n".encode())
            await w.drain()
            response = await header(r)
            fields = response.split(b"\r\n", 1)[0].split()
            if len(fields) < 2 or fields[1] != b"200":
                raise ConnectionError("HTTP upstream tunnel rejected")
        return r, w
    except BaseException:
        w.close()
        raise


async def pump(reader, writer):
    while True:
        data = await asyncio.wait_for(reader.read(65536), 300)
        if not data:
            return
        writer.write(data)
        await writer.drain()


async def relay(r1, w1, r2, w2):
    tasks = [asyncio.create_task(pump(r1, w2)), asyncio.create_task(pump(r2, w1))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        w1.close()
        w2.close()


async def fetch_json(proxy, host, path="/"):
    r, w = await connect_proxy(proxy, host, 443)
    try:
        try:
            await w.start_tls(client_tls_context(), server_hostname=host, ssl_handshake_timeout=20)
        except ssl.SSLCertVerificationError as error:
            raise ProxyTLSVerificationError('destination', error) from None
        w.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        response = await header(r)
        if response.split()[1] != b"200":
            raise ConnectionError("IP endpoint returned a non-200 response")
        chunks = []
        total = 0
        while True:
            chunk = await asyncio.wait_for(r.read(8192), 20)
            if not chunk:
                break
            total += len(chunk)
            if total > 262144:
                raise ValueError("response too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        if b"transfer-encoding: chunked" in response.lower():
            decoded = b""
            while body:
                line, body = body.split(b"\r\n", 1)
                n = int(line.split(b";")[0], 16)
                if not n:
                    break
                decoded += body[:n]
                body = body[n + 2:]
            body = decoded
        return json.loads(body)
    finally:
        w.close()
