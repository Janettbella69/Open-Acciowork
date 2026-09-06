"""Exercise real httpcore logging without sockets, DNS, TLS or external delivery."""

import logging

import httpcore
import httpx
import pytest
from test_companion_push import settings, subscription

from ophira_backend.push_transport import WebPushAdapter

CAPABILITY = "very-private-capability"


class MemoryPushStream:
    def __init__(self, *, fail: bool):
        self.fail = fail
        self.data = (
            b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n"
            b"Location: https://fcm.googleapis.com/wpush/very-private-capability\r\n\r\n"
        )

    async def read(self, max_bytes, **_kwargs):
        if self.fail:
            raise httpcore.ReadError(f"push service echoed {CAPABILITY}")
        data, self.data = self.data[:max_bytes], self.data[max_bytes:]
        return data

    async def write(self, buffer, **_kwargs):
        pass

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, **_kwargs):
        return self

    def get_extra_info(self, name):
        return None


class MemoryPushNetwork:
    def __init__(self, *, fail):
        self.fail = fail

    async def connect_tcp(self, *args, **kwargs):
        return MemoryPushStream(fail=self.fail)

    async def connect_unix_socket(self, *args, **kwargs):
        raise AssertionError("This test never connects sockets")

    async def sleep(self, seconds):
        pass


@pytest.mark.parametrize("fail", [False, True])
async def test_push_httpcore_headers_and_failure_logs_never_expose_capabilities(
    tmp_path, monkeypatch, caplog, fail,
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda host: ["1.1.1.1"])
    transport = httpx.AsyncHTTPTransport()
    # Unlike MockTransport this executes httpcore's real response-header and
    # exception tracing path; only its network backend is replaced in memory.
    transport._pool = httpcore.AsyncConnectionPool(network_backend=MemoryPushNetwork(fail=fail))
    adapter = WebPushAdapter(settings(tmp_path), transport=transport)
    outcome = await adapter.send(subscription(), "offline-topic")
    assert outcome.status == ("retry" if fail else "accepted")
    assert CAPABILITY not in caplog.text
    assert subscription().keys.auth not in caplog.text
    # Logger filters must not hide other tasks' HTTP diagnostics after reset.
    logging.getLogger("httpcore.http11").debug("ordinary httpcore response remains visible")
    logging.getLogger("httpx").info("ordinary httpx request remains visible")
    assert "ordinary httpcore response remains visible" in caplog.text
    assert "ordinary httpx request remains visible" in caplog.text
