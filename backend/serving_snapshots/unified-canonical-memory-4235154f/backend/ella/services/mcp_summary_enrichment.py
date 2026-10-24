"""Opt-in, owner-scoped OMI MCP summary enrichment; no model or canonical writes.

The existing conversation transaction owns the read/CAS/receipt/version commit.
Legacy MCP writes do not enter this module. Returned hashes are opaque preconditions,
not authorization. Caller identity must come from the authenticated MCP key.
"""
from __future__ import annotations

import copy
from ella.services import mcp_synthetic_pending as pending
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum

from ella.services.summary_sanitizer import SummarySanitizationError, sanitize_summary_update
from ella.services.mcp_source_report import SourceReportError, validate_report, report_digest

CONTRACT = "omi-mcp-summary-enrichment-v1"
CONTRACT_V2 = "omi-mcp-summary-enrichment-v2"
LEDGER = "mcp_summary_enrichment_operations_v1"
ORIGINAL = "mcp_summary_enrichment_original_v1"
MAX_OPERATIONS = 64
FIELDS = ("title", "overview", "emoji", "category")
# Exclude our receipt ledger to avoid self-referential hashes. Include original
# inputs, visible presentation, version lineage and competing enrichment authority.
SNAPSHOT_FIELDS = (
    "structured", "apps_results", "plugins_results", "transcript_segments",
    "source", "created_at", "started_at", "finished_at", "status", "discarded",
    "deleted", "is_locked", "data_protection_level", "summary_versions",
    "active_summary_version_id", "enrichment_state", "summary_writeback_receipt",
    "semantic_publication_policy", "processing_retry_state",
    "processing_retry_claim_token", "canonical_summary_publication_sequence",
    "canonical_summary_publication_sha256", ORIGINAL,
)


class EnrichmentError(ValueError):
    """Closed error code only; never echo summary text or credentials."""


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise EnrichmentError("unsupported_source_value")


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False, default=_json_default).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EnrichmentError("unsupported_source_value") from error
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_hash(uid, conversation_id, conversation):
    return _digest({"contract": CONTRACT, "uid": uid, "conversation_id": conversation_id,
                    "source": {k: conversation[k] for k in SNAPSHOT_FIELDS if k in conversation}})


def _eligible(conversation):
    if conversation.get("status") != "completed" or any(
            conversation.get(k) for k in ("discarded", "deleted", "is_locked")):
        raise EnrichmentError("conversation_not_eligible")
    policy = conversation.get("semantic_publication_policy")
    if "semantic_publication_policy" in conversation and (
            not isinstance(policy, dict) or policy.get("enabled") is not False):
        raise EnrichmentError("semantic_receipt_required")
    # Canonical publication has its own stronger reconciliation path. This tool
    # must not invalidate its receipt or pretend to publish canonical memory.
    if conversation.get("summary_writeback_receipt"):
        raise EnrichmentError("canonical_writeback_owned")
    state = conversation.get("enrichment_state")
    if state is not None and (not isinstance(state, dict) or state.get("pending")):
        raise EnrichmentError("enrichment_pending")
    structured = conversation.get("structured")
    if not isinstance(structured, dict) or not isinstance(structured.get("overview"), str):
        raise EnrichmentError("summary_not_usable")
    versions = conversation.get("summary_versions", [])
    active_id = conversation.get("active_summary_version_id")
    if not isinstance(versions, list) or any(not isinstance(v, dict) for v in versions):
        raise EnrichmentError("summary_version_malformed")
    if not versions and active_id is None:
        return
    ids = [v.get("id") for v in versions]
    if (not isinstance(active_id, str) or not active_id or
            any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids)):
        raise EnrichmentError("summary_version_malformed")
    active = [v for v in versions if v.get("is_active") is True]
    if len(active) != 1 or active[0].get("id") != active_id:
        raise EnrichmentError("summary_version_malformed")
    if any(active[0].get(k) != structured.get(k) for k in FIELDS):
        raise EnrichmentError("summary_version_mismatch")


def preconditions(uid, conversation_id, conversation, *, contract=CONTRACT):
    _eligible(conversation)
    if contract not in (CONTRACT, CONTRACT_V2):
        raise EnrichmentError("invalid_enrichment_contract")
    return {"contract": contract, "expected_snapshot_sha256": _snapshot_hash(uid, conversation_id, conversation),
            "expected_active_summary_version_id": conversation.get("active_summary_version_id")}


def _request(arguments, category_validator):
    is_v2 = (isinstance(arguments, dict) and isinstance(arguments.get("enrichment"), dict)
             and arguments["enrichment"].get("contract") == CONTRACT_V2)
    allowed = {"conversation_id", "enrichment", *FIELDS} | ({"source_report"} if is_v2 else set())
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise EnrichmentError("invalid_enrichment_request")
    cid = arguments.get("conversation_id")
    if not isinstance(cid, str) or not cid or len(cid) > 256 or "/" in cid:
        raise EnrichmentError("invalid_conversation_id")
    request = arguments.get("enrichment")
    keys = {"contract", "expected_snapshot_sha256", "expected_active_summary_version_id", "operation_id", "producer"}
    if not isinstance(request, dict) or set(request) != keys or request.get("contract") not in (CONTRACT, CONTRACT_V2):
        raise EnrichmentError("invalid_enrichment_contract")
    if not isinstance(request["expected_snapshot_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["expected_snapshot_sha256"]):
        raise EnrichmentError("invalid_snapshot_hash")
    version = request["expected_active_summary_version_id"]
    if version is not None and (not isinstance(version, str) or not version or len(version) > 256):
        raise EnrichmentError("invalid_expected_version")
    try:
        operation_id = request["operation_id"]
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise EnrichmentError("invalid_operation_id") from error
    if not isinstance(request["producer"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", request["producer"]):
        raise EnrichmentError("invalid_producer_label")
    if is_v2 and (not {"title", "overview", "source_report"} <= set(arguments)):
        raise EnrichmentError("invalid_enrichment_request")
    if is_v2:
        try:
            report_digest(arguments["source_report"])
        except SourceReportError as error:
            raise EnrichmentError(str(error)) from error
    changes = {k: arguments[k] for k in FIELDS if k in arguments}
    if not changes or any(not isinstance(v, str) or not v.strip() or len(v) > 32000 for v in changes.values()):
        raise EnrichmentError("invalid_summary_fields")
    try:
        sanitized = sanitize_summary_update(**changes)
        if sanitized.category is not None:
            category_validator(sanitized.category)
    except (SummarySanitizationError, ValueError) as error:
        raise EnrichmentError("unsafe_summary") from error
    cleaned = {k: getattr(sanitized, k) for k in changes}
    return cid, request, cleaned, list(sanitized.warnings), _digest(arguments)


def validate_pending_source(conversation):
    from ella.services.mcp_source_report import transcript_projection
    try:
        return transcript_projection(conversation)['sha256']
    except SourceReportError as error:
        raise EnrichmentError(str(error)) from error


def enrich_summary(database, uid, arguments, category_validator, *, _thin_authority=None):
    arguments = copy.deepcopy(arguments)
    cid, request, cleaned, warnings, payload_hash = _request(arguments, category_validator)
    is_v2 = request["contract"] == CONTRACT_V2
    now = datetime.now(timezone.utc)
    operation_id = request["operation_id"]

    def build(conversation):
        thin = conversation.get('thin_cloudbot_source_binding')
        if thin is not None:
            if (_thin_authority is None or _thin_authority.uid != uid
                    or thin.get('authority') != _thin_authority.witness
                    or thin.get('consent_scope') != _thin_authority.scope
                    or not getattr(database, 'authority', None) is _thin_authority):
                raise EnrichmentError('thin_exact_target_authority_required')
        elif _thin_authority is not None:
            raise EnrichmentError('thin_main_source_forbidden')
        # Identity is a confirmation fence as well as a fresh-write fence.
        # It must precede ledger replay; identity fields are not snapshot inputs.
        if is_v2:
            if any(k in conversation and conversation[k] != uid for k in ("uid", "user_id")) or (
                    "id" in conversation and conversation["id"] != cid):
                raise EnrichmentError("source_report_identity_mismatch")
        marked = pending.FIELD in conversation
        if marked:
            try:
                pending_mode = pending.admit_save(conversation[pending.FIELD], uid, cid, arguments,
                    datetime.now(timezone.utc), live_snapshot=_snapshot_hash(uid, cid, conversation),
                    live_version=conversation.get('active_summary_version_id'),
                    source_sha256=validate_pending_source(conversation))
            except pending.BindingError as error:
                raise EnrichmentError(str(error)) from error
        ledger = conversation.get(LEDGER, {})
        if not isinstance(ledger, dict):
            raise EnrichmentError("enrichment_ledger_malformed")
        previous = ledger.get(operation_id)
        if previous is not None:
            if marked and (pending_mode != 'ledger_confirmation_only' or
                    previous.get('result', {}).get('summary_version_id') != conversation[pending.FIELD]['saved_version_id'] or
                    previous.get('result', {}).get('source_report_sha256') != conversation[pending.FIELD]['saved_report_sha256'] or
                    previous.get('result', {}).get('synthetic_run_id') != conversation[pending.FIELD]['run_id']):
                raise EnrichmentError('pending_ledger_confirmation_mismatch')
            if not isinstance(previous, dict) or previous.get("request_sha256") != payload_hash:
                raise EnrichmentError("operation_id_reused")
            _eligible(conversation)
            if previous.get("post_snapshot_sha256") != _snapshot_hash(uid, cid, conversation):
                raise EnrichmentError("enrichment_operation_superseded")
            return {}, {**copy.deepcopy(previous["result"]), "idempotent_replay": True}
        if marked and pending_mode != 'new_commit_allowed':
            raise EnrichmentError('pending_completed_ledger_missing')
        _eligible(conversation)
        if len(ledger) >= MAX_OPERATIONS:
            raise EnrichmentError("enrichment_history_full")
        if (conversation.get("active_summary_version_id") != request["expected_active_summary_version_id"] or
                _snapshot_hash(uid, cid, conversation) != request["expected_snapshot_sha256"]):
            raise EnrichmentError("enrichment_source_changed")
        source_report = None
        if is_v2:
            try:
                source_report = validate_report(arguments["source_report"], cid, conversation)
            except SourceReportError as error:
                raise EnrichmentError(str(error)) from error
        structured = copy.deepcopy(conversation["structured"])
        structured.update(cleaned)
        versions = database.build_summary_version_update(
            conversation, next_structured=structured, source="mcp", kind="mcp_enriched",
            based_on_version_id=request["expected_active_summary_version_id"], activate=True,
            **({"source_report": source_report} if is_v2 else {}))
        update = {"structured." + k: v for k, v in cleaned.items()}
        update.update({k: versions[k] for k in ("summary_versions", "active_summary_version_id")})
        original = conversation.get(ORIGINAL)
        if original is None:
            original = {"contract": CONTRACT, "snapshot_sha256": request["expected_snapshot_sha256"],
                        "structured": copy.deepcopy(conversation["structured"]),
                        "active_summary_version_id": conversation.get("active_summary_version_id"),
                        "preserved_at": now, "via": "omi_mcp"}
            update[ORIGINAL] = original
        elif not isinstance(original, dict) or original.get("contract") != CONTRACT:
            raise EnrichmentError("enrichment_original_malformed")
        post = {**conversation, "structured": structured, "summary_versions": update["summary_versions"],
                "active_summary_version_id": update["active_summary_version_id"], ORIGINAL: original}
        result = {"success": True, "contract": request["contract"], "conversation_id": cid, "operation_id": operation_id,
                  "summary_version_id": update["active_summary_version_id"],
                  "source_snapshot_sha256": request["expected_snapshot_sha256"],
                  "post_snapshot_sha256": _snapshot_hash(uid, cid, post), "preserved_generated_results": True,
                  "canonical_write": False, "idempotent_replay": False, "sanitizer_warnings": warnings}
        if is_v2:
            result.update(source_report_sha256=report_digest(source_report),
                          source_transcript_sha256=source_report["source"]["sha256"])
        if marked:
            try:
                pending.admit_save(conversation[pending.FIELD], uid, cid, arguments,
                    datetime.now(timezone.utc), live_snapshot=_snapshot_hash(uid, cid, conversation),
                    live_version=conversation.get('active_summary_version_id'),
                    source_sha256=source_report['source']['sha256'])
                update[pending.FIELD] = pending.completed(conversation[pending.FIELD], result)
                result['synthetic_run_id'] = conversation[pending.FIELD]['run_id']
                result['pending_item_completed'] = True
            except pending.BindingError as error:
                raise EnrichmentError(str(error)) from error
        entry = {"request_sha256": payload_hash, "post_snapshot_sha256": result["post_snapshot_sha256"],
                 "created_at": now, "producer_claim": request["producer"], "via": "omi_mcp",
                 "based_on_version_id": request["expected_active_summary_version_id"], "result": result}
        update[LEDGER] = {**copy.deepcopy(ledger), operation_id: entry}
        return update, result

    committed = database.update_conversation_with_builder(uid, cid, build)
    if committed is None:
        raise EnrichmentError("conversation_not_found")
    return committed["result"]
