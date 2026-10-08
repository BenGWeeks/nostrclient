import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from nostr_sdk import RelayMessage, RelayUrl
from nostr_sdk import RelayStatus as SdkRelayStatus

from .. import router as router_module
from ..nostr import relay_manager as relay_manager_module
from ..nostr.message_pool import MessagePool
from ..nostr.relay_manager import (
    CLOSED_RESEND_MAX_DELAY,
    CLOSED_RESEND_MIN_DELAY,
    RelayConnection,
    RelayManager,
    _NotificationHandler,
)
from ..router import NostrRouter

URL = "wss://relay.example"


class FakeSdkRelay:
    def __init__(self, status=SdkRelayStatus.CONNECTED, success=1):
        self._status = status
        self._success = success

    def status(self):
        return self._status

    def stats(self):
        return SimpleNamespace(success=lambda: self._success, latency=lambda: None)


class FakeClient:
    def __init__(self, sdk_relay: FakeSdkRelay):
        self.sdk_relay = sdk_relay
        self.sent: list[list] = []
        self.connect_calls = 0
        self.fail_send = False

    async def relays(self):
        return {RelayUrl.parse(URL): self.sdk_relay}

    async def connect_relay(self, _relay_url):
        self.connect_calls += 1

    async def send_msg_to(self, relay_urls, message):
        if self.fail_send:
            raise RuntimeError("relay not connected")
        assert [str(u) for u in relay_urls] == [URL]
        self.sent.append(json.loads(message.as_json()))


def _manager(sdk_relay: FakeSdkRelay, subscriptions: dict | None = None):
    """A RelayManager wired to a fake SDK client (no thread, no network)."""
    manager = RelayManager.__new__(RelayManager)
    manager.relays = {}
    manager._cached_subscriptions = dict(subscriptions or {})
    manager._subscriptions_lock = threading.Lock()
    manager.message_pool = MessagePool()
    manager._client = FakeClient(sdk_relay)  # type: ignore[assignment]
    return manager


def _check(manager: RelayManager):
    asyncio.run(manager._reconnect_stale_relays())
    return manager._client.sent  # type: ignore[attr-defined]


def test_subscriptions_are_sent_again_after_sdk_reconnect():
    sdk_relay = FakeSdkRelay(success=1)
    manager = _manager(sdk_relay, {"sub1": [{"kinds": [23194]}]})

    assert _check(manager) == [["REQ", "sub1", {"kinds": [23194]}]]
    # same connection: nothing is sent again
    assert len(_check(manager)) == 1

    # the SDK dropped and re-established the connection on its own
    sdk_relay._success = 2
    assert _check(manager) == [["REQ", "sub1", {"kinds": [23194]}]] * 2
    assert manager.relays[URL].connection_count == 2


def test_multi_filter_subscription_is_replayed_intact():
    filters = [{"kinds": [23194]}, {"kinds": [23195]}]
    manager = _manager(FakeSdkRelay(), {"sub1": filters})
    assert _check(manager) == [["REQ", "sub1", *filters]]


def test_no_replay_while_relay_is_disconnected():
    sdk_relay = FakeSdkRelay(status=SdkRelayStatus.DISCONNECTED, success=1)
    manager = _manager(sdk_relay, {"sub1": [{"kinds": [1]}]})

    assert _check(manager) == []
    assert manager.relays[URL].connection_count == 0

    sdk_relay._status = SdkRelayStatus.CONNECTED
    assert len(_check(manager)) == 1


def test_failed_replay_is_retried_on_next_check():
    manager = _manager(FakeSdkRelay(), {"sub1": [{"kinds": [1]}]})
    manager._client.fail_send = True  # type: ignore[attr-defined]
    assert _check(manager) == []
    assert manager.relays[URL].connection_count == 0

    manager._client.fail_send = False  # type: ignore[attr-defined]
    assert len(_check(manager)) == 1


def test_terminated_relay_is_reconnected():
    manager = _manager(FakeSdkRelay(status=SdkRelayStatus.TERMINATED))
    _check(manager)
    assert manager._client.connect_calls == 1  # type: ignore[attr-defined]


def _closed(manager: RelayManager, subscription_id: str):
    message = RelayMessage.from_json(
        json.dumps(["CLOSED", subscription_id, "rate-limited: slow down"])
    )

    async def get_sdk_relay(url: str):
        return manager._client.sdk_relay  # type: ignore[attr-defined]

    manager._get_sdk_relay = get_sdk_relay  # type: ignore[method-assign]
    handler = _NotificationHandler(manager)
    asyncio.run(handler.handle_msg(RelayUrl.parse(URL), message))


def test_closed_subscription_is_sent_again_after_backoff(monkeypatch):
    manager = _manager(FakeSdkRelay(), {"sub1": [{"kinds": [23194]}]})
    assert len(_check(manager)) == 1

    now = time.time()
    monkeypatch.setattr(relay_manager_module.time, "time", lambda: now)
    _closed(manager, "sub1")
    relay = manager.relays[URL]
    assert relay.closed_subscriptions["sub1"].pending
    assert relay.error_list[0] == "rate-limited: slow down"

    # not before the backoff elapsed
    assert len(_check(manager)) == 1

    later = now + CLOSED_RESEND_MIN_DELAY + 1
    monkeypatch.setattr(relay_manager_module.time, "time", lambda: later)
    assert _check(manager)[-1] == ["REQ", "sub1", {"kinds": [23194]}]
    assert len(_check(manager)) == 2  # sent once per CLOSED


def test_closed_for_unknown_subscription_is_ignored():
    manager = _manager(FakeSdkRelay(), {"sub1": [{"kinds": [1]}]})
    _check(manager)
    _closed(manager, "someone-elses-sub")
    assert manager.relays[URL].closed_subscriptions == {}


def test_closed_backoff_grows_and_is_capped():
    relay = RelayConnection(url=URL)
    delays = [relay.subscription_closed("sub1", 1000.0 + i) for i in range(12)]
    assert delays[:3] == [
        CLOSED_RESEND_MIN_DELAY,
        CLOSED_RESEND_MIN_DELAY * 2,
        CLOSED_RESEND_MIN_DELAY * 4,
    ]
    assert delays[-1] == CLOSED_RESEND_MAX_DELAY
    # a CLOSED long after the previous one starts over
    quiet = 1000.0 + 2 * CLOSED_RESEND_MAX_DELAY
    assert relay.subscription_closed("sub1", quiet) == CLOSED_RESEND_MIN_DELAY


def test_client_close_forgets_closed_subscription():
    manager = _manager(FakeSdkRelay(), {"sub1": [{"kinds": [1]}]})
    _check(manager)
    _closed(manager, "sub1")
    manager._run_coro = lambda coro, **_k: coro.close()  # type: ignore[method-assign]
    manager.close_subscription("sub1")
    assert manager.relays[URL].closed_subscriptions == {}


def test_connection_changes_are_logged():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO", format="{message}")
    try:
        sdk_relay = FakeSdkRelay()
        manager = _manager(sdk_relay, {"sub1": [{"kinds": [1]}]})
        _check(manager)
        sdk_relay._status = SdkRelayStatus.DISCONNECTED
        _check(manager)
        _closed(manager, "sub1")
    finally:
        logger.remove(sink)
    log = "".join(lines)
    assert f"[Relay: {URL}] Connected." in log
    assert "Connection #1 established, sent 1 subscription(s)." in log
    assert "Disconnected (status: DISCONNECTED)." in log
    assert "CLOSED active subscription 'sub1'" in log


def test_req_with_same_id_replaces_previous_subscription(monkeypatch):
    closed: list[str] = []
    subscriptions: dict[str, list] = {}
    manager = SimpleNamespace(
        relays={URL: object()},
        add_subscription=lambda sid, filters: subscriptions.update({sid: filters}),
        close_subscription=lambda sid: (closed.append(sid), subscriptions.pop(sid)),
    )
    monkeypatch.setattr(
        router_module, "nostr_client", SimpleNamespace(relay_manager=manager)
    )
    nostr_router = NostrRouter(SimpleNamespace())  # type: ignore[arg-type]
    for kind in (1, 2):
        message = json.dumps(["REQ", "sub1", {"kinds": [kind]}])
        asyncio.run(nostr_router._handle_client_to_nostr(message))

    assert len(closed) == 1
    assert list(subscriptions.values()) == [[{"kinds": [2]}]]
    assert list(nostr_router.original_subscription_ids.values()) == ["sub1"]


def test_real_sdk_reconnect_resends_subscription():
    """End to end: a relay drops the connection, nostr-sdk reconnects by itself."""
    websockets = pytest.importorskip("websockets")
    connections: list[list] = []

    async def relay(ws):
        received: list = []
        connections.append(received)
        async for raw in ws:
            received.append(json.loads(raw))
            if len(connections) == 1:
                await ws.close()

    async def scenario(manager: RelayManager):
        server = await websockets.serve(relay, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            await asyncio.to_thread(manager.add_relay, url)
            await asyncio.to_thread(
                manager.add_subscription, "sub1", [{"kinds": [23194]}]
            )
            for _ in range(60):  # nostr-sdk retries after ~10s
                await asyncio.to_thread(manager.check_and_restart_relays)
                if len(connections) > 1 and connections[-1]:
                    break
                await asyncio.sleep(0.5)
        finally:
            server.close()

    manager = RelayManager()
    try:
        asyncio.run(scenario(manager))
    finally:
        manager.shutdown()

    assert len(connections) > 1, "nostr-sdk did not reconnect"
    assert ["REQ", "sub1", {"kinds": [23194]}] in connections[-1]
