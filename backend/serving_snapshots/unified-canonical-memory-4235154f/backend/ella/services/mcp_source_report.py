"""Pure, closed source-report validation; no I/O or execution authority.

Offsets are half-open Unicode code points in the exact stored segment text,
joined in order with U+0020. Evidence containment is not semantic completeness.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

OFFSET_UNIT = "unicode_code_point"
JOIN_RULE = "segments_in_stored_order_joined_with_single_U0020_no_other_transformation"
MAX_REPORT_BYTES = 65536


class SourceReportError(ValueError):
    """Content-free error codes only."""


def require(value, code):
    if not value:
        raise SourceReportError(code)


def text(value, maximum=4000):
    require(type(value) is str and bool(value.strip()) and len(value) <= maximum,
            "source_report_string_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise SourceReportError("source_report_utf8_invalid") from None


def closed(value, keys):
    require(type(value) is dict and set(value) == set(keys), "source_report_fields_invalid")


def report_digest(report):
    try:
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SourceReportError("source_report_encoding_invalid") from None
    require(len(encoded) <= MAX_REPORT_BYTES, "source_report_too_large")
    return hashlib.sha256(encoded).hexdigest()


def transcript_projection(conversation):
    segments = conversation.get("transcript_segments")
    require(type(segments) is list and all(type(s) is dict and type(s.get("text")) is str
                                         for s in segments), "source_transcript_invalid")
    exact = " ".join(s["text"] for s in segments)
    try:
        encoded = exact.encode("utf-8")
    except UnicodeError:
        raise SourceReportError("source_transcript_utf8_invalid") from None
    return {"text": exact, "sha256": hashlib.sha256(encoded).hexdigest(),
            "quote_offset_unit": OFFSET_UNIT, "join_rule": JOIN_RULE}


def validate_report(report, conversation_id, conversation):
    # Bound serialization before traversing untrusted recursive values.
    report_digest(report)
    closed(report, ("source", "quote_offset_unit", "commitments", "unknowns"))
    closed(report["source"], ("id", "sha256"))
    require(report["source"]["id"] == conversation_id, "source_report_identity_mismatch")
    source = transcript_projection(conversation)
    require(bool(source["text"].strip()), "source_transcript_empty")
    require(type(report["source"]["sha256"]) is str and
            re.fullmatch(r"[0-9a-f]{64}", report["source"]["sha256"]) is not None and
            report["source"]["sha256"] == source["sha256"], "source_report_hash_mismatch")
    require(report["quote_offset_unit"] == OFFSET_UNIT, "source_report_offset_unit_invalid")
    rows, unknowns = report["commitments"], report["unknowns"]
    require(type(rows) is list and len(rows) <= 50 and type(unknowns) is list and
            len(unknowns) <= 20, "source_report_list_invalid")
    for value in unknowns:
        text(value)
    by_id, seen = {}, set()
    for row in rows:
        closed(row, ("id", "kind", "speaker", "actor", "action", "timing", "quote",
                     "assent_to", "source_report", "execution_status"))
        text(row["id"], 200)
        require(row["id"] not in by_id, "source_report_duplicate_id")
        by_id[row["id"]] = row
        require(type(row["kind"]) is str and row["kind"] in
                ("promise", "request", "proposal", "agreement"), "source_report_kind_invalid")
        text(row["action"])
        for key in ("speaker", "actor"):
            if row[key] is not None:
                text(row[key], 200)
        quote = row["quote"]
        closed(quote, ("start", "end", "text"))
        require(type(quote["start"]) is int and type(quote["end"]) is int and
                0 <= quote["start"] < quote["end"] <= len(source["text"]),
                "source_report_span_invalid")
        text(quote["text"], MAX_REPORT_BYTES)
        require(quote["text"] == source["text"][quote["start"]:quote["end"]],
                "source_report_quote_mismatch")
        key = (quote["start"], quote["end"], row["action"], row["kind"])
        require(key not in seen, "source_report_duplicate_obligation")
        seen.add(key)
        if row["timing"] is not None:
            text(row["timing"])
            require(row["timing"] in quote["text"], "source_report_timing_mismatch")
        assertion = row["source_report"]
        closed(assertion, ("state", "quote"))
        require(type(assertion["state"]) is str and assertion["state"] in
                ("not_reported", "reported_complete", "reported_incomplete"),
                "source_report_completion_invalid")
        if assertion["state"] == "not_reported":
            require(assertion["quote"] is None, "source_report_assertion_invalid")
        else:
            text(assertion["quote"])
            require(assertion["quote"] in quote["text"], "source_report_assertion_invalid")
        require(row["execution_status"] == "unverified", "source_report_execution_forbidden")
        if row["kind"] != "agreement":
            require(row["assent_to"] is None, "source_report_assent_invalid")
    for row in rows:
        if row["kind"] == "agreement":
            target = row["assent_to"]
            require(type(target) is str and target in by_id and target != row["id"] and
                    by_id[target]["kind"] in ("proposal", "request"),
                    "source_report_assent_invalid")
    result = copy.deepcopy(report)
    for row in result["commitments"]:
        # Source reporting never sets a trusted execution state.
        row["execution_status"] = "unverified"
    return result


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


_STRING = {"type": "string", "minLength": 1, "maxLength": 4000}
ENTRY_SCHEMA = _object({
    "id": {"type": "string", "minLength": 1, "maxLength": 200},
    "kind": {"type": "string", "enum": ["promise", "request", "proposal", "agreement"]},
    "speaker": _nullable({"type": "string", "minLength": 1, "maxLength": 200}),
    "actor": _nullable({"type": "string", "minLength": 1, "maxLength": 200}),
    "action": _STRING, "timing": _nullable(_STRING),
    "quote": _object({"start": {"type": "integer", "minimum": 0},
                      "end": {"type": "integer", "minimum": 1},
                      "text": {"type": "string", "minLength": 1, "maxLength": MAX_REPORT_BYTES}}),
    "assent_to": _nullable({"type": "string", "minLength": 1, "maxLength": 200}),
    "source_report": _object({"state": {"type": "string", "enum": ["not_reported", "reported_complete", "reported_incomplete"]},
                              "quote": _nullable(_STRING)}),
    "execution_status": {"type": "string", "const": "unverified"},
})
REPORT_SCHEMA = _object({
    "source": _object({"id": {"type": "string", "minLength": 1, "maxLength": 256},
                       "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}),
    "quote_offset_unit": {"type": "string", "const": OFFSET_UNIT},
    "commitments": {"type": "array", "maxItems": 50, "items": ENTRY_SCHEMA},
    "unknowns": {"type": "array", "maxItems": 20, "items": _STRING},
})
