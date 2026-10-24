"""Explicit production composition; no activation/background work on import."""

import asyncio, hashlib, os, re, uuid
from ella.services.whisper_tool_delivery_legacy import ToolWorkStore, ToolDeliveryService, held, validate_binding


async def current_binding(uid):
    from ella.services.semantic_runtime_authority import resolve_semantic_runtime, semantic_runtime_authority_identity
    from ella.services.semantic_host_context import HostReadBudget

    # Same retained resolver/canonical account + configured host policy/model snapshot.
    runtime = await resolve_semantic_runtime(uid, host_read_budget=HostReadBudget(max_calls=2, wall_seconds=12))
    identity = semantic_runtime_authority_identity(runtime)
    if runtime.uid != uid or runtime.provider != 'hermes' or runtime.status != 'active':
        held('tool_runtime_inactive')
    return validate_binding(
        {
            'uid': uid,
            'agent_id': runtime.agent_id,
            'profile_name': runtime.profile_name,
            'account_user_id': runtime.account_user_id,
            'runtime_identity_sha256': identity.digest,
        },
        uid,
    )


async def current_consent(uid):
    from ella.services.ai_consent import assert_current_ai_consent

    await asyncio.to_thread(assert_current_ai_consent, uid)


async def current_capture(uid, proof):
    from ella.services.whisper_tool_capture import current_hashed_capture

    if await asyncio.to_thread(current_hashed_capture, uid, proof) is not True:
        held('tool_capture_owner_stale')


async def strict_audio(uid, message, work):
    """Exactly one existing configured primary provider; no provider override/fallback.

    Scoped START and current mode/session checks occur before this helper. Local
    audio uses the established serving path; ambiguous final enqueue retains it.
    """
    import httpx
    from ella.routers import guardian

    if uid in {'.', '..'} or not re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,128}', uid):
        held('tool_audio_owner_invalid')
    settings = await asyncio.to_thread(guardian.app_settings_db.get_voice_settings, uid)
    effective = guardian.build_effective_voice_settings(uid, settings)['effective_voice_settings']
    providers = guardian._guardian_tts_candidates(effective)
    if not providers:
        held('tool_provider_unavailable')
    headers = {'X-TTS-Provider': providers[0], 'Accept-Encoding': 'identity'}
    if guardian.ELLA_INTERNAL_VOICE_TTS_TOKEN:
        headers.update({'X-Ella-Internal-Token': guardian.ELLA_INTERNAL_VOICE_TTS_TOKEN, 'X-Ella-Subject-Uid': uid})
    await current_consent(uid)
    binding = await current_binding(uid)
    if binding != work['binding']:
        held('tool_runtime_changed')
    await ToolWorkStore(await guardian._get_pool()).authority_started(
        uid, work['tool_work_id'], binding, current_capture
    )
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        async with client.stream(
            'POST', guardian.ELLA_INTERNAL_VOICE_TTS_URL, json={'text': message}, headers=headers
        ) as response:
            if response.status_code != 200:
                held('tool_synthesis_failed')
            if response.headers.get('content-encoding', 'identity').strip().lower() not in {'', 'identity'}:
                held('tool_synthesis_encoding_invalid')
            mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if mime not in {'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav'}:
                held('tool_synthesis_invalid')
            chunks = []
            size = 0
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > 10485760:
                    held('tool_audio_capacity')
                chunks.append(chunk)
    if not size:
        held('tool_synthesis_invalid')
    content = b''.join(chunks)
    filename = 'retained-' + work['tool_work_id'] + '-' + uuid.uuid4().hex + ('.wav' if 'wav' in mime else '.mp3')

    def write():
        # Existing serving base; anchored no-follow owned directory, exclusive file.
        basefd = os.open(guardian.AUDIO_BASE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directoryfd = None
        try:
            try:
                os.mkdir(uid, mode=0o700, dir_fd=basefd)
            except FileExistsError:
                pass
            directoryfd = os.open(uid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=basefd)
            metadata = os.fstat(directoryfd)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                held('tool_audio_directory_invalid')
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directoryfd)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
        finally:
            if directoryfd is not None:
                os.close(directoryfd)
            os.close(basefd)

    await asyncio.to_thread(write)
    return guardian.AUDIO_PUBLIC_URL + '/' + uid + '/' + filename


async def create_tool_service():
    from ella.routers.guardian import _get_pool

    return ToolDeliveryService(
        ToolWorkStore(await _get_pool()),
        runtime_binding=current_binding,
        assert_consent=current_consent,
        assert_capture=current_capture,
        synthesize=strict_audio,
    )


async def issue_scanner_tool_work(
    *, pool, uid, origin, request_id, conversation_id, generation, owner_token, is_current_capture_owner
):
    """Call immediately after committed delivery origin, before ANY agent dispatch.

    Producer owns this callable; none of these arguments comes from an HTTP body
    or a model tool. Existing capture-owner function is injected by producer.
    """
    from ella.services.whisper_delivery import capture_identity

    async def capture_current():
        value = await asyncio.to_thread(is_current_capture_owner, uid, conversation_id, generation, owner_token)
        if value is not True:
            held('tool_capture_owner_stale')

    async with asyncio.timeout(20):
        await capture_current()
        await current_consent(uid)
        binding = await current_binding(uid)
        work_id = await ToolWorkStore(pool).issue(
            uid=uid,
            origin=origin,
            capture_key=capture_identity(uid, conversation_id, generation, owner_token),
            request_id=request_id,
            binding=binding,
            capture_proof={
                'conversation_id': conversation_id,
                'generation': generation,
                'owner_token_sha256': hashlib.sha256(owner_token.encode()).hexdigest(),
            },
        )
        await capture_current()
        await current_consent(uid)
        if await current_binding(uid) != binding:
            held('tool_runtime_changed')
        return work_id
