import asyncio
import base64

import pytest

from sandbox.egress import EgressProxy, is_public, serve
from sandbox.runner import Execution, protect_secrets, run

TOKEN = "egress-token-0123456789"
AUTH = "Proxy-Authorization: Basic " + base64.b64encode(f"sandbox:{TOKEN}".encode()).decode()


@pytest.fixture
async def origin():
    """A tiny HTTP server on 127.0.0.1 standing in for a website."""

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        first = head.split(b"\r\n")[0].decode()
        body = f"hello from origin: {first}".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()


async def start_proxy(**kwargs):
    proxy = EgressProxy(TOKEN, **kwargs)
    server = await serve(proxy, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def exchange(port: int, request: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 5)
    writer.close()
    return data.decode(errors="replace")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.204",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "::ffff:192.168.1.1",
    ],
)
def test_private_addresses_are_not_public(address):
    assert not is_public(address)


async def test_credential_required(origin):
    server, port = await start_proxy()
    async with server:
        answer = await exchange(port, "CONNECT example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert answer.startswith("HTTP/1.1 407")


async def test_private_destinations_and_other_ports_are_refused(origin):
    async def to_lan(host, port):
        return ["192.168.0.204"]

    server, port = await start_proxy(resolver=to_lan)
    async with server:
        for target in ("router.lan:443", "127.0.0.1:443", "[::1]:443"):
            answer = await exchange(port, f"CONNECT {target} HTTP/1.1\r\n{AUTH}\r\n\r\n")
            assert answer.startswith("HTTP/1.1 403"), (target, answer)
        answer = await exchange(port, f"CONNECT example.com:22 HTTP/1.1\r\n{AUTH}\r\n\r\n")
        assert answer.startswith("HTTP/1.1 403") and "port 22" in answer
        answer = await exchange(port, f"GET http://router.lan/admin HTTP/1.1\r\n{AUTH}\r\n\r\n")
        assert answer.startswith("HTTP/1.1 403")


async def test_mixed_dns_answers_are_refused():
    async def rebinding(host, port):
        return ["93.184.216.34", "10.0.0.1"]

    server, port = await start_proxy(resolver=rebinding)
    async with server:
        answer = await exchange(port, f"CONNECT example.com:443 HTTP/1.1\r\n{AUTH}\r\n\r\n")
        assert answer.startswith("HTTP/1.1 403")


async def test_allowed_destinations_are_proxied(origin):
    """Loopback stands in for a public site here (the check is swapped for the test)."""

    async def to_origin(host, port):
        return ["127.0.0.1"]

    server, port = await start_proxy(resolver=to_origin, allowed=lambda a: True, ports={origin})
    async with server:
        answer = await exchange(
            port,
            f"GET http://site.test:{origin}/page?q=1 HTTP/1.1\r\nHost: site.test\r\n{AUTH}\r\n\r\n",
        )
        assert "hello from origin: GET /page?q=1 HTTP/1.1" in answer
        assert "Proxy-Authorization" not in answer

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT site.test:{origin} HTTP/1.1\r\n{AUTH}\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"GET /tunnel HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        assert b"GET /tunnel" in await asyncio.wait_for(reader.read(65536), 5)
        writer.close()


async def test_curl_through_the_proxy_from_sandboxed_code(config, mode, origin):
    """End to end: code in the sandbox uses the proxy environment it is given."""

    async def to_origin(host, port):
        return ["127.0.0.1"]

    server, port = await start_proxy(resolver=to_origin, allowed=lambda a: True, ports={origin})
    config.egress_proxy = f"http://127.0.0.1:{port}"
    config.egress_token = TOKEN
    async with server:
        code = f"curl -s -m 5 http://site.test:{origin}/from-sandbox"
        if mode == "bwrap":
            pytest.skip("bwrap shares the host network only through the proxy's container")
        result = await run(
            Execution(workspace="n", kind="shell", code=code, network=True), config, mode
        )
        assert "hello from origin: GET /from-sandbox" in result.stdout
        off = await run(
            Execution(workspace="n", kind="shell", code="env", network=False), config, mode
        )
        assert "HTTPS_PROXY" not in off.stdout and TOKEN not in off.stdout


def test_the_service_is_not_dumpable():
    """Non-dumpable: processes of the same uid (the code it runs) can't read its
    /proc/<pid>/environ or ptrace it, so its token and proxy credential stay private."""
    import ctypes

    assert protect_secrets()
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(3, 0, 0, 0, 0) == 0  # PR_GET_DUMPABLE
