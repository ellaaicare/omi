import asyncio
import ast
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from typing import Awaitable, Callable, Optional

import pytest
from fastapi import HTTPException

sys.modules.setdefault('database._client', MagicMock(db=MagicMock()))
from utils.ella import scanner
from utils.ella.ambient_deadline import AmbientDeadlines


class Loop:
    def __init__(self):
        self.now = 0
        self.handles = []

    def time(self):
        return self.now

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def call_later(self, delay, fn, *args):
        handle = SimpleNamespace(at=self.now + delay, fn=fn, args=args, cancelled=False)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        self.handles.append(handle)
        return handle

    def advance(self, seconds):
        self.now += seconds
        for handle in tuple(self.handles):
            if not handle.cancelled and handle.at <= self.now:
                handle.cancelled = True
                handle.fn(*handle.args)


def item(**changes):
    return dict(
        uid='user-a',
        conversation_id='conversation-a',
        origin_generation='generation-a',
        origin_owner_token='owner-a',
        device_type='omi',
        segments=[{'text': 'fictional short intent'}],
        **changes
    )


@pytest.fixture
def scheduling(monkeypatch):
    loop = Loop()
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: loop)
    enqueued, discarded = [], []
    active = {'value': True}
    scheduler = AmbientDeadlines(
        lambda value: enqueued.append(value) or True,
        lambda value, token: discarded.append((value, token)),
        lambda value: active['value'],
        lambda value, token: True,
    )
    return loop, scheduler, enqueued, discarded, active


def test_silence_fires_original_age_deadline_without_synthetic_segments(scheduling):
    loop, scheduler, enqueued, _, _ = scheduling
    scheduler.schedule_from_thread(item(), {'batch_id': 'batch-a', 'delay_seconds': 10})
    loop.advance(6)
    scheduler.schedule_from_thread(item(), {'batch_id': 'batch-a', 'delay_seconds': 4})
    loop.advance(3.999)
    assert enqueued == []
    loop.advance(0.001)
    assert len(enqueued) == 1 and enqueued[0]['segments'] == []
    assert enqueued[0]['ambient_batch_id'] == 'batch-a'
    loop.advance(20)
    assert len(enqueued) == 1


@pytest.mark.parametrize('boundary', ['close', 'rollover', 'inactive'])
def test_finish_disconnect_consent_loss_and_rollover_discard(scheduling, boundary):
    loop, scheduler, enqueued, discarded, active = scheduling
    scheduler.schedule_from_thread(item(), {'batch_id': 'batch-a', 'delay_seconds': 10})
    if boundary == 'close':
        scheduler.close()
    elif boundary == 'rollover':
        scheduler.discard_except('conversation-b')
    else:
        active['value'] = False
    loop.advance(10)
    assert enqueued == [] and [x[1] for x in discarded] == ['batch-a']
    assert scheduler._pending == {}


def test_replaced_timer_cannot_fire_or_discard_replacement(scheduling):
    loop, scheduler, enqueued, discarded, _ = scheduling
    scheduler.schedule_from_thread(item(), {'batch_id': 'old', 'delay_seconds': 10})
    old_callback = loop.handles[0]
    scheduler.schedule_from_thread(item(), {'batch_id': 'new', 'delay_seconds': 20})
    old_callback.fn(*old_callback.args)
    loop.advance(10)
    assert enqueued == [] and [x[1] for x in discarded] == ['old']
    loop.advance(10)
    assert [x['ambient_batch_id'] for x in enqueued] == ['new']


def test_closed_callback_and_queue_saturation_discard(scheduling):
    loop, scheduler, enqueued, discarded, _ = scheduling
    scheduler._enqueue = lambda _: False
    scheduler.schedule_from_thread(item(), {'batch_id': 'full', 'delay_seconds': 1})
    loop.advance(1)
    scheduler.close()
    scheduler.schedule_from_thread(item(), {'batch_id': 'late', 'delay_seconds': 1})
    assert enqueued == [] and [x[1] for x in discarded] == ['full', 'late']


def test_stale_old_receipt_does_not_cancel_live_replacement(scheduling):
    loop, scheduler, enqueued, discarded, _ = scheduling
    scheduler._current = lambda _item, token: token == 'new'
    scheduler.schedule_from_thread(item(), {'batch_id': 'new', 'delay_seconds': 10})
    scheduler.schedule_from_thread(item(), {'batch_id': 'old', 'delay_seconds': 20})
    loop.advance(10)
    assert [x['ambient_batch_id'] for x in enqueued] == ['new']
    assert [x[1] for x in discarded] == ['old']


@pytest.mark.parametrize('boundary', ['close', 'rollover'])
def test_queued_timer_remains_owned_until_dispatch_completes(scheduling, boundary):
    loop, scheduler, enqueued, discarded, _ = scheduling
    scheduler.schedule_from_thread(item(), {'batch_id': 'queued', 'delay_seconds': 1})
    loop.advance(1)
    assert len(enqueued) == 1
    if boundary == 'close':
        scheduler.close()
    else:
        scheduler.discard_except('replacement')
    assert [x[1] for x in discarded] == ['queued']
    assert scheduler._pending == {}


def test_late_thread_bridge_uses_absolute_deadline_not_new_delay(scheduling):
    loop, scheduler, enqueued, _, _ = scheduling
    loop.advance(6)
    scheduler.schedule_from_thread(item(), {'batch_id': 'batch-a', 'delay_seconds': 10, 'deadline_monotonic': 10})
    loop.advance(4)
    assert len(enqueued) == 1


def test_real_event_loop_thread_bridge_fires_once_and_closes():
    async def scenario():
        ready = asyncio.Event()
        enqueued, discarded = [], []

        def enqueue(value):
            enqueued.append(value)
            ready.set()
            return True

        scheduler = AmbientDeadlines(
            enqueue, lambda value, token: discarded.append(token), lambda _: True, lambda _, token: True
        )
        await asyncio.to_thread(
            scheduler.schedule_from_thread,
            item(),
            {'batch_id': 'real', 'delay_seconds': 0.02, 'deadline_monotonic': asyncio.get_running_loop().time() + 0.02},
        )
        await asyncio.wait_for(ready.wait(), timeout=1)
        scheduler.close()
        await asyncio.sleep(0.03)
        assert len(enqueued) == 1 and enqueued[0]['segments'] == []
        assert discarded == ['real'] and scheduler._pending == {}

    asyncio.run(scenario())


@pytest.fixture
def batching(monkeypatch):
    scanner.reset_scanner_batch_state()
    monkeypatch.setattr(scanner, 'SCANNER_AMBIENT_BATCHING_ENABLED', True)
    monkeypatch.setattr(scanner, 'SCANNER_AMBIENT_BATCH_SECONDS', 10)
    monkeypatch.setattr(scanner, 'SCANNER_AMBIENT_BATCH_WORDS', 70)
    monkeypatch.setattr(scanner, 'SCANNER_AMBIENT_BATCH_MAX_WORDS', 180)
    yield
    scanner.reset_scanner_batch_state()


def apply(segments, *, now, token=None, owner='owner-a'):
    return scanner._apply_ambient_batching(
        'user-a',
        'conversation-a',
        segments,
        'omi',
        'trace-a',
        origin_generation='generation-a',
        origin_owner_token=owner,
        ambient_batch_id=token,
        now=now,
    )


def test_deadline_claim_after_silence_contains_exact_original_segments(batching):
    first = [{'text': 'fictional intent', 'speaker': 'SPEAKER_1'}]
    second = [{'text': 'fictional lapse', 'speaker': 'SPEAKER_1'}]
    _, receipt = apply(first, now=100)
    token = receipt['_deferred']['batch_id']
    _, second_receipt = apply(second, now=106)
    assert second_receipt['_deferred']['batch_id'] == token and second_receipt['_deferred']['delay_seconds'] == 4
    claimed, meta = apply([], now=110, token=token)
    assert claimed == first + second and meta['flush_reason'] == 'time_threshold'
    assert apply([], now=110, token=token)[0] is None
    assert scanner._SCANNER_BATCHES == {}


def test_ingress_and_two_deadline_claims_race_without_duplicates(batching):
    _, meta = apply([{'text': 'short intent'}], now=100)
    token = meta['_deferred']['batch_id']
    barrier = threading.Barrier(3)
    results = []

    def claim():
        barrier.wait()
        results.append(apply([], now=110, token=token)[0])

    def ingress():
        barrier.wait()
        results.append(apply([{'text': 'later lapse'}], now=110)[0])

    threads = [threading.Thread(target=claim), threading.Thread(target=claim), threading.Thread(target=ingress)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    claimed = [segment['text'] for result in results if result for segment in result]
    assert claimed.count('short intent') == 1
    assert claimed.count('later lapse') <= 1
    # Ingress may create the next batch; stale claims cannot consume it.
    if scanner._SCANNER_BATCHES:
        assert all(value['id'] != token for value in scanner._SCANNER_BATCHES.values())


def test_owner_and_batch_replacement_fences(batching):
    _, first = apply([{'text': 'first'}], now=100)
    _, other = apply([{'text': 'other owner'}], now=100, owner='owner-b')
    assert apply([], now=110, token=first['_deferred']['batch_id'], owner='owner-b')[0] is None
    scanner.discard_ambient_batch(item(), first['_deferred']['batch_id'])
    _, replacement = apply([{'text': 'replacement'}], now=110)
    assert apply([], now=120, token=first['_deferred']['batch_id'])[0] is None
    assert apply([], now=120, token=replacement['_deferred']['batch_id'])[0] == [{'text': 'replacement'}]
    assert apply([], now=120, token=other['_deferred']['batch_id'], owner='owner-b')[0] == [{'text': 'other owner'}]


def test_long_batch_and_backpressure_preserve_existing_thresholds(batching):
    assert apply([{'text': ' '.join(['word'] * 70)}], now=100)[1]['flush_reason'] == 'word_threshold'
    scanner._SCANNER_RATE_LIMIT_UNTIL['global'] = 130
    _, deferred = apply([{'text': 'short'}], now=100)
    token = deferred['_deferred']['batch_id']
    assert deferred['_deferred']['delay_seconds'] == 30
    _, at_deadline = apply([], now=110, token=token)
    assert at_deadline['flush_reason'] == 'rate_limited_defer'
    assert at_deadline['_deferred']['delay_seconds'] == 20
    assert apply([], now=130, token=token)[1]['flush_reason'] == 'time_threshold'
    scanner._SCANNER_RATE_LIMIT_UNTIL['global'] = 160
    _, dropped = apply([{'text': ' '.join(['word'] * 180)}], now=130)
    assert dropped['flush_reason'] == 'rate_limited_drop' and '_deferred' not in dropped


def test_word_eligible_backoff_does_not_wait_for_age_threshold(batching):
    scanner._SCANNER_RATE_LIMIT_UNTIL['global'] = 102
    _, meta = apply([{'text': ' '.join(['word'] * 70)}], now=100)
    assert meta['_deferred']['delay_seconds'] == 2
    assert apply([], now=102, token=meta['_deferred']['batch_id'])[1]['flush_reason'] == 'word_threshold'


def test_timer_reuses_existing_guarded_payload_and_does_not_force_routing(monkeypatch, batching):
    posts, receipts = [], []
    monkeypatch.setattr(scanner.ELLA_CONFIG, 'scanner_enabled', True)
    monkeypatch.setattr(scanner, 'SCANNER_WEBHOOK_KEY', 'test-only')
    monkeypatch.setattr(scanner, '_log_trace_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, 'select_playback_ledger_candidates', lambda *_: [])
    monkeypatch.setattr(
        scanner,
        '_post_scanner_webhook',
        lambda _url, json, **_: posts.append(json) or SimpleNamespace(status_code=200, headers={}),
    )
    now = {'value': 100}
    monkeypatch.setattr(scanner.time, 'time', lambda: now['value'])
    kwargs = dict(
        uid='user-a',
        conversation_id='conversation-a',
        guardian_mode='memory_support',
        origin_generation='generation-a',
        origin_owner_token='owner-a',
        on_ambient_deferred=receipts.append,
    )
    scanner.send_to_scanner(segments=[{'text': 'fictional intent', 'speaker': 'SPEAKER_1'}], **kwargs)
    now['value'] = 106
    scanner.send_to_scanner(segments=[{'text': 'fictional lapse', 'speaker': 'SPEAKER_1'}], **kwargs)
    assert posts == []
    now['value'] = 110
    scanner.send_to_scanner(segments=[], ambient_batch_id=receipts[-1]['batch_id'], **kwargs)
    assert len(posts) == 1 and len(posts[0]['segments']) == 2
    assert posts[0]['guardian_mode'] == 'memory_support'
    assert posts[0]['scanner_batch']['flush_reason'] == 'time_threshold'
    assert 'spoken_diagnostic' not in posts[0] and '_deferred' not in posts[0]['scanner_batch']


def test_timer_off_mode_discards_without_provider(monkeypatch, batching):
    _, meta = apply([{'text': 'fictional intent'}], now=100)
    monkeypatch.setattr(scanner.ELLA_CONFIG, 'scanner_enabled', True)
    monkeypatch.setattr(scanner, 'SCANNER_WEBHOOK_KEY', 'test-only')
    monkeypatch.setattr(scanner, '_log_trace_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, '_post_scanner_webhook', lambda *_args, **_kwargs: pytest.fail('OFF egress'))
    scanner.send_to_scanner(
        segments=[],
        guardian_mode=None,
        ambient_batch_id=meta['_deferred']['batch_id'],
        **{k: v for k, v in item().items() if k != 'segments'}
    )
    assert scanner._SCANNER_BATCHES == {}


@pytest.mark.parametrize(
    'changed', ['generation', 'owner', 'conversation', 'drained', 'expired', 'missing', 'protocol']
)
def test_authoritative_owner_fails_closed(changed):
    source = Path(__file__).resolve().parents[2] / 'utils/conversations/capture_protocol.py'
    tree = ast.parse(source.read_text())
    names = {
        '_current_capture_owner_transaction',
        '_authority_tuple_matches',
        '_conversation_tuple_matches',
        '_status_value',
        '_aware',
        '_lease_expired',
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(
        datetime=datetime,
        timezone=timezone,
        Any=object,
        Dict=dict,
        Optional=__import__('typing').Optional,
        CAPTURE_PROTOCOL_VERSION=2,
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    now = datetime.now(timezone.utc)
    authority = dict(
        protocol_version=2,
        conversation_id='conversation-a',
        generation='generation-a',
        owner_token='owner-a',
        state='active',
        lease_expires_at=now + timedelta(seconds=30),
    )
    conversation = dict(
        id='conversation-a',
        capture_protocol_version=2,
        capture_generation='generation-a',
        capture_owner_token='owner-a',
        capture_owner_id='owner-a',
        capture_state='active',
        status='in_progress',
        capture_lease_expires_at=now + timedelta(seconds=30),
    )
    exists = {'value': True}
    ref = lambda data: SimpleNamespace(get=lambda **_: SimpleNamespace(exists=exists['value'], to_dict=lambda: data))
    call = lambda: namespace['_current_capture_owner_transaction'](
        object(), ref(authority), ref(conversation), 'conversation-a', 'generation-a', 'owner-a', now
    )
    assert call() is True
    if changed == 'generation':
        authority['generation'] = 'replaced'
    elif changed == 'owner':
        conversation['capture_owner_id'] = 'replaced'
    elif changed == 'conversation':
        authority['conversation_id'] = 'other-conversation'
    elif changed == 'drained':
        conversation['capture_state'] = 'drained'
    elif changed == 'expired':
        authority['lease_expires_at'] = now
    elif changed == 'missing':
        exists['value'] = False
    else:
        conversation['capture_protocol_version'] = 1
    assert call() is False


def test_owner_expiry_during_snapshot_reads_uses_post_read_time():
    source = Path(__file__).resolve().parents[2] / 'utils/conversations/capture_protocol.py'
    tree = ast.parse(source.read_text())
    names = {
        '_current_capture_owner_transaction',
        '_authority_tuple_matches',
        '_conversation_tuple_matches',
        '_status_value',
        '_aware',
        '_lease_expired',
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for n in nodes:
        n.decorator_list = []
    start = datetime.now(timezone.utc)
    read_done = {'value': False}

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert read_done['value']
            return start + timedelta(seconds=31)

    namespace = dict(
        datetime=Clock, timezone=timezone, Any=object, Dict=dict, Optional=Optional, CAPTURE_PROTOCOL_VERSION=2
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    # Datetimes are the production datetime class in the helper's isinstance.
    expiry = Clock.fromtimestamp((start + timedelta(seconds=30)).timestamp(), timezone.utc)
    authority = dict(
        protocol_version=2,
        conversation_id='conversation-a',
        generation='generation-a',
        owner_token='owner-a',
        state='active',
        lease_expires_at=expiry,
    )
    conversation = dict(
        id='conversation-a',
        capture_protocol_version=2,
        capture_generation='generation-a',
        capture_owner_token='owner-a',
        capture_owner_id='owner-a',
        capture_state='active',
        status='in_progress',
        capture_lease_expires_at=expiry,
    )
    authority_ref = SimpleNamespace(get=lambda **_: SimpleNamespace(exists=True, to_dict=lambda: authority))

    def finish_read(**_):
        read_done['value'] = True
        return SimpleNamespace(exists=True, to_dict=lambda: conversation)

    assert (
        namespace['_current_capture_owner_transaction'](
            object(), authority_ref, SimpleNamespace(get=finish_read), 'conversation-a', 'generation-a', 'owner-a'
        )
        is False
    )


@pytest.mark.parametrize(
    'boundary',
    [
        'allowed',
        'off',
        'revoked',
        'unavailable',
        'replaced',
        'finish_during_read',
        'rollover',
        'revoked_during_read',
        'off_during_read',
        'owner_unavailable',
    ],
)
def test_real_timer_bridge_rechecks_consent_mode_owner_and_cleans(boundary, monkeypatch, batching):
    source = Path(__file__).resolve().parents[2] / 'routers/transcribe.py'
    tree = ast.parse(source.read_text())
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {'dispatch_scanner_item', 'ambient_capture_active', '_dispatch_scanner_with_current_consent'}
    ]
    posts, completed, failures = [], [], []

    class Rejected(RuntimeError):
        pass

    async def reject(_):
        return Rejected()

    async def run_sync(subject, checker, rejector, provider, **kwargs):
        try:
            checker(subject)
        except HTTPException as exc:
            raise await rejector(exc)
        return provider(**kwargs)

    after_read = {'value': False}

    async def mode(_):
        return (
            None if boundary == 'off' or (boundary == 'off_during_read' and after_read['value']) else 'memory_support'
        ), None

    def consent(_):
        if boundary in ('revoked', 'unavailable') or (boundary == 'revoked_during_read' and after_read['value']):
            raise HTTPException(403 if boundary == 'revoked' else 503)
        return SimpleNamespace(authorized=True, typesafe_egress_authorized=False)

    def complete(value, token):
        completed.append(token)
        scanner.discard_ambient_batch(value, token)

    ns = dict(
        uid='user-a',
        websocket_active=True,
        accepting_capture=True,
        current_conversation_id='other' if boundary == 'rollover' else 'conversation-a',
        generation_id='generation-a',
        owner_token='owner-a',
        Awaitable=Awaitable,
        Callable=Callable,
        Optional=Optional,
        AiConsentEgressDecision=SimpleNamespace,
        HTTPException=HTTPException,
        AiConsentWebSocketRejected=Rejected,
        _run_sync_provider_with_current_consent=run_sync,
        resolve_ai_consent_egress_decision=consent,
        reject_stt_egress=reject,
        _load_authoritative_guardian_mode=mode,
        send_to_scanner=scanner.send_to_scanner,
        discard_ambient_batch=scanner.discard_ambient_batch,
        _delivery_log=lambda *args, **kwargs: failures.append(kwargs),
        ambient_deadlines=SimpleNamespace(complete=complete, schedule_from_thread=lambda *_: pytest.fail('rearm')),
    )

    def owner(*_):
        after_read['value'] = True
        if boundary == 'owner_unavailable':
            raise RuntimeError('fictional owner store unavailable')
        if boundary == 'finish_during_read':
            ns['accepting_capture'] = False
        return boundary != 'replaced'

    ns['is_current_capture_owner'] = owner

    async def threadpool(fn, *args):
        return fn(*args)

    ns['run_in_threadpool'] = threadpool
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), ns)
    monkeypatch.setattr(scanner.ELLA_CONFIG, 'scanner_enabled', True)
    monkeypatch.setattr(scanner, 'SCANNER_WEBHOOK_KEY', 'test-only')
    monkeypatch.setattr(scanner, '_log_trace_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, 'select_playback_ledger_candidates', lambda *_: [])
    monkeypatch.setattr(
        scanner,
        '_post_scanner_webhook',
        lambda _url, json, **_: posts.append(json) or SimpleNamespace(status_code=200, headers={}),
    )
    _, meta = apply([{'text': 'fictional intent'}], now=100)
    token = meta['_deferred']['batch_id']
    monkeypatch.setattr(scanner.time, 'time', lambda: 110)
    asyncio.run(ns['dispatch_scanner_item']({**item(), 'segments': [], 'ambient_batch_id': token}))
    assert len(posts) == (1 if boundary == 'allowed' else 0)
    assert completed == [token] and scanner._SCANNER_BATCHES == {}
