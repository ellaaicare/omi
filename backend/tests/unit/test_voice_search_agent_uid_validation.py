from pathlib import Path
import types


def _load_validate_agent_uid():
    voice_py = Path(__file__).resolve().parents[2] / "ella" / "routers" / "voice.py"
    source = voice_py.read_text()
    start = source.index("def _validate_agent_uid(")
    end = source.index("\n\ndef _keyword_score(", start)
    module = types.ModuleType("voice_validate_agent_uid_test")
    exec(source[start:end], module.__dict__)
    return module._validate_agent_uid


def test_validate_agent_uid_accepts_case_mismatched_omi_agent_id():
    validate = _load_validate_agent_uid()
    uid = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
    agent_id = "ella-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2"

    assert validate(agent_id, uid) is True


def test_validate_agent_uid_rejects_unrelated_agent_id():
    validate = _load_validate_agent_uid()
    uid = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
    agent_id = "ella-omi-someone-else"

    assert validate(agent_id, uid) is False
