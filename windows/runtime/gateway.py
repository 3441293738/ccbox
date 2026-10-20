"""Runs OUTSIDE the workload network namespace. Accepts proxy traffic only via UDS."""
import asyncio
import hmac
import json
import os
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
from transport import connect_proxy, header, relay

from clipboard_transfer import PROTOCOL, TYPES, validate as validate_clipboard

MAX_CLIPBOARD_IMAGE = TYPES["image"][1]


async def open_unix_connection(path):
    """Open a Unix socket on Linux without assuming the platform asyncio export."""
    return await asyncio.open_unix_connection(path)


def parse_request(raw):
    lines = raw.decode("iso-8859-1").split("\r\n")
    method, target, version = lines[0].split(" ")
    if not version.startswith("HTTP/1."):
        raise ValueError("invalid HTTP version")
    headers = []
    seen = set()
    for line in lines[1:]:
        if not line:
            break
        key, value = line.split(":", 1)
        if not key or any(c.isspace() for c in key):
            raise ValueError("invalid header")
        key_lower = key.lower()
        if key_lower in ("host", "content-length", "transfer-encoding", "origin", "cookie") and key_lower in seen:
            raise ValueError("duplicate header")
        seen.add(key_lower)
        headers.append((key, value.strip()))
    if "content-length" in seen and "transfer-encoding" in seen:
        raise ValueError("ambiguous request")
    return method, target, version, headers


class Gateway:
    def __init__(self, proxy, desktop_token="", desktop_port=6080):
        self.proxy = proxy
        self.desktop_token = desktop_token
        self.desktop_port = desktop_port
        # Cookies ignore TCP ports: distinct names avoid cross-environment overwrites.
        self.cookie_name = 'ccbox_session_' + str(desktop_port)
        self.slots = asyncio.Semaphore(128)
        self.clipboard_slots = asyncio.Semaphore(4)

    async def handle_proxy(self, r, w):
        upstream = None
        connected = False
        try:
            async with self.slots:
                method, target, version, headers = parse_request(await header(r))
                if method == "CONNECT":
                    p = urlsplit("//" + target)
                    if p.username or p.password or p.path or p.query or p.fragment or not p.port:
                        raise ValueError("invalid CONNECT target")
                    host, port = p.hostname, p.port
                else:
                    p = urlsplit(target)
                    if p.scheme != "http" or p.username or p.password or not p.hostname:
                        raise ValueError("use CONNECT for TLS")
                    host, port = p.hostname, p.port or 80
                ur, uw = await connect_proxy(self.proxy, host, port)
                upstream = uw
                if method == "CONNECT":
                    w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await w.drain()
                else:
                    path = p.path or "/"
                    if p.query:
                        path += "?" + p.query
                    filtered = [(k, v) for k, v in headers if k.lower() not in
                                ("proxy-authorization", "proxy-connection", "connection", "host")]
                    filtered += [("Host", p.netloc), ("Connection", "close")]
                    uw.write((f"{method} {path} {version}\r\n" + "".join(
                        f"{k}: {v}\r\n" for k, v in filtered) + "\r\n").encode("iso-8859-1"))
                    await uw.drain()
                connected = True
                await relay(r, w, ur, uw)
        except Exception:
            if not connected:
                try:
                    w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await w.drain()
                except Exception:
                    pass
        finally:
            w.close()
            if upstream:
                upstream.close()

    def desktop_auth(self, target, headers):
        if not self.desktop_token or not target.startswith('/') or target.startswith('//'):
            return False, False
        d = {k.lower(): v for k, v in headers}
        expected = f"127.0.0.1:{self.desktop_port}"
        if d.get("host") != expected or d.get("origin", "http://" + expected) != "http://" + expected:
            return False, False
        q = parse_qs(urlsplit(target).query)
        supplied = q.get("access", [""])[0]
        if supplied and hmac.compare_digest(supplied, self.desktop_token):
            return True, True
        cookie = SimpleCookie()
        try:
            cookie.load(d.get("cookie", ""))
            supplied = cookie[self.cookie_name].value
        except Exception:
            return False, False
        return hmac.compare_digest(supplied, self.desktop_token), False

    async def handle_desktop(self, r, w):
        upstream = None
        try:
            method, target, version, headers = parse_request(await header(r))
            ok, initial = self.desktop_auth(target, headers)
            path = urlsplit(target).path
            if not ok or (method != "GET" and not (method == "POST" and path in ('/clipboard/image', '/clipboard/text'))):
                w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await w.drain()
                return
            if method == 'POST':
                fields = {k.lower(): v for k, v in headers}
                if initial or fields.get('origin') != f'http://127.0.0.1:{self.desktop_port}':
                    await self.clipboard_response(w, b'403 Forbidden', b'origin required')
                    return
                async with self.clipboard_slots:
                    await self.handle_clipboard(r, w, headers, path.rsplit('/', 1)[-1])
                return
            if initial:
                q = parse_qs(urlsplit(target).query)
                q.pop("access", None)
                path = urlsplit(target).path + "?" + urlencode(q, doseq=True)
                w.write((f"HTTP/1.1 303 See Other\r\nLocation: {path}\r\n"
                         f"Set-Cookie: {self.cookie_name}={self.desktop_token}; HttpOnly; SameSite=Strict; Path=/\r\n"
                         "Referrer-Policy: no-referrer\r\nCache-Control: no-store\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").encode())
                await w.drain()
                return
            if method != 'GET':
                w.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await w.drain()
                return
            if urlsplit(target).path == '/desktop.html':
                body = Path('/opt/ccbox/desktop.html').read_bytes()
                w.write(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                         "X-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
                         "Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
                await w.drain()
                return
            ur, uw = await open_unix_connection("/desktop/web.sock")
            upstream = uw
            # The workload sees only the RFB transport, not the Windows viewer's
            # User-Agent, language, client hints, origin, cookies or referer.
            filtered = desktop_transport_headers(headers)
            uw.write((f"{method} {target} {version}\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in filtered) + "\r\n").encode("iso-8859-1"))
            await uw.drain()
            if desktop_websocket(headers):
                await relay(r, w, ur, uw)
            else:
                # A plain asset request is not a tunnel. Otherwise a browser can
                # reuse this connection and send a clipboard POST to websockify,
                # bypassing this router and receiving its HTTP 501 response.
                raw = await header(ur)
                lines = raw.split(b'\r\n')
                response = [lines[0]] + [line for line in lines[1:] if line and
                    line.split(b':', 1)[0].lower() not in (b'connection', b'keep-alive')]
                w.write(b'\r\n'.join(response) + b'\r\nConnection: close\r\n\r\n')
                await w.drain()
                while data := await asyncio.wait_for(ur.read(65536), 30):
                    w.write(data)
                    await w.drain()
        except Exception:
            try:
                w.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await w.drain()
            except Exception:
                pass
        finally:
            w.close()
            if upstream:
                upstream.close()

    @staticmethod
    async def clipboard_response(writer, status, body=b''):
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\nCache-Control: no-store\r\nContent-Type: text/plain; charset=utf-8"
                     + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()

    async def handle_clipboard(self, reader, writer, headers, kind):
        """Only authenticated same-origin calls reach this bounded data endpoint."""
        fields = {k.lower(): v for k, v in headers}
        content_type = fields.get('content-type', '').split(';', 1)[0].strip().lower()
        expected, limit = TYPES[kind]
        size = fields.get('content-length', '')
        if 'transfer-encoding' in fields or not size.isascii() or not size.isdecimal():
            return await self.clipboard_response(writer, b'400 Bad Request')
        length = int(size)
        if content_type != expected:
            return await self.clipboard_response(writer, b'415 Unsupported Media Type')
        if not 0 < length <= limit:
            return await self.clipboard_response(writer, b'413 Payload Too Large')
        try:
            payload = await asyncio.wait_for(reader.readexactly(length), 20)
            validate_clipboard(kind, payload)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            return await self.clipboard_response(writer, b'400 Bad Request')
        control_w = None
        try:
            control_r, control_w = await asyncio.wait_for(
                open_unix_connection('/desktop/control.sock'), 5)
            control_w.write(PROTOCOL + kind.encode('ascii') + b'\n'
                            + str(length).encode('ascii') + b'\n' + payload)
            await asyncio.wait_for(control_w.drain(), 10)
            if await asyncio.wait_for(control_r.readline(), 20) != b'OK\n':
                raise RuntimeError('sandbox rejected clipboard data')
        except (asyncio.TimeoutError, OSError, ValueError, RuntimeError):
            return await self.clipboard_response(writer, b'502 Bad Gateway', b'sandbox clipboard unavailable')
        finally:
            if control_w:
                control_w.close()
        await self.clipboard_response(writer, b'204 No Content')


def desktop_websocket(headers):
    fields = {k.lower(): v.lower() for k, v in headers}
    return fields.get('upgrade') == 'websocket' and 'upgrade' in {
        value.strip() for value in fields.get('connection', '').split(',')}


def desktop_transport_headers(headers):
    websocket = desktop_websocket(headers)
    allowed = {'if-modified-since', 'if-none-match'}
    if websocket:
        allowed.update({'sec-websocket-key', 'sec-websocket-version',
                        'sec-websocket-protocol', 'sec-websocket-extensions'})
    result = [('Host','127.0.0.1:6081'), ('Origin','http://127.0.0.1:6081'),
              ('User-Agent','CCBOX-Desktop-Viewer'), ('Accept','*/*'),
              ('Connection', 'Upgrade' if websocket else 'close')]
    if websocket:
        result.append(('Upgrade', 'websocket'))
    return result + [(k,v) for k,v in headers if k.lower() in allowed]


async def main():
    config = json.loads(Path("/run/secrets/upstream.json").read_text())
    g = Gateway(config["proxy"], config["desktop_token"], config["desktop_port"])
    socket = Path("/ipc/proxy.sock")
    socket.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(g.handle_proxy, str(socket), limit=65536)
    os.chmod(socket, 0o666)
    desktop = await asyncio.start_server(g.handle_desktop, "0.0.0.0", 6080, limit=65536)
    print("gateway ready; upstream-only; no direct fallback", flush=True)
    async with server, desktop:
        await asyncio.gather(server.serve_forever(), desktop.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
