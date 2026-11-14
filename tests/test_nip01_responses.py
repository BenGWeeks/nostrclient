import asyncio
import json
from types import SimpleNamespace

from .. import router as router_module
from ..models import Config, Relay, RelayDb, RelayStatus, UserConfig
from ..router import NostrRouter


class FakeWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str):
        self.sent.append(text)


class FakeRelayManager:
    def __init__(self, relays):
        self.relays = relays
        self.published: list[str] = []
        self.subscriptions: dict[str, list] = {}

    def publish_message(self, message: str):
        self.published.append(message)

    def add_subscription(self, subscription_id: str, filters: list):
        self.subscriptions[subscription_id] = filters


def _router(monkeypatch, relays):
    manager = FakeRelayManager(relays)
    monkeypatch.setattr(
        router_module, "nostr_client", SimpleNamespace(relay_manager=manager)
    )
    websocket = FakeWebSocket()
    return NostrRouter(websocket), websocket, manager  # type: ignore[arg-type]


def test_event_without_relays_returns_ok_false(monkeypatch):
    nostr_router, websocket, manager = _router(monkeypatch, {})
    message = json.dumps(["EVENT", {"id": "abc"}])
    asyncio.run(nostr_router._handle_client_to_nostr(message))
    assert [json.loads(m) for m in websocket.sent] == [
        ["OK", "abc", False, "error: no relay connections"]
    ]
    assert manager.published == []


def test_event_with_relays_is_published_and_returns_ok_true(monkeypatch):
    nostr_router, websocket, manager = _router(monkeypatch, {"wss://r": object()})
    message = json.dumps(["EVENT", {"id": "abc"}])
    asyncio.run(nostr_router._handle_client_to_nostr(message))
    assert [json.loads(m) for m in websocket.sent] == [["OK", "abc", True, ""]]
    assert manager.published == [message]


def test_req_without_relays_returns_closed(monkeypatch):
    nostr_router, websocket, manager = _router(monkeypatch, {})
    message = json.dumps(["REQ", "sub1", {"kinds": [1]}])
    asyncio.run(nostr_router._handle_client_to_nostr(message))
    assert [json.loads(m) for m in websocket.sent] == [
        ["CLOSED", "sub1", "error: no relay connections"]
    ]
    assert manager.subscriptions == {}
    assert nostr_router.subscriptions == []


def test_req_with_relays_subscribes_without_response(monkeypatch):
    nostr_router, websocket, manager = _router(monkeypatch, {"wss://r": object()})
    message = json.dumps(["REQ", "sub1", {"kinds": [1]}])
    asyncio.run(nostr_router._handle_client_to_nostr(message))
    assert websocket.sent == []
    assert list(nostr_router.original_subscription_ids.values()) == ["sub1"]
    assert list(manager.subscriptions.values()) == [[{"kinds": [1]}]]


def test_models_accept_null_values_and_defaults():
    relay = Relay.model_validate(
        {"id": None, "url": None, "active": None, "connected": None, "status": None}
    )
    assert relay.model_dump()["status"] is None
    assert RelayDb.model_validate({"id": "1", "url": "wss://r", "active": None})
    assert RelayStatus.model_validate({"error_list": None}).error_list is None
    config = Config.model_validate({"private_ws": True, "public_ws": False})
    assert config.private_ws_endpoint is None
    assert UserConfig(owner_id="admin").extra == Config()
    assert "private_ws_endpoint" in json.loads(config.model_dump_json())
