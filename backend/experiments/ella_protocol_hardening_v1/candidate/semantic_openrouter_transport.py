"""Fixed OpenRouter role transport; existing shared .env snapshot only, no fallback/retry/tools."""

import hashlib, io, json, threading, time, signal, re
from contextlib import contextmanager
from pathlib import Path
import httpx
from dotenv.parser import parse_stream
from gateway.platforms.semantic_codex_transport import _read_snapshot, IsolationHold

MODEL = 'deepseek/deepseek-v4-flash'
ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'


class ProviderDeadlineExceeded(Exception):
    """Closed local origin; no provider response or exception text retained."""


@contextmanager
def _deadline(seconds=90):
    # This transport executes only in the owned main-thread role subprocess.
    if threading.current_thread() is not threading.main_thread():
        raise IsolationHold('provider_transport_failure')
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(*_):
        raise ProviderDeadlineExceeded()

    signal.signal(signal.SIGALRM, expired)
    interval = min(seconds, previous_timer[0]) if previous_timer[0] > 0 else seconds
    signal.setitimer(signal.ITIMER_REAL, interval)
    try:
        yield started + interval
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1]
            )


def _key(raw):
    try:
        rows = [r for r in parse_stream(io.StringIO(raw.decode('utf-8'))) if r.key == 'OPENROUTER_API_KEY']
        if len(rows) != 1 or rows[0].error:
            raise ValueError()
        key = rows[0].value
        if not isinstance(key, str) or not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError()
        return key
    except Exception:
        raise IsolationHold('credential_unqualified') from None


def _json(raw):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError()
            d[k] = v
        return d

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def completion_metadata(value):
    """Closed provider metadata only, never content, reasoning text or URLs."""
    choices = value.get('choices')
    choice = choices[0] if type(choices) is list and len(choices) == 1 and type(choices[0]) is dict else {}
    message = choice.get('message')
    message_is_object = type(message) is dict
    message = message if message_is_object else {}
    finish = choice.get('finish_reason')
    reasons = {'stop', 'length', 'tool_calls', 'content_filter', 'error'}
    role = message.get('role')
    usage = value.get('usage')
    usage = usage if type(usage) is dict else {}
    details = usage.get('completion_tokens_details')
    details = details if type(details) is dict else {}

    def tokens(v):
        return v if type(v) is int and 0 <= v <= 1000000 else None

    response_id = value.get('id')
    return {
        'finish_reason': (
            finish if type(finish) is str and finish in reasons else ('missing' if finish is None else 'other')
        ),
        'message_role': (
            role
            if type(role) is str and role in {'assistant', 'user', 'system', 'tool'}
            else ('missing' if role is None else 'other')
        ),
        'choice_count': len(choices) if type(choices) is list and len(choices) <= 64 else None,
        'response_object': (
            value.get('object')
            if value.get('object') in ('chat.completion', 'chat.completion.chunk')
            else ('missing' if 'object' not in value else 'other')
        ),
        'root_error_present': value.get('error') is not None,
        'choice_error_present': choice.get('error') is not None,
        'choice_index_valid': type(choice.get('index')) is int and choice.get('index') == 0,
        'message_is_object': message_is_object,
        'tool_calls_present': bool(message.get('tool_calls')),
        'function_call_present': bool(message.get('function_call')),
        'refusal_present': bool(message.get('refusal')),
        'completion_tokens': tokens(usage.get('completion_tokens')),
        'reasoning_tokens': tokens(details.get('reasoning_tokens')),
        'provider_response_id': (
            response_id if type(response_id) is str and re.fullmatch(r'gen-[A-Za-z0-9_-]{1,160}', response_id) else None
        ),
    }


def build_isolated_client(
    *,
    auth_path: Path,
    authority_digest,
    revalidate_authority,
    approved_request_hashes,
    response_observer=None,
    diagnostic_observer=None,
    require_durable_diagnostic=False,
    expected_model=MODEL,
    max_output_tokens=8192
):
    if expected_model != MODEL:
        raise IsolationHold('configured_model_invalid')
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
        raise IsolationHold('request_invalid')
    if (
        not isinstance(authority_digest, str)
        or len(authority_digest) != 64
        or any(c not in '0123456789abcdef' for c in authority_digest)
        or not callable(revalidate_authority)
        or type(approved_request_hashes) is not frozenset
        or len(approved_request_hashes) != 1
        or any(
            not isinstance(h, str) or len(h) != 64 or any(c not in '0123456789abcdef' for c in h)
            for h in approved_request_hashes
        )
    ):
        raise IsolationHold('authority_or_fixture_unqualified')

    def authority():
        try:
            valid = revalidate_authority() == authority_digest
        except Exception:
            valid = False
        if not valid:
            raise IsolationHold('runtime_authority_changed')

    authority()
    env_path = auth_path.with_name('.env')
    snapshot = _read_snapshot(env_path)
    fingerprint = hashlib.sha256(snapshot).digest()
    key = _key(snapshot)
    del snapshot
    lock = threading.Lock()
    used = False

    def _send(request):
        nonlocal used
        d = dict(
            phase='admission',
            terminal_seen=False,
            terminal_validated=False,
            function_return_ready=False,
            eof=False,
            event_counts={},
            sequence_truncated=False,
        )
        try:
            authority()
            if type(request) is not dict:
                raise IsolationHold('request_invalid')
            try:
                wire = json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
            except Exception:
                raise IsolationHold('request_invalid') from None
            if len(wire.encode()) > 250000 or hashlib.sha256(wire.encode()).hexdigest() not in approved_request_hashes:
                raise IsolationHold('request_not_reviewed')
            with lock:
                if used:
                    raise IsolationHold('request_already_consumed_or_cap')
                used = True
            if hashlib.sha256(_read_snapshot(env_path)).digest() != fingerprint:
                raise IsolationHold('credential_snapshot_changed')
            body = {
                'model': MODEL,
                'stream': False,
                'messages': [{'role': 'user', 'content': wire}],
                'tools': [],
                'max_tokens': max_output_tokens,
                'response_format': {'type': 'json_object'},
                'provider': {'allow_fallbacks': False, 'data_collection': 'deny'},
            }
            d['phase'] = 'opening_client'
            with httpx.Client(timeout=httpx.Timeout(90, connect=10), trust_env=False, follow_redirects=False) as client:
                d['phase'] = 'opening_response'
                with client.stream(
                    'POST',
                    ENDPOINT,
                    headers={
                        'Authorization': 'Bearer ' + key,
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                        'Accept-Encoding': 'identity',
                    },
                    json=body,
                ) as response:
                    status = response.status_code
                    d['protocol_metadata'] = {
                        'status': status,
                        'parsing_stage': 'headers',
                        'wire_bytes': 0,
                        'decoded_bytes': 0,
                    }
                    if status != 200:
                        raise IsolationHold('provider_http_failure')
                    if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                        raise IsolationHold('response_content_invalid')
                    if response.headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                        raise IsolationHold('response_encoding_invalid')
                    d['phase'] = 'reading_stream'
                    parts = []
                    count = 0
                    for part in response.iter_raw():
                        count += len(part)
                        d['protocol_metadata'].update(wire_bytes=count, decoded_bytes=count)
                        if count > 650000:
                            raise IsolationHold('response_bound_exceeded')
                        parts.append(part)
                    d['eof'] = True
                    d['protocol_metadata'].update(parsing_stage='decoded_eof', wire_bytes=count, decoded_bytes=count)
            try:
                value = _json(b''.join(parts))
            except Exception:
                raise IsolationHold('response_invalid') from None
            if type(value) is dict:
                d['provider_completion'] = completion_metadata(value)
            if type(value) is not dict or value.get('error') is not None:
                raise IsolationHold('provider_terminal_failure')
            if value.get('object') != 'chat.completion':
                raise IsolationHold('response_invalid')
            if value.get('model') != MODEL:
                raise IsolationHold('configured_model_invalid')
            rid = value.get('id')
            if not isinstance(rid, str) or not rid or len(rid) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in rid):
                raise IsolationHold('response_invalid')
            choices = value.get('choices')
            if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                raise IsolationHold('response_invalid')
            c = choices[0]
            if c.get('error') is not None:
                raise IsolationHold('provider_terminal_failure')
            m = c.get('message')
            if (
                type(c.get('index')) is not int
                or c.get('index') != 0
                or c.get('finish_reason') != 'stop'
                or type(m) is not dict
                or m.get('role') != 'assistant'
                or m.get('tool_calls')
                or m.get('function_call')
                or m.get('refusal')
            ):
                raise IsolationHold('response_nonterminal_or_tool')
            text = m.get('content')
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > 100000:
                raise IsolationHold('response_incomplete_or_oversize')
            # Both generation and verification protocols require one JSON object.
            try:
                assert type(_json(text)) is dict
            except Exception:
                raise IsolationHold('response_invalid') from None
            authority()
            if hashlib.sha256(_read_snapshot(env_path)).digest() != fingerprint:
                raise IsolationHold('credential_snapshot_changed')
            d.update(phase='postcheck', terminal_seen=True, terminal_validated=True, function_return_ready=True)
            d['protocol_metadata']['parsing_stage'] = 'completed'
            if response_observer is not None:
                response_observer(
                    {'status': status, 'content_type': 'application/json', 'wire_bytes': count, 'decoded_bytes': count}
                )
            return {'text': text, 'model': MODEL, 'provider_response_id': rid}
        except (httpx.HTTPError, ProviderDeadlineExceeded) as error:
            # Closed metadata only: no exception string, URL, request or body.
            allowed = {
                'ReadTimeout',
                'ConnectTimeout',
                'WriteTimeout',
                'PoolTimeout',
                'ReadError',
                'WriteError',
                'ConnectError',
                'RemoteProtocolError',
                'LocalProtocolError',
                'DecodingError',
            }
            label = 'TimeoutError' if isinstance(error, ProviderDeadlineExceeded) else type(error).__name__
            if label not in allowed | {'TimeoutError'}:
                label = 'other_exception'
            d['exception'] = [{'class': label, 'locations': []}]
            raise IsolationHold('provider_transport_failure') from None
        finally:
            if diagnostic_observer is not None:
                try:
                    diagnostic_observer(d)
                except BaseException:
                    pass
            if (
                require_durable_diagnostic
                and d['function_return_ready']
                and getattr(diagnostic_observer, 'persisted', False) is not True
            ):
                raise IsolationHold('diagnostic_capture_unavailable')

    def send(request):
        with _deadline() as expires:
            result = _send(request)
            if time.monotonic() >= expires:
                raise IsolationHold('provider_transport_failure')
            return result

    return send
