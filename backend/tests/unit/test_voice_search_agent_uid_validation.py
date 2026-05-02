from ella.routers.voice import _validate_agent_uid


def test_validate_agent_uid_accepts_case_mismatched_omi_agent_id():
    uid = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
    agent_id = "ella-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2"

    assert _validate_agent_uid(agent_id, uid) is True


def test_validate_agent_uid_rejects_unrelated_agent_id():
    uid = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
    agent_id = "ella-omi-someone-else"

    assert _validate_agent_uid(agent_id, uid) is False
