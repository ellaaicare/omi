"""Read-only current-summary projection. No history, writes, or reply interception."""

import copy
import math
from ella.services.mcp_source_report import SourceReportError, validate_report, report_digest, transcript_projection
from datetime import datetime, timezone

CONTRACT = "ella-current-conversation-v1"
CONTRACT_V2 = "ella-current-conversation-v2"
SUMMARY_FIELDS = ("title", "overview", "emoji", "category")
TOOL = {
    "name": "get_current_conversation_by_id",
    "description": (
        "Read one account-owned conversation's CURRENT active summary and source transcript. "
        "Excludes historical summaries and internal processing fields. Capture, summary, and "
        "retrieval times are distinct. Action/reminder completion is UNCONFIRMED; narrative "
        "wording is not proof of execution. Exact ID only; not a global latest-memory query."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"conversation_id": {"type": "string", "minLength": 1},
                       "projection_contract": {"type": "string", "enum": [CONTRACT, CONTRACT_V2]}},
        "required": ["conversation_id"],
        "additionalProperties": False,
    },
}


class CurrentConversationError(ValueError):
    """Closed error codes; never include content or account identifiers."""


def _time(value):
    if value is None:
        return None
    try:
        parsed = (value if isinstance(value, datetime)
                  else datetime.fromisoformat(value.replace("Z", "+00:00")))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise CurrentConversationError("current_timestamp_invalid") from None


def read_current_conversation(database, user_id, arguments, *, now=None):
    """Use the same authenticated owner getter as the existing exact-ID tool."""
    if (not isinstance(arguments, dict) or set(arguments) not in ({"conversation_id"}, {"conversation_id", "projection_contract"})
            or arguments.get("projection_contract", CONTRACT) not in (CONTRACT, CONTRACT_V2)
            or not isinstance(arguments["conversation_id"], str)
            or not arguments["conversation_id"].strip()):
        raise CurrentConversationError("current_arguments_invalid")
    if not isinstance(user_id, str) or not user_id:
        raise CurrentConversationError("current_owner_required")
    cid = arguments["conversation_id"]
    source = database.get_conversation(user_id, cid)
    if source is not None and source.get('thin_cloudbot_source_binding') is not None:
        authority = getattr(database, '_thin_authority', None)
        if (authority is None or authority.uid != user_id
                or source['thin_cloudbot_source_binding'].get('authority') != authority.witness):
            raise CurrentConversationError('thin_exact_target_authority_required')
    if source is None:
        raise CurrentConversationError("current_conversation_not_found")
    if not isinstance(source, dict) or source.get("id") != cid:
        raise CurrentConversationError("current_source_identity_invalid")
    # Some stores include an owner field; never return a contradictory document.
    if any(k in source and source[k] != user_id for k in ("uid", "user_id")):
        raise CurrentConversationError("current_source_identity_invalid")
    if source.get("is_locked"):
        raise CurrentConversationError("current_conversation_locked")
    if (source.get("status") != "completed" or source.get("discarded")
            or source.get("deleted")):
        raise CurrentConversationError("current_conversation_unavailable")

    active_id = source.get("active_summary_version_id")
    versions = source.get("summary_versions")
    if (not isinstance(active_id, str) or not active_id
            or not isinstance(versions, list) or not versions
            or any(not isinstance(v, dict) for v in versions)):
        raise CurrentConversationError("current_active_version_invalid")
    ids = [v.get("id") for v in versions]
    if (any(not isinstance(v, str) or not v for v in ids)
            or len(ids) != len(set(ids))
            or any(type(v.get("is_active")) is not bool for v in versions)):
        raise CurrentConversationError("current_active_version_invalid")
    active = [v for v in versions if v["is_active"]]
    if len(active) != 1 or active[0]["id"] != active_id:
        raise CurrentConversationError("current_active_version_invalid")
    active = active[0]
    structured = source.get("structured")
    if (not isinstance(structured, dict)
            or any(not isinstance(active.get(k), str) for k in SUMMARY_FIELDS)
            or any(active.get(k) != structured.get(k) for k in SUMMARY_FIELDS)):
        raise CurrentConversationError("current_active_content_mismatch")

    segments = source.get("transcript_segments")
    if not isinstance(segments, list):
        raise CurrentConversationError("current_transcript_invalid")
    transcript = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise CurrentConversationError("current_transcript_invalid")
        projected = {"text": segment["text"]}
        for key in ("id", "speaker"):
            if key in segment:
                if segment[key] is not None and not isinstance(segment[key], str):
                    raise CurrentConversationError("current_transcript_invalid")
                projected[key] = segment[key]
        if "speaker_id" in segment:
            value = segment["speaker_id"]
            # TranscriptSegment serializes Optional[int]; bool is not an ID.
            if value is not None and type(value) is not int:
                raise CurrentConversationError("current_transcript_invalid")
            projected["speaker_id"] = value
        for key in ("start", "end"):
            value = segment.get(key)
            if value is not None:
                # Upstream offsets are signed: provider timestamps are shifted by
                # audio-stream start minus stored conversation start. Overlap and
                # datetime precision can place a segment before that origin.
                # Preserve the exact value; reject bool, nonnumeric, nonfinite or
                # out-of-float-range values. Pair ordering is checked below.
                try:
                    valid_offset = type(value) in (int, float) and math.isfinite(value)
                except OverflowError:
                    valid_offset = False
                if not valid_offset:
                    raise CurrentConversationError("current_transcript_invalid")
                projected[key + "_offset_seconds"] = value
        if ("start" in segment and "end" in segment
                and segment["start"] is not None and segment["end"] is not None
                and segment["start"] > segment["end"]):
            raise CurrentConversationError("current_transcript_invalid")
        transcript.append(projected)

    times = {
        "capture_started_at_utc": _time(source.get("started_at")),
        "capture_ended_at_utc": _time(source.get("finished_at")),
        "summary_created_at_utc": _time(active.get("created_at")),
        # Do not substitute the document's update time or the retrieval time.
        "summary_updated_at_utc": _time(active.get("updated_at")),
        "retrieved_at_utc": _time(now if now is not None else datetime.now(timezone.utc)),
    }
    if (times["capture_started_at_utc"] and times["capture_ended_at_utc"]
            and datetime.fromisoformat(times["capture_started_at_utc"].replace("Z", "+00:00"))
            > datetime.fromisoformat(times["capture_ended_at_utc"].replace("Z", "+00:00"))):
        raise CurrentConversationError("current_capture_interval_invalid")
    if not times["summary_created_at_utc"]:
        raise CurrentConversationError("current_active_version_time_missing")
    result = {
        "contract": CONTRACT,
        "source_identity": {"conversation_id": cid, "account_scope": "authenticated_owner"},
        "active_summary_version_id": active_id,
        "current_summary": {k: active[k] for k in SUMMARY_FIELDS},
        "timestamps": times,
        "transcript_provenance": {
            "conversation_id": cid,
            "kind": "source_transcript_not_action_execution_evidence",
            "segment_time_basis": "seconds_from_capture_start",
            "segments": copy.deepcopy(transcript),
        },
        "action_confirmation": {
            "status": "unconfirmed",
            "execution_or_delivery_verified": False,
            "evidence": "No action execution, reminder delivery, or calendar creation receipt is provided by this tool.",
            "interpretation": "Tasks or reminders mentioned in summary/transcript are source statements; do not claim they were completed or scheduled.",
        },
        "freshness": {
            "basis": "current_stored_snapshot_at_read",
            "global_latest_memory": False,
            "historical_summary_contents_included": False,
            "retrieval_time_is_not_capture_time": True,
            "pre_reply_retrieval_enforced": False,
        },
    }

    if arguments.get("projection_contract") == CONTRACT_V2:
        try:
            projection = transcript_projection(source)
            stored_report = active.get("source_report")
            if stored_report is None:
                if active.get("source_report_sha256") is not None:
                    raise CurrentConversationError("current_source_report_invalid")
                report = None
                digest = None
            else:
                report = validate_report(stored_report, cid, source)
                digest = report_digest(report)
                if active.get("source_report_sha256") != digest:
                    raise CurrentConversationError("current_source_report_invalid")
        except SourceReportError as error:
            raise CurrentConversationError("current_source_report_invalid") from error
        result.update(contract=CONTRACT_V2, source_transcript=projection,
                      current_source_report={"state": "recorded" if report is not None else "not_recorded",
                                             "summary_version_id": active_id,
                                             "source_report_sha256": digest, "report": report})
    from ella.services import mcp_synthetic_pending as pending
    if arguments.get('projection_contract') == CONTRACT_V2 and pending.FIELD in source:
        try:
            result['pending_synthetic_run'] = pending.project(source[pending.FIELD], user_id, cid, now or datetime.now(timezone.utc))
        except pending.BindingError as error:
            raise CurrentConversationError(str(error)) from error
    return result
