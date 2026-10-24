"""Authenticated opt-in1 role. Direct configured inference; no AIAgent/state DB.

Local profile-owned snapshot only. No auth loader/refresh/recovery/global fallback.
The selected profile is server scope, never a body/header identity assertion.
"""

import asyncio
import sys
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path
from gateway.platforms.semantic_codex_transport import (
    build_isolated_client,
    _read_snapshot,
    IsolationHold,
    MODEL,
    ENDPOINT,
)


def _read_config_snapshot(path: Path) -> bytes:
    """Read a fixed authorized file without following any symlink or writing locks. Config may be readable."""
    if not path.is_absolute() or ".." in path.parts:
        raise IsolationHold("auth_path_invalid")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        f = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            before = os.fstat(f)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or before.st_mode & 0o022
                or before.st_size > 1024 * 1024
            ):
                raise IsolationHold("auth_file_unsafe")
            data = bytearray()
            while True:
                chunk = os.read(f, 65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise IsolationHold("auth_file_too_large")
            after = os.fstat(f)
            linked = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if signature(before) != signature(after) or signature(after) != signature(linked):
                raise IsolationHold("auth_snapshot_changed")
            return bytes(data)
        finally:
            os.close(f)
    except IsolationHold:
        raise
    except Exception:
        raise IsolationHold("auth_snapshot_unavailable") from None
    finally:
        os.close(fd)


class SemanticRoleHold(RuntimeError):
    pass


def require_shared_root_policy(config, model):
    """Existing RuntimeProfilePolicy public_payload/sha256 contract, no I/O."""
    policy = config.get('ella_runtime_policy')
    allowed = {
        'version',
        'provider',
        'model',
        'credentialRef',
        'fallbackModels',
        'reasoningEffort',
        'apiToolsets',
        'isolationPosture',
        'fallbackRoutes',
        'digest',
    }
    required = allowed - {'fallbackRoutes', 'digest'}
    if not isinstance(policy, dict) or set(policy) - allowed or not required.issubset(policy):
        raise SemanticRoleHold('semantic_runtime_policy_witness_missing')
    public = {k: v for k, v in policy.items() if k != 'digest'}
    calculated = hashlib.sha256(json.dumps(public, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if (
        policy.get('digest') != calculated
        or policy.get('provider') != model.get('provider')
        or policy.get('model') != model.get('default')
        or policy.get('credentialRef')
        != (
            {'openai-codex': 'global-auth:openai-codex', 'openrouter': 'env:OPENROUTER_API_KEY'}.get(
                model.get('provider')
            )
        )
        or not isinstance(policy.get('version'), str)
        or not policy['version']
    ):
        raise SemanticRoleHold('semantic_runtime_policy_witness_mismatch')
    return policy


def execute(
    *,
    request_body,
    session,
    role,
    nonce,
    profile,
    auth_path,
    config_path,
    revalidate_config,
    lifecycle_observer=None,
    diagnostic_observer=None
):
    if (
        role not in ('generator', 'verifier', 'learning_generator', 'learning_verifier')
        or not isinstance(nonce, str)
        or len(nonce) != 64
        or any(c not in '0123456789abcdef' for c in nonce)
    ):
        raise SemanticRoleHold('semantic_role_nonce_invalid')
    if not isinstance(profile, str) or not profile or profile == 'custom' or len(profile) > 128:
        raise SemanticRoleHold('semantic_server_profile_missing')
    snapshot = _read_config_snapshot(config_path)
    # This parses only the existing profile configuration; source-only tests
    # supply artificial values. No secrets included in acknowledgement.
    import yaml

    config = yaml.safe_load(snapshot)
    model = config.get('model') if isinstance(config, dict) else None
    if (
        not isinstance(model, dict)
        or model.get('provider') not in ('openai-codex', 'openrouter')
        or not isinstance(model.get('default'), str)
        or not model['default']
    ):
        raise SemanticRoleHold('semantic_configured_primary_unsupported')
    policy = require_shared_root_policy(config, model)
    canonical_base = (
        ENDPOINT.rsplit('/', 1)[0] if model['provider'] == 'openai-codex' else 'https://openrouter.ai/api/v1'
    )
    if model['provider'] == 'openrouter' and model['default'] != 'deepseek/deepseek-v4-flash':
        raise SemanticRoleHold('semantic_configured_primary_unsupported')
    if model.get('base_url') not in (None, '', canonical_base):
        raise SemanticRoleHold('semantic_configured_endpoint_unsupported')
    routes = config.get('model_routes', {})
    if not isinstance(routes, dict):
        raise SemanticRoleHold('semantic_route_configuration_invalid')
    selected_route = routes.get(request_body.get('model')) if isinstance(request_body, dict) else None
    if selected_route is not None and (
        not isinstance(selected_route, dict)
        or selected_route.get('model') != model['default']
        or selected_route.get('provider', model['provider']) != model['provider']
        or selected_route.get('base_url') not in (None, '', canonical_base)
        or selected_route.get('api_key')
    ):
        raise SemanticRoleHold('semantic_configured_route_mismatch')
    frozen = hashlib.sha256(snapshot).hexdigest()

    def authority():
        if revalidate_config() != (profile, str(auth_path), str(config_path)):
            raise SemanticRoleHold('semantic_profile_selector_changed')
        return hashlib.sha256(_read_config_snapshot(config_path)).hexdigest()

    if not isinstance(request_body, dict) or request_body.get('stream') is not False:
        raise SemanticRoleHold('semantic_nonstream_role_required')
    messages = request_body.get('messages')
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or messages[0].get('role') != 'user'
    ):
        raise SemanticRoleHold('semantic_role_message_invalid')
    try:
        role_request = json.loads(messages[0]['content'])
    except Exception:
        raise SemanticRoleHold('semantic_role_request_invalid') from None
    if (
        not isinstance(role_request, dict)
        or role_request.get('policy')
        != ('ella-semantic-learning-v3' if role.startswith('learning_') else 'ella-semantic-summary-v1')
        or not isinstance(role_request.get('instructions'), str)
        or not role_request['instructions']
    ):
        raise SemanticRoleHold('semantic_role_request_invalid')
    if request_body.get('tools') or request_body.get('tool_choice'):
        raise SemanticRoleHold('semantic_tools_forbidden')
    wire = json.dumps(role_request, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    request_hash = hashlib.sha256(wire.encode()).hexdigest()
    factory = build_isolated_client
    provider_options = {}
    if model['provider'] == 'openrouter':
        from gateway.platforms.semantic_openrouter_transport import build_isolated_client as factory

        limit = request_body.get('max_tokens')
        if type(limit) is not int or not 1 <= limit <= 8192:
            raise SemanticRoleHold('semantic_role_request_invalid')
        provider_options['max_output_tokens'] = limit
    client = factory(
        auth_path=auth_path,
        authority_digest=frozen,
        revalidate_authority=authority,
        approved_request_hashes=frozenset([request_hash]),
        response_observer=None,
        diagnostic_observer=diagnostic_observer,
        require_durable_diagnostic=diagnostic_observer is not None,
        expected_model=model['default'],
        **provider_options
    )
    if lifecycle_observer is not None:
        lifecycle_observer("pre_dispatch")
    actual = client(role_request)
    if actual['model'] != model['default']:
        raise SemanticRoleHold('semantic_actual_model_changed')
    completion_id = 'chatcmpl-' + uuid.uuid4().hex[:29]
    ack = {
        'version': 1,
        'profile': profile,
        'model': actual['model'],
        'provider': model['provider'],
        'initial_model': model['default'],
        'initial_provider': model['provider'],
        'runtime_policy_version': policy['version'],
        'runtime_policy_digest': policy['digest'],
        'credential_source': (
            'configured_shared_root_oauth' if model['provider'] == 'openai-codex' else 'configured_shared_root_api_key'
        ),
        'tools_executed': 0,
        'role': role,
        'nonce': nonce,
        'fallback_used': False,
        'completed': True,
        'partial': False,
        'failed': False,
        'session_id': session,
        'gateway_response_id': completion_id,
        'provider_response_id': actual['provider_response_id'],
        'request_sha256': request_hash,
    }
    return {
        'id': completion_id,
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': request_body.get('model'),
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': actual['text']}, 'finish_reason': 'stop'}],
        'hermes': {'completed': True, 'partial': False, 'failed': False},
        'semantic_protocol': ack,
    }


async def handle(adapter, request):
    from aiohttp import web

    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    if not adapter._api_key:
        return web.json_response({'error': {'code': 'semantic_auth_required'}}, status=403)
    limited = adapter._concurrency_limited_response()
    if limited is not None:
        return limited
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.config import get_hermes_home
    from hermes_constants import get_default_hermes_root

    def selector():
        home = Path(get_hermes_home())
        return get_active_profile_name(), str(get_default_hermes_root() / 'auth.json'), str(home / 'config.yaml')

    selected = selector()
    session = request.headers.get('X-Hermes-Session-Id', '')
    key, key_error = adapter._parse_session_key_header(request)
    if key_error is not None:
        return key_error
    if not session or session != key or len(session) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in session):
        return web.json_response({'error': {'code': 'semantic_role_session_invalid'}}, status=400)
    body = await request.json()
    adapter._activate_admitted_request()
    adapter._inflight_agent_runs += 1
    proc = None
    cancelled = False
    try:
        control = {
            'request_body': body,
            'session': session,
            'role': request.headers.get('X-Ella-Semantic-Role'),
            'nonce': request.headers.get('X-Ella-Semantic-Nonce'),
            'profile': selected[0],
            'auth_path': selected[1],
            'config_path': selected[2],
            'config_sha256': hashlib.sha256(_read_config_snapshot(Path(selected[2]))).hexdigest(),
        }
        payload = json.dumps(control, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > 250000:
            raise SemanticRoleHold('semantic_role_input_capacity')
        started = time.monotonic()
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                '-I',
                str(Path(__file__).with_name('semantic_role_worker.py')),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        while True:
            try:
                proc = await asyncio.shield(launch)
                break
            except asyncio.CancelledError:
                cancelled = True
                continue
        if cancelled:
            raise asyncio.CancelledError()
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload), timeout=max(0.001, 100 - (time.monotonic() - started))
        )
        if time.monotonic() - started >= 100:
            raise SemanticRoleHold('semantic_role_worker_held')
        if proc.returncode != 0 or stderr or len(stdout) > 650000:
            raise SemanticRoleHold('semantic_role_worker_held')
        result = json.loads(stdout)
        if selector() != selected:
            raise SemanticRoleHold('semantic_profile_selector_changed')
        if hashlib.sha256(_read_config_snapshot(Path(selected[2]))).hexdigest() != control['config_sha256']:
            raise SemanticRoleHold('semantic_profile_configuration_changed')
        if time.monotonic() - started >= 100:
            raise SemanticRoleHold('semantic_role_worker_held')
    except asyncio.CancelledError:
        cancelled = True
    except (IsolationHold, SemanticRoleHold, asyncio.TimeoutError, ValueError, OSError):
        result = None
    finally:
        if proc is not None and proc.returncode is None:
            # Owned child only. Killing HTTP client does not prove provider drain.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            drain = asyncio.create_task(proc.communicate())
            while True:
                try:
                    await asyncio.shield(drain)
                    break
                except asyncio.CancelledError:
                    cancelled = True
        adapter._inflight_agent_runs -= 1
    if cancelled:
        raise asyncio.CancelledError()
    if result is None:
        return web.json_response({'error': {'code': 'semantic_role_held'}}, status=503)
    return web.json_response(
        result,
        headers={
            'X-Hermes-Session-Id': session,
            'X-Hermes-Session-Key': session,
            'X-Hermes-Completed': 'true',
            'X-Hermes-Partial': 'false',
        },
    )
