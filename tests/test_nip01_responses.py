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


def test_malformed_event_returns_ok_false(monkeypatch):
    router, websocket, manager = _router(monkeypatch, relays=["wss://relay.example"])
    asyncio.run(router._handle_client_to_nostr(json.dumps(["EVENT", "notadict"])))
    assert manager.published == []
    assert json.loads(websocket.sent[-1])[:3] == ["OK", "", False]


def test_relay_event_frames_reach_message_pool():
    from nostr_sdk import EventBuilder, Keys, RelayMessage, RelayUrl

    from ..nostr.relay_manager import _NotificationHandler

    added: list[tuple[str, str]] = []
    relay_state = SimpleNamespace(
        update_from_sdk=lambda _sdk: None, append_error=lambda _m: None
    )

    async def get_sdk_relay(_url):
        return None

    manager = SimpleNamespace(
        _ensure_relay_state=lambda _url: relay_state,
        _get_sdk_relay=get_sdk_relay,
        message_pool=SimpleNamespace(add_message=lambda m, u: added.append((m, u))),
    )
    event = EventBuilder.text_note("nwc").sign_with_keys(Keys.generate())
    message = RelayMessage.event("sub", event)
    handler = _NotificationHandler(manager)  # type: ignore[arg-type]
    asyncio.run(handler.handle_msg(RelayUrl.parse("wss://relay.example"), message))
    assert len(added) == 1
    assert json.loads(added[0][0])[0:2] == ["EVENT", "sub"]


def test_update_config_never_stores_private_endpoint(monkeypatch):
    from .. import views_api

    stored: list[Config] = []

    async def fake_update_config(owner_id, config):
        stored.append(config.model_copy())
        return config

    monkeypatch.setattr(views_api, "update_config", fake_update_config)
    monkeypatch.setattr(
        views_api, "encrypt_internal_message", lambda *_a, **_k: "fresh-token"
    )
    data = Config(public_ws=True, private_ws_endpoint="client-supplied")
    result = asyncio.run(views_api.api_update_config(data))
    assert stored[0].private_ws_endpoint is None
    assert stored[0].public_ws is True
    assert result["private_ws_endpoint"] == "fresh-token"
