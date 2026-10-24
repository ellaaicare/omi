"""One bounded append request through the existing exact provision authority."""

from __future__ import annotations
import asyncio, json
from database.enrichment_graph_guard import require_current as require_queue_graph
from urllib.parse import quote
import httpx
from ella.utils.provision_authority import hermes_provision_authority
from ella.services.semantic_host_context import unique_pairs
from ella.services.semantic_learning_v3 import digest, bytes_digest, hold


async def apply_learning_on_host(
    *, runtime, grant, host_read_budget, client_factory=httpx.AsyncClient, authority_reader=hermes_provision_authority
):
    if grant['binding']['uid'] != runtime.uid:
        hold('learning_host_owner_required')
    seconds = host_read_budget.reserve('sync')
    authority = authority_reader()
    snapshot = authority.snapshot()
    authority_reader(expected_snapshot=snapshot)
    url = authority.base_url + '/workspace/' + quote(runtime.agent_id, safe='') + '/semantic-context/learn'
    body = {
        'uid': runtime.uid,
        'agent_id': runtime.agent_id,
        'conversation_id': grant['binding']['conversation_id'],
        'learning_receipt_id': grant['learning_receipt_id'],
    }
    await asyncio.to_thread(require_queue_graph, runtime.uid, grant['binding']['conversation_id'])
    try:
        async with asyncio.timeout(seconds):
            async with client_factory(
                timeout=httpx.Timeout(seconds, connect=min(2, seconds)), follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    'POST',
                    url,
                    json=body,
                    headers={
                        'Authorization': 'Bearer ' + authority.token,
                        'X-Ella-Owner-Uid': runtime.uid,
                        'Accept': 'application/json',
                    },
                ) as response:
                    if response.status_code != 200:
                        hold('learning_host_rejected')
                    parts = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 196608:
                            hold('learning_host_capacity')
                        parts.append(chunk)
        authority_reader(expected_snapshot=snapshot)
        value = json.loads(
            b''.join(parts),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(value, dict) or set(value) != {'receipt', 'context_snapshot'}:
            hold('learning_host_response_invalid')
        receipt = value['receipt']
        context = value['context_snapshot']
        if (
            not isinstance(receipt, dict)
            or receipt.get('status') != 'completed'
            or receipt.get('grant_sha256') != digest(grant)
            or receipt.get('memory_after_sha256') != grant['memory_after_sha256']
            or not isinstance(context, dict)
            or context.get('owner_uid') != runtime.uid
            or receipt.get('context_sha256') != digest(context)
            or receipt.get('context_revision') != context.get('revision')
            or bytes_digest(context['documents']['MEMORY.md'].encode()) != grant['memory_after_sha256']
        ):
            hold('learning_host_completion_unconfirmed')
        return value
    except Exception:
        # Unknown append cannot be reported as rollback/cleanup or retried here.
        hold('learning_host_append_outcome_unknown')
