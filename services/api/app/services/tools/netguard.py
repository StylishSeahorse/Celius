"""Outbound HTTP for tools, guarded against SSRF.

Only http(s) on the default ports to public addresses. The guard sits in the connection
layer: the host is resolved, every address it resolves to must be public, and the socket
connects to one of exactly those addresses. A DNS answer can't change between the
check and the connection (rebinding), and every redirect goes through the same path.
TLS still verifies the certificate against the hostname.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpcore
import httpx

ALLOWED_PORTS = {"http": 80, "https": 443}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedDestination(Exception):
    """The URL or the address it resolves to is not allowed."""


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global excludes private, loopback, link-local, CGNAT (100.64/10), reserved,
    # documentation and unspecified ranges.
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_url(url: str, allowlist: list[str] | None = None) -> str:
    """Validate scheme, port, credentials and the host allowlist. Returns the host."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BlockedDestination("Invalid URL") from exc
    if parts.scheme not in ALLOWED_PORTS:
        raise BlockedDestination("Only http and https URLs are allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise BlockedDestination("The URL has no host")
    if parts.username or parts.password:
        raise BlockedDestination("URLs with credentials are not allowed")
    if port is not None and port != ALLOWED_PORTS[parts.scheme]:
        raise BlockedDestination("Only the standard ports (80, 443) are allowed")
    if allowlist and not any(host == h or host.endswith("." + h) for h in allowlist):
        raise BlockedDestination(f"{host} is not on the web allowlist")
    return host


async def resolve_checked(
    host: str, port: int, *, resolver: Resolver = system_resolver, allow_private: bool = False
) -> list[str]:
    try:
        addresses = [host] if _is_ip(host) else await resolver(host, port)
    except OSError as exc:
        raise BlockedDestination(f"Could not resolve {host}") from exc
    if not addresses:
        raise BlockedDestination(f"Could not resolve {host}")
    if not allow_private:
        # All of them, not just the first: a mixed answer is a rebinding attempt.
        blocked = [a for a in addresses if not is_public(a)]
        if blocked:
            raise BlockedDestination(f"{host} resolves to a private or reserved address")
    return addresses


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolver: Resolver, allow_private: bool):
        self._inner = httpcore.AnyIOBackend()
        self._resolver = resolver
        self._allow_private = allow_private

    async def connect_tcp(
        self,
        host,
        port,
        timeout=None,  # noqa: ASYNC109 -- httpcore's interface
        local_address=None,
        socket_options=None,
    ):
        addresses = await resolve_checked(
            host.strip("[]"), port, resolver=self._resolver, allow_private=self._allow_private
        )
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        assert error is not None
        raise error

    async def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover
        raise BlockedDestination("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose connections can only reach checked public addresses.
    Ignores proxy environment variables: the guard must see the real destination."""

    def __init__(self, *, resolver: Resolver = system_resolver, allow_private: bool = False):
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=10,
            network_backend=_PinnedBackend(resolver, allow_private),
        )


def guarded_client(
    *,
    timeout: float,
    resolver: Resolver = system_resolver,
    allow_private: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Redirects are not followed automatically: callers re-check each Location."""
    return httpx.AsyncClient(
        transport=transport or GuardedTransport(resolver=resolver, allow_private=allow_private),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers=headers,
    )
