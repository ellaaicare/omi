"""Explicit internal APPLY of one server-issued retained contextual proposal.

No public route, provider call, queue or automatic APPLY. New mutation is guarded
inside the existing CAS transaction. Exact accepted CAS replay delegates to the
existing owned reconciliation path; it does not regenerate or admit new content.
Cancellation closes new admission, not already committed remote work. The CAS
task remains owned to finish reconciliation or report its existing unknown state.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import asdict, dataclass
from typing import Any

from ella.services import semantic_publication as admission

MAX_OWNED_APPLIES = 8
_owned_lock = threading.Lock()
_owned_applies = {}


@dataclass
class _OwnedApply:
    task: Any


@dataclass(frozen=True)
class _ApplyDependencies:
    resolver: Any
    identity: Any
    revalidator: Any
    consent: Any
    hydrate: Any
    writer: Any


def _reject(code):
    raise admission.SemanticPublicationRejectedError(code)


def _proposal_contract(proposal):
    """Closed wire shapes: historical v1, or explicitly versioned v2.

    Candidate schema 2 is the contextual summary shape; semantic contract 2
    selects the newer generator/verifier rules. Neither substitutes for the
    other, and the current policy/receipt binding must authorize both.
    """
    fields = {"publication_receipt_id", "policy", "policy_revision",
              "candidate_schema_version", "summary", "candidate_sha256"}
    if not isinstance(proposal, dict):
        _reject("semantic_apply_proposal_invalid")
    if set(proposal) == fields:
        return 1
    if set(proposal) == fields | {"semantic_contract_version"}:
        version = proposal["semantic_contract_version"]
        if type(version) is int and version == 2:
            return version
        _reject("semantic_apply_contract_invalid")
    _reject("semantic_apply_proposal_invalid")


class RetainedSemanticApply:
    def __init__(self, *, uid, conversation_id, repository, read_conversation, dependencies):
        if not isinstance(uid, str) or not uid or not isinstance(conversation_id, str) or not conversation_id:
            _reject("semantic_apply_owner_required")
        self.uid, self.cid, self.repository = uid, conversation_id, repository
        self.read, self._deps = read_conversation, dependencies

    def _owned(self, conversation):
        if (
            not isinstance(conversation, dict)
            or conversation.get("id") != self.cid
            or conversation.get("uid", self.uid) != self.uid
        ):
            _reject("semantic_apply_owned_source_unavailable")
        return conversation

    async def __call__(self, proposal):
        key = (self.uid, self.cid)
        caller = asyncio.current_task()
        owned = _OwnedApply(caller)
        with _owned_lock:
            if key in _owned_applies:
                _reject("semantic_apply_owned_operation_active")
            if len(_owned_applies) >= MAX_OWNED_APPLIES:
                _reject("semantic_apply_owned_capacity")
            _owned_applies[key] = owned
        try:
            return await self._apply(proposal, key, owned)
        finally:
            # Before writer handoff the caller owns cleanup. After handoff only
            # the exact retained CAS task's completion callback may release it.
            with _owned_lock:
                if _owned_applies.get(key) is owned and owned.task is caller:
                    del _owned_applies[key]

    async def _apply(self, proposal, owned_key, owned):
        proposal = copy.deepcopy(proposal)
        contract_version = _proposal_contract(proposal)
        if (
            proposal.get("policy") != "ella-semantic-summary-v1"
            or type(proposal.get("candidate_schema_version")) is not int
            or proposal["candidate_schema_version"] != 2
        ):
            _reject("semantic_apply_proposal_invalid")
        candidate = proposal["summary"]
        if (
            not isinstance(candidate, dict)
            or set(candidate) != set(admission.SUMMARY_FIELDS)
            or any(not isinstance(value, str) for value in candidate.values())
            or admission.candidate_sha256(candidate) != proposal["candidate_sha256"]
        ):
            _reject("semantic_apply_candidate_changed")
        conversation = self._owned(await asyncio.to_thread(self.read, self.uid, self.cid))
        receipts = conversation.get(admission.RECEIPTS_FIELD) or {}
        receipt_id = proposal["publication_receipt_id"]
        if not isinstance(receipt_id, str) or not receipt_id or not isinstance(receipts, dict):
            _reject("semantic_apply_receipt_required")
        receipt = receipts.get(receipt_id)
        if (
            not isinstance(receipt, dict)
            or receipt.get("contract") != admission.CONTRACT
            or receipt.get("candidate_sha256") != proposal["candidate_sha256"]
        ):
            _reject("semantic_apply_receipt_required")
        try:
            binding = admission.PublicationBinding(**receipt["binding"])
        except (KeyError, TypeError) as exc:
            raise admission.SemanticPublicationRejectedError("semantic_apply_receipt_invalid") from exc
        if (
            binding.uid != self.uid
            or binding.conversation_id != self.cid
            or proposal["policy_revision"] != binding.policy_revision
        ):
            _reject("semantic_apply_receipt_owner_changed")
        payload = {
                "domain": "ella-retained-semantic-apply-v1",
                "binding": asdict(binding),
                "receipt_id": receipt_id,
                "candidate": candidate,
                "schema_version": 2,
            }
        # Preserve exact historical v1 operation hashes and accepted replay.
        # New v2 operations explicitly bind their verifier contract as well as
        # the policy revision (which already binds this version).
        if contract_version == 2:
            payload["semantic_contract_version"] = contract_version
        payload_sha = admission.digest(payload)
        token = "semantic-apply-" + payload_sha
        accepted = conversation.get("summary_writeback_receipt") or {}
        replay = (
            isinstance(accepted, dict)
            and accepted.get("token") == token
            and accepted.get("payload_sha256") == payload_sha
            and accepted.get("source_sha256") == binding.source_sha256
            and accepted.get("source_version") == binding.source_version
            and accepted.get("semantic_publication_receipt_id") == receipt_id
            and accepted.get("status") in {"pending_reconciliation", "completed"}
        )
        closed = threading.Event()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        runtime = identity = None

        def admission_open():
            if closed.is_set():
                _reject("semantic_apply_admission_cancelled")

        async def revalidate():
            admission_open()
            if await asyncio.to_thread(self._deps.consent, self.uid) != self.uid:
                _reject("semantic_apply_consent_owner_changed")
            current = await self._deps.revalidator(identity, repository=self.repository)
            if (
                current is None
                or self._deps.identity(current) != identity
                or current.uid != self.uid
                or current.provider != "hermes"
                or current.status != "active"
            ):
                _reject("semantic_apply_runtime_changed")
            admission_open()

        def context(owner):
            admission_open()
            if owner != self.uid or self._deps.consent(owner) != owner:
                _reject("semantic_apply_consent_owner_changed")
            snapshot = self._deps.hydrate(runtime, uid=owner, runtime_identity=identity)
            admission_open()
            return snapshot

        def validate_snapshot(current):
            admission_open()
            self._owned(current)
            if (
                admission.candidate_schema(current) != 2
                or admission.semantic_contract_version(current) != contract_version
                or admission.current_binding(current, uid=self.uid, conversation_id=self.cid) != binding
            ):
                _reject("semantic_apply_authority_changed")
            admission.require_publication(
                current,
                uid=self.uid,
                conversation_id=self.cid,
                candidate=candidate,
                receipt_id=receipt_id,
                read_companion_context=context,
            )
            admission_open()

        def transaction_guard(current):
            admission_open()
            if threading.get_ident() == loop_thread:
                _reject("semantic_apply_transaction_thread_required")
            future = asyncio.run_coroutine_threadsafe(revalidate(), loop)
            try:
                future.result(timeout=15)
            except BaseException as exc:
                future.cancel()
                if isinstance(exc, admission.SemanticPublicationRejectedError):
                    raise
                raise admission.SemanticPublicationRejectedError("semantic_apply_authority_unavailable") from exc
            validate_snapshot(current)

        def replay_guard(current):
            # If the accepted receipt vanishes/races before the CAS snapshot,
            # replay must never fall through into a NEW mutation, even if the
            # owner policy was subsequently disabled.
            _reject("semantic_apply_replay_cannot_admit_new")

        try:
            if not replay:
                if await asyncio.to_thread(self._deps.consent, self.uid) != self.uid:
                    _reject("semantic_apply_consent_owner_changed")
                runtime = await self._deps.resolver(
                    self.uid, repository=self.repository, target_mode="hermes-cloud-transcript"
                )
                if (
                    runtime is None
                    or runtime.uid != self.uid
                    or runtime.provider != "hermes"
                    or runtime.status != "active"
                ):
                    _reject("semantic_apply_retained_target_required")
                identity = self._deps.identity(runtime)
                await revalidate()
                await asyncio.to_thread(validate_snapshot, conversation)
            kwargs = dict(
                uid=self.uid,
                conversation_id=self.cid,
                expected_canonical_source_sha256=binding.source_sha256,
                source_version=binding.source_version,
                operation_token=token,
                payload_sha256=payload_sha,
                **candidate,
                summary_source="hermes",
                summary_kind="hermes_enriched",
                based_on_version_id=binding.active_summary_version_id,
                set_active=True,
                trace_id=token,
                preserve_generated_results=True,
                semantic_publication_receipt_id=receipt_id,
                semantic_publication_original_candidate=copy.deepcopy(candidate),
                semantic_companion_context_reader=context if not replay else None,
                semantic_publication_authority_guard=transaction_guard if not replay else replay_guard,
            )
            task = asyncio.create_task(self._deps.writer(**kwargs))
            with _owned_lock:
                owned.task = task

            def completed(done):
                with _owned_lock:
                    if _owned_applies.get(owned_key) is owned and owned.task is done:
                        del _owned_applies[owned_key]
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(completed)
            # Do not cancel a possibly committed CAS/reconciliation. Closing the
            # event instead prevents its worker from admitting a NEW mutation.
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                closed.set()
                raise
        except asyncio.CancelledError:
            closed.set()
            raise


def create_retained_apply_bridge(*, uid, conversation_id, repository, read_conversation,host_read_budget=None):
    """Internal factory; callers cannot supply authority readers in callback JSON."""
    from ella.services.ai_consent import assert_current_ai_consent
    from ella.services.semantic_runtime_authority import (
        resolve_semantic_runtime as resolve_isolated_runtime,
        semantic_runtime_authority_identity as runtime_authority_identity,
        revalidate_semantic_runtime as revalidate_runtime_authority,
    )
    from ella.services.semantic_host_context import context_reader
    from ella.services.summary_writeback import write_conversation_summary_cas

    from functools import partial
    from ella.services.semantic_host_context import HostReadBudget
    host_read_budget=host_read_budget or HostReadBudget()
    return RetainedSemanticApply(
        uid=uid,
        conversation_id=conversation_id,
        repository=repository,
        read_conversation=read_conversation,
        dependencies=_ApplyDependencies(
            partial(resolve_isolated_runtime,host_read_budget=host_read_budget),
            runtime_authority_identity,
            partial(revalidate_runtime_authority,host_read_budget=host_read_budget),
            assert_current_ai_consent,
            context_reader(conversation_id=conversation_id, read_conversation=read_conversation,read_budget=host_read_budget),
            write_conversation_summary_cas,
        ),
    )
