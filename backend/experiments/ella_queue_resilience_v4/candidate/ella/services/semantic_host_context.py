"""Existing provision-authority transport to the exact owner's workspace host.

Sync runs once outside transactions. Snapshot is read-only, single-attempt and
bounded; it replaces a local file read without fallback or cached authority.
"""

from __future__ import annotations

from ella.services import enrichment_stage_timing as _timing
from ella.services.semantic_operation_budget import remaining_timeout
from database.enrichment_graph_guard import require_current as require_queue_graph
import asyncio
import hashlib
import json
import threading
import time
from urllib.parse import quote
import httpx

from ella.services.canonical_summary_source import canonical_source_from_conversation, canonical_source_sha256
from ella.services.semantic_companion_context import CompanionContextUnavailable
from ella.services.semantic_companion_context import SOURCES, MAX_DOCUMENT_BYTES, MAX_SNAPSHOT_BYTES
from ella.utils.provision_authority import hermes_provision_authority

from ella.services.transcript_processing_hold import (
    assert_transcript_processing_active,
    require_transcript_processing_active,
)

MAX_BYTES = 196608

# Closed engineering metadata only; never body/header echo or authority.
HOST_HOLD_CODES = frozenset(
    [
        'access_denied',
        'authority_changed_during_source_read',
        'canonical_source_changed',
        'canonical_source_changed_during_read',
        'canonical_source_owner_changed',
        'companion_document_capacity',
        'companion_document_changed',
        'companion_document_invalid',
        'companion_owner_runtime_required',
        'companion_runtime_authority_required',
        'companion_snapshot_capacity',
        'companion_snapshot_changed',
        'companion_workspace_changed',
        'companion_workspace_invalid',
        'companion_workspace_unavailable',
        'companion_workspace_writable_by_others',
        'full_runtime_authority_adapter_unconfigured',
        'full_runtime_authority_required',
        'full_runtime_identity_invalid',
        'host_authority_source_unavailable',
        'host_backend_auth_missing',
        'host_backend_authority_capacity',
        'host_backend_authority_invalid',
        'host_backend_authority_rejected',
        'host_backend_authority_unavailable',
        'host_backend_nonfinite',
        'host_backend_origin_invalid',
        'host_backend_owner_or_source_changed',
        'host_backend_source_binding_invalid',
        'host_registry_invalid',
        'host_registry_owner_runtime_mismatch',
        'host_request_owner_mismatch',
        'host_request_workspace_changed',
        'host_subject_invalid',
        'host_sync_source_changed',
        'host_sync_source_pin_changed',
        'host_sync_source_unsafe',
        'host_sync_stage_changed',
        'host_sync_stage_replaced',
        'invalid_key',
        'invalid_type',
        'invalid_value',
        'journal_capacity',
        'journal_directory_replaced',
        'journal_directory_unsafe',
        'journal_duplicate_key',
        'journal_nonfinite',
        'journal_path_invalid',
        'journal_publish_busy',
        'journal_publish_lock_replaced',
        'journal_publish_lock_unavailable',
        'journal_publish_lock_unsafe',
        'journal_publish_readback_failed',
        'journal_publish_replaced',
        'journal_record_capacity',
        'journal_record_changed',
        'journal_record_invalid',
        'journal_record_name_invalid',
        'journal_record_unsafe',
        'os_error',
        'owner_runtime_changed',
        'owner_source_required',
        'post_hydration_authority_or_source_changed',
        'post_sync_authority_or_source_changed',
        'private_transport_capacity',
        'snapshot_sync_completion_invalid',
        'snapshot_sync_completion_required',
        'source_digest_required',
        'sync_child_cleanup_unverified',
        'sync_child_failed',
        'sync_completion_without_start',
        'sync_deadline_invalid',
        'sync_replay_context_changed',
        'sync_start_invalid',
        'sync_started_outcome_unknown',
        'timeout',
        'unknown',
    ]
)


def observe_host_failure(*, action, cid, category, http_status=None, hold_code='unknown'):
    """Observation must never change the original error, admission or retry rules."""
    try:
        if category not in {'http_rejected', 'transport_timeout', 'transport_error', 'local_error'}:
            return
        code = hold_code if type(hold_code) is str and hold_code in HOST_HOLD_CODES else 'unknown'
        status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        print(
            'ella_semantic_host_observation '
            + json.dumps(
                {
                    'version': 1,
                    'source_sha256': hashlib.sha256(cid.encode()).hexdigest(),
                    'action': action if action in {'sync', 'snapshot'} else 'unknown',
                    'category': category,
                    'http_status': status,
                    'hold_code': code,
                },
                sort_keys=True,
                allow_nan=False,
            ),
            flush=True,
        )
    except BaseException:
        pass


class HostReadBudget:
    """One operation's finite cross-host read allowance, shared across retries."""

    def __init__(self, *, max_calls=128, wall_seconds=480, clock=time.monotonic):
        if type(max_calls) is not int or not 1 <= max_calls <= 128 or not 0 < wall_seconds <= 480:
            raise CompanionContextUnavailable('semantic_host_budget_invalid')
        self.clock = clock
        self.deadline = clock() + wall_seconds
        self.max_calls = max_calls
        self.calls = 0
        self.lock = threading.Lock()

    def reserve(self, action):
        with self.lock:
            remaining = self.deadline - self.clock()
            if remaining <= 0 or self.calls >= self.max_calls:
                raise CompanionContextUnavailable('semantic_host_read_budget_exhausted')
            self.calls += 1
            return remaining_timeout(
                min(90 if action == 'sync' else 60 if action == 'context_snapshot' else 5, remaining)
            )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()


def unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise CompanionContextUnavailable('semantic_host_duplicate_key')
        out[key] = value
    return out


def host_context(
    *,
    runtime,
    uid,
    conversation_id,
    runtime_identity,
    read_conversation,
    action='snapshot',
    read_budget=None,
    client_factory=httpx.AsyncClient,
    authority_reader=hermes_provision_authority,
):
    with _timing.stage('semantic_host_context.host_context'):
        if action not in {'sync', 'snapshot'} or runtime.uid != uid or runtime_identity.uid != uid:
            raise CompanionContextUnavailable('semantic_host_owner_required')
        assert_transcript_processing_active(uid)
        doc = read_conversation(uid, conversation_id)
        if not isinstance(doc, dict) or doc.get('id') != conversation_id or doc.get('uid', uid) != uid:
            raise CompanionContextUnavailable('semantic_host_owned_source_required')
        source_hash = canonical_source_sha256(
            canonical_source_from_conversation(uid=uid, conversation_id=conversation_id, conversation=doc)
        )
        authority = authority_reader()
        captured = authority.snapshot()
        body = {
            'uid': uid,
            'agent_id': runtime.agent_id,
            'conversation_id': conversation_id,
            'expected_source_sha256': source_hash,
        }
        url = authority.base_url + '/workspace/' + quote(runtime.agent_id, safe='') + '/semantic-context/' + action
        # Budget is reserved before authority configuration or network I/O.
        limit = (read_budget or HostReadBudget()).reserve('context_snapshot' if action == 'snapshot' else action)
        authority_reader(expected_snapshot=captured)
        if action == 'sync':
            require_queue_graph(uid, conversation_id)

        async def exchange():
            with _timing.stage('semantic_host_context.exchange'):
                async with asyncio.timeout(limit):
                    async with client_factory(
                        timeout=httpx.Timeout(limit, connect=min(2, limit)), follow_redirects=False, trust_env=False
                    ) as client:
                        await require_transcript_processing_active(uid)
                        async with client.stream(
                            'POST',
                            url,
                            json=body,
                            headers={
                                'Authorization': 'Bearer ' + authority.token,
                                'X-Ella-Owner-Uid': uid,
                                'Accept': 'application/json',
                            },
                        ) as response:
                            if response.status_code != 200:
                                observe_host_failure(
                                    action=action,
                                    cid=conversation_id,
                                    category='http_rejected',
                                    http_status=response.status_code,
                                    hold_code=response.headers.get('X-Ella-Semantic-Hold-Code', 'unknown'),
                                )
                                raise CompanionContextUnavailable('semantic_host_rejected')
                            chunks = []
                            size = 0
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > MAX_BYTES:
                                    raise CompanionContextUnavailable('semantic_host_response_bound')
                                chunks.append(chunk)
                            return b''.join(chunks)

        try:
            # Every production call is on an existing to_thread/CAS worker boundary.
            # Reject accidental invocation on the application event loop.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise CompanionContextUnavailable('semantic_host_thread_required')
            encoded = asyncio.run(exchange())
            assert_transcript_processing_active(uid)
            value = json.loads(
                encoded,
                object_pairs_hook=unique_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(CompanionContextUnavailable('semantic_host_nonfinite')),
            )
            authority_reader(expected_snapshot=captured)
            if not isinstance(value, dict) or set(value) != {'receipt', 'context_snapshot'}:
                raise CompanionContextUnavailable('semantic_host_envelope_invalid')
            receipt = value['receipt']
            context = value['context_snapshot']
            expected_binding = {
                'uid': uid,
                'conversation_id': conversation_id,
                'source_sha256': source_hash,
                'runtime_authority_sha256': runtime_identity.digest,
            }
            binding = receipt.get('binding') if isinstance(receipt, dict) else None
            if (
                not isinstance(binding, dict)
                or set(binding) != {*expected_binding, 'registry_revision'}
                or any(binding.get(k) != v for k, v in expected_binding.items())
                or not isinstance(binding.get('registry_revision'), str)
                or len(binding['registry_revision']) != 64
                or receipt.get('status') != ('completed' if action == 'sync' else 'snapshot')
                or not isinstance(context, dict)
                or set(context) != {'owner_uid', 'revision', 'documents'}
                or context['owner_uid'] != uid
                or receipt.get('context_revision') != context['revision']
                or receipt.get('context_sha256') != digest(context)
            ):
                raise CompanionContextUnavailable('semantic_host_authority_changed')
            documents = context.get('documents')
            if (
                not isinstance(documents, dict)
                or set(documents) != set(SOURCES)
                or not isinstance(context.get('revision'), str)
                or not context['revision']
                or any(
                    not isinstance(v, str) or len(v.encode('utf-8')) > MAX_DOCUMENT_BYTES for v in documents.values()
                )
                or len(json.dumps(context, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES
            ):
                raise CompanionContextUnavailable('semantic_host_context_invalid')
            after = read_conversation(uid, conversation_id)
            if (
                not isinstance(after, dict)
                or after.get('id') != conversation_id
                or after.get('uid', uid) != uid
                or canonical_source_sha256(
                    canonical_source_from_conversation(uid=uid, conversation_id=conversation_id, conversation=after)
                )
                != source_hash
            ):
                raise CompanionContextUnavailable('semantic_host_source_changed')
            return context
        except CompanionContextUnavailable:
            raise
        except (TimeoutError, httpx.TimeoutException):
            observe_host_failure(action=action, cid=conversation_id, category='transport_timeout')
            raise CompanionContextUnavailable('semantic_host_unavailable') from None
        except httpx.HTTPError:
            observe_host_failure(action=action, cid=conversation_id, category='transport_error')
            raise CompanionContextUnavailable('semantic_host_unavailable') from None
        except Exception:
            observe_host_failure(action=action, cid=conversation_id, category='local_error')
            raise CompanionContextUnavailable('semantic_host_unavailable') from None


def context_reader(*, conversation_id, read_conversation, read_budget=None):
    budget = read_budget or HostReadBudget()

    def read(runtime, *, uid, runtime_identity):
        return host_context(
            runtime=runtime,
            uid=uid,
            conversation_id=conversation_id,
            runtime_identity=runtime_identity,
            read_conversation=read_conversation,
            read_budget=budget,
        )

    return read
