from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from utils.conversations import spoken_diagnostic as diagnostic


class Snapshot:
    def __init__(self, data):
        self.data = data
        self.exists = data is not None

    def to_dict(self):
        return self.data


class Ref:
    def __init__(self, store, key):
        self.store = store
        self.key = key

    def get(self, transaction=None):
        return Snapshot(self.store.get(self.key))

    def collection(self, name):
        return Collection(self.store, f"{self.key}/{name}")


class Collection:
    def __init__(self, store, prefix):
        self.store = store
        self.prefix = prefix

    def document(self, name):
        return Ref(self.store, f"{self.prefix}/{name}")


class Transaction:
    def create(self, ref, value):
        assert ref.key not in ref.store
        ref.store[ref.key] = value

    def update(self, ref, value):
        ref.store[ref.key] = {**ref.store[ref.key], **value}


def fixture(monkeypatch):
    now = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)
    uid = "owner-test"
    conversation_id = "conversation-original"
    store = {
        f"{uid}/authority": {
            "protocol_version": 2, "state": "active", "generation": "generation-1", "owner_token": "owner-token",
            "conversation_id": conversation_id, "lease_expires_at": now + timedelta(seconds=30),
        },
        f"{uid}/{conversation_id}": {
            "id": conversation_id, "capture_protocol_version": 2, "capture_state": "active", "status": "in_progress",
            "capture_owner_id": "owner-token", "capture_generation": "generation-1", "capture_owner_token": "owner-token",
            "capture_lease_expires_at": now + timedelta(seconds=30),
        },
    }
    monkeypatch.setattr(diagnostic, "_authority_ref", lambda owner: Ref(store, f"{owner}/authority"))
    monkeypatch.setattr(diagnostic, "_conversation_ref", lambda owner, conversation: Ref(store, f"{owner}/{conversation}"))
    monkeypatch.setattr(diagnostic, "_capture_protocol", lambda: SimpleNamespace(
        CAPTURE_PROTOCOL_VERSION=2,
        _authority_tuple_matches=lambda a, c, g, o: (a["protocol_version"], a["conversation_id"], a["generation"], a["owner_token"]) == (2, c, g, o),
        _conversation_tuple_matches=lambda a, c, g, o: (a["id"], a["capture_protocol_version"], a["capture_generation"], a["capture_owner_token"]) == (c, 2, g, o),
    ))
    config = diagnostic.DiagnosticConfig("0" * 64, "canary-01", now, now + timedelta(minutes=5))
    return now, uid, conversation_id, store, config


def test_default_disabled_and_invalid_window(monkeypatch):
    monkeypatch.delenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", raising=False)
    assert diagnostic.current_config() is None
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_OWNER_SHA256", "0" * 64)
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_RUN_ID", "canary-01")
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_START_UTC", "2026-10-04T17:00:00Z")
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_END_UTC", "2026-10-04T17:11:00Z")
    assert diagnostic.current_config() is None
    assert diagnostic._phrase_in_segments([{"text": "silver lantern check in"}])
    assert not diagnostic._phrase_in_segments([{"text": "help me, silver lantern check in"}])
    assert not diagnostic._phrase_in_segments([
        {"text": "silver lantern check in"}, {"text": "I have chest pain"},
    ])


def test_claim_once_across_rollover_and_render_once(monkeypatch):
    now, uid, conversation_id, store, config = fixture(monkeypatch)
    tx = Transaction()
    claim = diagnostic._reserve_tx(tx, uid, conversation_id, config, now)
    assert claim and claim["response_version"] == diagnostic.RESPONSE_VERSION
    assert diagnostic._reserve_tx(tx, uid, conversation_id, config, now) is None
    store[f"{uid}/rollover"] = {**store[f"{uid}/{conversation_id}"]}
    assert diagnostic._reserve_tx(tx, uid, "rollover", config, now) is None
    claim_id = claim["claim_id"]
    rendering = diagnostic._transition_tx(tx, uid, claim_id, "claimed", "rendering", now)
    assert rendering and rendering["state"] == "rendering"
    assert diagnostic._transition_tx(tx, uid, claim_id, "claimed", "rendering", now) is None
    assert diagnostic._current_claim_tx(tx, uid, claim_id, "rendering", now)


def test_revoked_expired_or_replaced_capture_fails_closed(monkeypatch):
    now, uid, conversation_id, store, config = fixture(monkeypatch)
    tx = Transaction()
    claim = diagnostic._reserve_tx(tx, uid, conversation_id, config, now)
    assert claim
    claim_id = claim["claim_id"]
    store[f"{uid}/authority"]["state"] = "drained"
    assert diagnostic._transition_tx(tx, uid, claim_id, "claimed", "rendering", now) is None
    store[f"{uid}/authority"]["state"] = "active"
    store[f"{uid}/authority"]["generation"] = "generation-2"
    assert diagnostic._current_claim_tx(tx, uid, claim_id, "claimed", now) is None
    store[f"{uid}/authority"]["generation"] = "generation-1"
    assert diagnostic._current_claim_tx(tx, uid, claim_id, "claimed", now + timedelta(seconds=61)) is None
