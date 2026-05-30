from datetime import datetime, timezone
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
from ella.services.correction_propagation import run_correction_propagation


def _conversation(conversation_id: str, uid: str, title: str, overview: str, text: str, hour: int = 9):
    return {
        "id": conversation_id,
        "uid": uid,
        "created_at": datetime(2026, 5, 29, hour, 30, tzinfo=timezone.utc),
        "source": "omi",
        "structured": {
            "title": title,
            "overview": overview,
            "category": "education",
        },
        "active_summary_version_id": f"{conversation_id}-v1",
        "summary_versions": [{"id": f"{conversation_id}-v1", "is_active": True}],
        "transcript_segments": [{"speaker": "Speaker", "text": text}],
    }


def test_correction_propagation_creates_idempotent_same_user_candidate_proposal():
    source = _conversation(
        "conv-source",
        "user-123",
        "Mei Xin teacher meeting",
        "[Ella] The summary called Mei Xin a teen.",
        "Mei Xin wanted to email her teacher instead of meeting in person.",
    )
    related = _conversation(
        "conv-related",
        "user-123",
        "Teacher email follow-up",
        "[Ella] Mei Xin discussed emailing her teacher.",
        "Mei Xin and Greg talked about the teacher email plan.",
    )
    other_user = _conversation(
        "conv-other-user",
        "user-999",
        "Teacher email follow-up",
        "[Ella] Mei Xin discussed emailing her teacher.",
        "Mei Xin and Greg talked about the teacher email plan.",
    )
    unrelated = _conversation(
        "conv-unrelated",
        "user-123",
        "Grocery list",
        "[Ella] Plato bought apples.",
        "Apples and bread were on the list.",
    )
    seen = {}

    def load_candidates(uid, source_conversation, limit):
        assert uid == "user-123"
        return [source_conversation, related, other_user, unrelated]

    def create_proposal(**kwargs):
        key = kwargs["idempotency_key"]
        if key in seen:
            return {"created": False, "deduped": True, "proposal": seen[key]}
        proposal = {
            "proposal_id": "proposal-related",
            "idempotency_key": key,
            "payload": kwargs["payload"],
        }
        seen[key] = proposal
        return {"created": True, "deduped": False, "proposal": proposal}

    run1 = run_correction_propagation(
        uid="user-123",
        source_conversation=source,
        correction_id="corr-123",
        trace_id="trace-123",
        correction_text="Actually Mei Xin, also called Rain, was the student emailing the teacher.",
        correction_type="identity",
        candidate_loader=load_candidates,
        create_proposal=create_proposal,
        min_confidence=0.7,
    )
    run2 = run_correction_propagation(
        uid="user-123",
        source_conversation=source,
        correction_id="corr-123",
        trace_id="trace-123",
        correction_text="Actually Mei Xin, also called Rain, was the student emailing the teacher.",
        correction_type="identity",
        candidate_loader=load_candidates,
        create_proposal=create_proposal,
        min_confidence=0.7,
    )

    assert run1.run_id == "omi-correction-propagation:user-123:conv-source:corr-123"
    assert run1.proposal_count == 1
    assert run1.auto_applied_count == 0
    assert [d.action for d in run1.decisions].count("created_proposal") == 1
    assert any(d.reason == "cross_user_candidate" for d in run1.decisions)
    assert any(d.conversation_id == "conv-unrelated" and d.action == "skip" for d in run1.decisions)
    created = next(d for d in run1.decisions if d.action == "created_proposal")
    assert created.conversation_id == "conv-related"
    assert "teacher" in created.overlap_terms
    assert run2.proposal_count == 1
    assert any(d.action == "deduped_proposal" for d in run2.decisions)
    assert len(seen) == 1


def test_correction_propagation_skips_active_version_incompatible_candidate():
    source = _conversation("conv-source", "user-123", "Cafe order", "[Ella] Cafe order.", "Cafe Coffee Waffle Stop.")
    stale = _conversation("conv-stale", "user-123", "Cafe order", "[Ella] Cafe order.", "Cafe Coffee Waffle Stop.")
    stale["active_summary_version_id"] = "missing-active-version"

    run = run_correction_propagation(
        uid="user-123",
        source_conversation=source,
        correction_id="corr-123",
        trace_id="trace-123",
        correction_text="Actually the cafe was Cafe Coffee and Waffle Stop.",
        correction_type="title",
        candidate_loader=lambda uid, source_conversation, limit: [stale],
        create_proposal=lambda **kwargs: {"created": True, "proposal": {"proposal_id": "should-not-create"}},
        min_confidence=0.1,
    )

    assert run.proposal_count == 0
    assert run.decisions[0].action == "skip"
    assert "active_summary_version_incompatible" in run.decisions[0].skipped_reasons
