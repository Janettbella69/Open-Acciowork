"""Official Weixin wire contracts and egress boundaries, entirely without networking."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import traceback
from collections.abc import AsyncIterator

import httpcore
import httpx
import pytest

from ophira_backend.weixin_transport import (
    MAX_WEIXIN_MESSAGES,
    MAX_WEIXIN_RESPONSE_BYTES,
    WEIXIN_API_ORIGIN,
    WeixinTransport,
    WeixinTransportError,
    validate_weixin_base_url,
)

TOKEN = "private-bot-token"
CONTEXT = "private-context-token"
QR = "private-qr-value+/=&"
PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda _host: [PUBLIC_IP])


def make_transport(payload, *, status: int = 200):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "ilinkai.weixin.qq.com"
        assert request.extensions["sni_hostname"] == "ilinkai.weixin.qq.com"
        return httpx.Response(status, json=payload)

    return WeixinTransport(transport=httpx.MockTransport(handle)), requests


async def send(transport: WeixinTransport, **overrides):
    kwargs = {
        "to_user_id": "owner@im.wechat", "text": "A direct reply\n第二行",
        "client_id": "stable-client-id", "context_token": CONTEXT,
    }
    kwargs.update(overrides)
    return await transport.send_text(TOKEN, **kwargs)


async def test_start_login_is_official_fixed_post_and_carries_no_account_secret() -> None:
    transport, requests = make_transport({"qrcode": QR, "qrcode_img_content": "weixin-qr-data"})
    result = await transport.start_login()
    assert result.qrcode == QR
    assert result.qrcode_img_content == "weixin-qr-data"
    assert QR not in repr(result)
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/ilink/bot/get_bot_qrcode"
    assert dict(request.url.params) == {"bot_type": "3"}
    assert json.loads(request.content) == {"local_token_list": []}
    assert "authorization" not in request.headers
    assert request.headers["authorizationtype"] == "ilink_bot_token"
    assert request.headers["ilink-app-id"] == "bot"
    assert int(request.headers["ilink-app-clientversion"]) == (2 << 16) | (4 << 8) | 9
    assert 0 <= int(base64.b64decode(request.headers["x-wechat-uin"])) < 2**32


async def test_login_query_is_encoded_without_bot_headers_or_secret_logs(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    transport, requests = make_transport({"status": "need_verifycode"})
    result = await transport.login_status(QR, verify_code="135 790&x=y")
    request = requests[0]
    assert result.status == "need_verifycode"
    assert request.method == "GET"
    assert request.url.path == "/ilink/bot/get_qrcode_status"
    assert dict(request.url.params) == {"qrcode": QR, "verify_code": "135 790&x=y"}
    assert request.content == b""
    assert not {"authorization", "authorizationtype", "x-wechat-uin"} & set(request.headers)
    assert QR not in caplog.text
    assert "135" not in caplog.text
    logging.getLogger("httpx").info("ordinary http request remains visible")
    assert "ordinary http request remains visible" in caplog.text


@pytest.mark.parametrize("status", [
    "wait", "scaned", "expired", "need_verifycode", "verify_code_blocked", "binded_redirect",
])
async def test_login_intermediate_states_return_without_fabricated_credentials(status) -> None:
    transport, requests = make_transport({"status": status})
    result = await transport.login_status(QR)
    assert result.status == status
    assert result.bot_token is None
    assert result.base_url is None
    assert len(requests) == 1


async def test_confirmed_login_returns_credentials_privately_and_canonical_origin() -> None:
    transport, _requests = make_transport({
        "status": "confirmed", "bot_token": TOKEN, "ilink_bot_id": "bot@im.bot",
        "ilink_user_id": "owner@im.wechat", "baseurl": WEIXIN_API_ORIGIN + "/",
    })
    result = await transport.login_status(QR)
    assert result.bot_token == TOKEN
    assert result.ilink_bot_id == "bot@im.bot"
    assert result.ilink_user_id == "owner@im.wechat"
    assert result.base_url == WEIXIN_API_ORIGIN
    assert TOKEN not in repr(result)
    assert "owner@im.wechat" not in repr(result)


@pytest.mark.parametrize("payload", [
    {"status": "confirmed"},
    {"status": "confirmed", "bot_token": TOKEN, "ilink_bot_id": "bot@im.bot"},
    {"status": "future_state"},
    {"status": []},
    {"status": "scaned_but_redirect"},
])
async def test_incomplete_or_unknown_login_responses_fail_closed(payload) -> None:
    transport, _requests = make_transport(payload)
    with pytest.raises(WeixinTransportError, match="MALFORMED_RESPONSE"):
        await transport.login_status(QR)


async def test_login_redirect_is_only_returned_and_never_automatically_followed() -> None:
    transport, requests = make_transport({
        "status": "scaned_but_redirect", "redirect_host": "ilinkai.weixin.qq.com",
    })
    result = await transport.login_status(QR)
    assert result.redirect_host == "ilinkai.weixin.qq.com"
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [
    {"status": "scaned_but_redirect", "redirect_host": "unknown.weixin.qq.com"},
    {"status": "scaned_but_redirect", "redirect_host": "127.0.0.1"},
    {"status": "scaned_but_redirect", "redirect_host": "ilinkai.weixin.qq.com/evil"},
    {"status": "confirmed", "baseurl": "https://ilinkai.weixin.qq.com.evil.test"},
])
async def test_server_cannot_redirect_credentials_or_qr_to_unreviewed_hosts(payload) -> None:
    transport, requests = make_transport(payload)
    with pytest.raises(WeixinTransportError, match="DESTINATION_REJECTED"):
        await transport.login_status(QR)
    assert len(requests) == 1


async def test_updates_keep_uint64_ids_and_untrusted_fields_for_channel_validation() -> None:
    message = {
        "message_id": 18446744073709551615, "from_user_id": "owner@im.wechat",
        "to_user_id": "bot@im.bot", "message_type": 1, "message_state": 2,
        "context_token": CONTEXT, "group_id": "unsupported-group",
        "item_list": [{"type": 1, "text_item": {"text": "Untrusted task"}}],
    }
    transport, requests = make_transport({
        "ret": 0, "msgs": [message], "get_updates_buf": "next-cursor",
        "longpolling_timeout_ms": 35000,
    })
    result = await transport.get_updates(TOKEN, cursor="previous-cursor")
    assert result.msgs == (message,)
    assert result.msgs[0]["message_id"] == 18446744073709551615
    assert result.get_updates_buf == "next-cursor"
    assert result.longpolling_timeout_ms == 35000
    assert CONTEXT not in repr(result)
    request = requests[0]
    assert request.url.path == "/ilink/bot/getupdates"
    assert request.headers["authorization"] == "Bearer " + TOKEN
    assert json.loads(request.content) == {
        "get_updates_buf": "previous-cursor",
        "base_info": {"channel_version": "2.4.9", "bot_agent": "Ophira/0.1.0"},
    }


@pytest.mark.parametrize("payload,code", [
    ({"ret": -14, "errmsg": TOKEN}, "SESSION_EXPIRED"),
    ({"ret": 0, "errcode": -14}, "SESSION_EXPIRED"),
    ({"ret": 123, "errmsg": TOKEN}, "API_REJECTED"),
    ({"ret": False}, "MALFORMED_RESPONSE"),
    ({"ret": 0, "msgs": ["bad"]}, "MALFORMED_RESPONSE"),
    ({"ret": 0, "msgs": [{}] * (MAX_WEIXIN_MESSAGES + 1)}, "RESPONSE_TOO_LARGE"),
    ({"ret": 0, "get_updates_buf": 10}, "MALFORMED_RESPONSE"),
    ({"ret": 0, "longpolling_timeout_ms": True}, "MALFORMED_RESPONSE"),
])
async def test_update_error_codes_are_typed_and_do_not_echo_upstream_details(payload, code) -> None:
    transport, _requests = make_transport(payload)
    with pytest.raises(WeixinTransportError) as error:
        await transport.get_updates(TOKEN)
    assert error.value.code == code
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("server_id", [None, 18446744073709551615, "18446744073709551615"])
async def test_send_is_one_context_bound_request_with_explicit_ack(server_id) -> None:
    response = {"ret": 0}
    if server_id is not None:
        response["message_id"] = server_id
    transport, requests = make_transport(response)
    result = await send(transport, run_id="run-123")
    assert result.message_id == (str(server_id) if server_id is not None else None)
    assert result.client_id == "stable-client-id"
    assert len(requests) == 1
    assert requests[0].url.path == "/ilink/bot/sendmessage"
    msg = json.loads(requests[0].content)["msg"]
    assert msg == {
        "from_user_id": "", "to_user_id": "owner@im.wechat", "client_id": "stable-client-id",
        "message_type": 2, "message_state": 2, "context_token": CONTEXT,
        "item_list": [{"type": 1, "text_item": {"text": "A direct reply\n第二行"}}],
        "run_id": "run-123",
    }


@pytest.mark.parametrize("payload,code,unknown", [
    ({}, "MALFORMED_RESPONSE", True),
    ({"ret": False}, "MALFORMED_RESPONSE", True),
    ({"ret": "0"}, "MALFORMED_RESPONSE", True),
    ({"ret": 0, "message_id": True}, "MALFORMED_RESPONSE", True),
    ({"ret": 0, "message_id": 2**64}, "MALFORMED_RESPONSE", True),
    ({"ret": -14, "errmsg": TOKEN}, "SESSION_EXPIRED", False),
    ({"ret": 999, "errmsg": CONTEXT}, "API_REJECTED", False),
])
async def test_send_never_infers_ack_or_retries_bad_responses(payload, code, unknown):
    transport, requests = make_transport(payload)
    with pytest.raises(WeixinTransportError) as error:
        await send(transport)
    assert error.value.code == code
    assert error.value.outcome_unknown is unknown
    assert error.value.retryable is False
    assert len(requests) == 1
    assert TOKEN not in str(error.value)
    assert CONTEXT not in str(error.value)


@pytest.mark.parametrize("kwargs", [
    {"to_user_id": "group@chatroom"}, {"to_user_id": "@im.wechat"},
    {"to_user_id": "owner@im.wechat\n"}, {"to_user_id": "x@y@im.wechat"},
    {"context_token": ""}, {"text": "x" * 4001}, {"text": "\ud800"},
    {"client_id": "x\x00y"}, {"run_id": "x" * 257},
])
async def test_invalid_outbound_requests_fail_before_any_network_call(kwargs) -> None:
    transport, requests = make_transport({"ret": 0})
    with pytest.raises(WeixinTransportError, match="INVALID_ARGUMENT"):
        await send(transport, **kwargs)
    assert requests == []


@pytest.mark.parametrize("url", [
    "http://ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com.evil.test",
    "https://other.weixin.qq.com", "https://127.0.0.1", "https://[::1]",
    "https://ilinkai.weixin.qq.com:444", "https://user:pass@ilinkai.weixin.qq.com",
    "https://ilinkai.weixin.qq.com/path", "https://ilinkai.weixin.qq.com?token=secret",
    "https://ilinkai.weixin.qq.com#fragment", " https://ilinkai.weixin.qq.com",
    "https://ilinkai.weixin.qq.com\\evil", "https://ilinkai.weixin.qq.com:invalid",
])
async def test_arbitrary_base_urls_are_rejected_before_io(url) -> None:
    transport, requests = make_transport({"ret": 0})
    with pytest.raises(WeixinTransportError, match="DESTINATION_REJECTED"):
        await transport.get_updates(TOKEN, base_url=url)
    assert requests == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
async def test_private_dns_cannot_receive_bot_credentials(monkeypatch, address) -> None:
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda _host: [address])
    transport, requests = make_transport({"ret": 0})
    with pytest.raises(WeixinTransportError, match="DESTINATION_REJECTED"):
        await transport.get_updates(TOKEN)
    assert requests == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_http_redirects_are_not_followed(status) -> None:
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/?token=" + TOKEN})

    transport = WeixinTransport(transport=httpx.MockTransport(handle))
    with pytest.raises(WeixinTransportError, match="REDIRECT_REJECTED"):
        await transport.get_updates(TOKEN)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError])
@pytest.mark.parametrize("is_send", [False, True])
async def test_network_failure_is_sanitized_and_send_is_never_retried(failure, is_send) -> None:
    requests = []

    def handle(request):
        requests.append(request)
        raise failure("upstream echoed " + TOKEN, request=request)

    transport = WeixinTransport(transport=httpx.MockTransport(handle))
    with pytest.raises(WeixinTransportError) as error:
        if is_send:
            await send(transport)
        else:
            await transport.get_updates(TOKEN)
    assert len(requests) == 1
    assert error.value.retryable is not is_send
    assert error.value.outcome_unknown is is_send
    assert TOKEN not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("status,unknown", [
    (400, False), (408, True), (429, False), (500, True), (503, True),
])
async def test_send_http_rejection_does_not_echo_body_or_retry(status, unknown) -> None:
    transport, requests = make_transport({"errmsg": TOKEN}, status=status)
    with pytest.raises(WeixinTransportError) as error:
        await send(transport)
    assert error.value.code == f"HTTP_{status}"
    assert error.value.retryable is False
    assert error.value.outcome_unknown is unknown
    assert len(requests) == 1


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False
        self.read_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_stream_size_is_bounded_before_reading_entire_body_and_closed() -> None:
    stream = Chunks([b"x" * MAX_WEIXIN_RESPONSE_BYTES, b"x", b"must-not-read"])
    transport = WeixinTransport(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=stream),
    ))
    with pytest.raises(WeixinTransportError, match="RESPONSE_TOO_LARGE") as error:
        await send(transport)
    assert error.value.outcome_unknown
    assert stream.closed
    assert stream.read_count == 2


@pytest.mark.parametrize("body", [
    b'[]', b'{"ret":0,"ret":1}', b'{"ret":0,"extra":NaN}',
    b'{"ret":0,"extra":1e9999}', b'not json',
    b'{"ret":0,"extra":' + b'[' * 25 + b'0' + b']' * 25 + b'}',
])
async def test_invalid_ambiguous_or_deep_json_never_confirms_send(body) -> None:
    transport = WeixinTransport(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=body),
    ))
    with pytest.raises(WeixinTransportError) as error:
        await send(transport)
    assert error.value.outcome_unknown
    assert not error.value.retryable


@pytest.mark.parametrize("headers,code", [
    ({"content-length": str(MAX_WEIXIN_RESPONSE_BYTES + 1)}, "RESPONSE_TOO_LARGE"),
    ({"content-length": "broken"}, "MALFORMED_RESPONSE"),
    ({"content-encoding": "gzip"}, "MALFORMED_RESPONSE"),
])
async def test_oversized_and_compressed_responses_are_rejected_before_body(headers, code):
    stream = Chunks([b"must-not-read"])
    transport = WeixinTransport(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, headers=headers, stream=stream),
    ))
    with pytest.raises(WeixinTransportError, match=code):
        await transport.get_updates(TOKEN)
    assert stream.read_count == 0
    assert stream.closed


async def test_cancellation_propagates_and_log_filter_is_reset(caplog) -> None:
    entered = asyncio.Event()

    async def handle(_request):
        entered.set()
        await asyncio.Event().wait()

    transport = WeixinTransport(transport=httpx.MockTransport(handle))
    task = asyncio.create_task(transport.get_updates(TOKEN))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").info("unrelated diagnostics")
    assert "unrelated diagnostics" in caplog.text


async def test_deadline_also_bounds_dns_resolution_and_avoids_network(monkeypatch) -> None:
    async def stalled(_client, *_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("ophira_backend.weixin_transport.send_pinned_public_request", stalled)
    transport = WeixinTransport(timeout_seconds=0.01)
    with pytest.raises(WeixinTransportError, match="TIMEOUT") as error:
        await send(transport)
    assert error.value.outcome_unknown
    assert not error.value.retryable


class MemoryStream:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        body = json.dumps({"status": "wait"}).encode()
        self.data = (
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\nSet-Cookie: " + TOKEN.encode() + b"\r\n\r\n" + body
        )

    async def read(self, max_bytes, **_kwargs):
        if self.fail:
            raise httpcore.ReadError("server echoed " + TOKEN)
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


class MemoryNetwork:
    def __init__(self, fail):
        self.fail = fail

    async def connect_tcp(self, *_args, **_kwargs):
        return MemoryStream(fail=self.fail)

    async def connect_unix_socket(self, *_args, **_kwargs):
        raise AssertionError("No sockets are allowed")

    async def sleep(self, seconds):
        pass


@pytest.mark.parametrize("fail", [True, False])
async def test_real_httpcore_logging_never_exposes_qr_credentials_or_headers(caplog, fail):
    caplog.set_level(logging.DEBUG)
    http_transport = httpx.AsyncHTTPTransport()
    http_transport._pool = httpcore.AsyncConnectionPool(network_backend=MemoryNetwork(fail))
    transport = WeixinTransport(transport=http_transport)
    if fail:
        with pytest.raises(WeixinTransportError, match="NETWORK_ERROR"):
            await transport.login_status(QR, verify_code="975310")
    else:
        assert (await transport.login_status(QR, verify_code="975310")).status == "wait"
    assert TOKEN not in caplog.text
    assert "private-qr-value" not in caplog.text
    assert "975310" not in caplog.text
    logging.getLogger("httpcore.http11").debug("ordinary response remains visible")
    assert "ordinary response remains visible" in caplog.text


def test_origin_normalization_accepts_only_reviewed_default_and_standard_port() -> None:
    assert validate_weixin_base_url(WEIXIN_API_ORIGIN) == WEIXIN_API_ORIGIN
    assert validate_weixin_base_url(WEIXIN_API_ORIGIN + ":443/") == WEIXIN_API_ORIGIN


async def test_empty_poll_can_omit_default_fields_without_inventing_cursor_progress() -> None:
    transport, _requests = make_transport({})
    updates = await transport.get_updates(TOKEN, cursor="existing-cursor")
    assert updates.msgs == ()
    assert updates.get_updates_buf is None
