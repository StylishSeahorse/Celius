"""Egress proxy: the sandbox's only way to the internet.

The sandbox container sits on an internal network with no route out. Code that needs the
internet (pip, npm, curl, git, requests…) goes through this HTTP proxy, which:

- requires the per-deployment proxy credential (given only to runs with network on);
- allows only the standard web ports (80, 443 by default);
- resolves the host itself and refuses private, loopback, link-local, CGNAT, multicast
  and reserved addresses, so the LAN (router, model server, NAS…) stays unreachable,
  and connects to exactly the address it checked (no DNS rebinding);
- logs each destination.

    python -m sandbox.egress          # listens on 0.0.0.0:3128
"""

import asyncio
import base64
import binascii
import contextlib
import hmac
import ipaddress
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

log = logging.getLogger("egress")

HEAD_LIMIT = 64 * 1024
IDLE_SECONDS = 120
CONNECT_SECONDS = 15

Resolver = Callable[[str, int], Awaitable[list[str]]]


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


class EgressProxy:
    def __init__(
        self,
        token: str,
        *,
        ports: set[int] | frozenset[int] = frozenset({80, 443}),
        resolver: Resolver = system_resolver,
        allowed: Callable[[str], bool] = is_public,
        max_connections: int = 128,
    ):
        if len(token) < 16:
            raise RuntimeError("SANDBOX_EGRESS_TOKEN must be set (at least 16 characters)")
        self.token = token
        self.ports = set(ports)
        self.resolver = resolver
        self.allowed = allowed
        self.slots = asyncio.Semaphore(max_connections)

    # ------------------------------------------------------------------ checks

    def _authorised(self, headers: dict[str, str]) -> bool:
        scheme, _, value = headers.get("proxy-authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            _, _, password = base64.b64decode(value).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        return hmac.compare_digest(password, self.token)

    async def _connect(self, host: str, port: int):
        host = host.strip("[]").lower()
        if port not in self.ports:
            raise Refused(403, f"port {port} is not allowed")
        try:
            addresses = [host] if _is_ip(host) else await self.resolver(host, port)
        except OSError as exc:
            raise Refused(502, f"cannot resolve {host}") from exc
        if not addresses:
            raise Refused(502, f"cannot resolve {host}")
        if not all(self.allowed(a) for a in addresses):
            raise Refused(403, f"{host} is a private or reserved address")
        error: Exception | None = None
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port), CONNECT_SECONDS
                )
            except (OSError, TimeoutError) as exc:
                error = exc
        raise Refused(502, f"cannot connect to {host}: {error}")

    # ------------------------------------------------------------------ serving

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self.slots:
            try:
                await self._handle(reader, writer)
            except Refused as exc:
                log.info("refused: %s", exc)
                await _reply(writer, exc.status, str(exc))
            except (ConnectionError, TimeoutError, asyncio.IncompleteReadError):
                pass
            except Exception:
                log.exception("proxy error")
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

    async def _handle(self, reader, writer) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_SECONDS)
        if len(head) > HEAD_LIMIT:
            raise Refused(431, "request head too large")
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = (lines[0].split(" ") + ["", ""])[:3]
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        if not self._authorised(headers):
            await _reply(
                writer,
                407,
                "proxy authentication required",
                extra='Proxy-Authenticate: Basic realm="celius"\r\n',
            )
            return

        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            upstream_reader, upstream_writer = await self._connect(host, int(port or 443))
            log.info("CONNECT %s", target)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _pipe_both(reader, writer, upstream_reader, upstream_writer)
            return

        # Plain http: absolute-form request line.
        if not target.startswith("http://"):
            raise Refused(400, "only absolute http:// URLs or CONNECT")
        parts = urlsplit(target)
        try:
            host, port = parts.hostname or "", parts.port or 80
        except ValueError as exc:
            raise Refused(400, "bad URL") from exc
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        upstream_reader, upstream_writer = await self._connect(host, port)
        log.info("%s http://%s:%s/", method, host, port)
        kept = [
            line
            for line in lines[1:]
            if line and not line.lower().startswith(("proxy-", "connection:", "keep-alive:"))
        ]
        upstream_writer.write(
            f"{method} {path} {version}\r\n".encode("latin-1")
            + "\r\n".join([*kept, "Connection: close", "", ""]).encode("latin-1")
        )
        await upstream_writer.drain()
        await _pipe_both(reader, writer, upstream_reader, upstream_writer)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _reply(writer, status: int, reason: str, extra: str = "") -> None:
    body = f"{status} {reason}\n".encode()
    with contextlib.suppress(Exception):
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n{extra}Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await asyncio.wait_for(source.read(65536), IDLE_SECONDS):
            sink.write(chunk)
            await sink.drain()
    finally:
        with contextlib.suppress(Exception):
            sink.write_eof()


async def _pipe_both(client_reader, client_writer, upstream_reader, upstream_writer) -> None:
    try:
        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
            return_exceptions=True,
        )
    finally:
        with contextlib.suppress(Exception):
            upstream_writer.close()


async def serve(proxy: EgressProxy, host: str = "0.0.0.0", port: int = 3128):
    return await asyncio.start_server(proxy.handle, host, port)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s egress %(message)s")
    ports = {int(p) for p in os.environ.get("EGRESS_PORTS", "80,443").split(",") if p.strip()}
    proxy = EgressProxy(os.environ.get("SANDBOX_EGRESS_TOKEN", ""), ports=ports)

    async def run() -> None:
        server = await serve(proxy, port=int(os.environ.get("EGRESS_LISTEN_PORT", "3128")))
        log.info("egress proxy listening (ports %s)", sorted(ports))
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
