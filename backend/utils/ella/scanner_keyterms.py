import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger("ella.scanner_keyterms")

DEFAULT_PROVISION_API_URL = "http://100.76.138.56:8200"
DEFAULT_TTL_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 1.5
DEFAULT_MAX_TERMS = 100
DEFAULT_MAX_TOKENS = 500
DEFAULT_DEEPGRAM_MAX_TERMS = 50
DEFAULT_DEEPGRAM_MAX_TOKENS = 250

_cache: dict[str, "KeytermCacheEntry"] = {}
_uid_agent_ids: dict[str, str] = {}
_refreshing: set[str] = set()
_background_tasks: set[asyncio.Task] = set()
_pool = None

_SECTION_RE = re.compile(r"^\s*@(?P<kind>[a-zA-Z0-9_-]+)\s*:\s*(?P<name>[^\[]+?)(?:\s+\[(?P<state>[A-Z_ -]+)\])?\s*$")
_INLINE_TERM_RE = re.compile(r"`([^`]{2,80})`|\"([^\"]{2,80})\"|'([^']{2,80})'")
_DATE_RE = re.compile(r"\b(?:expires|until|end(?:s)?):\s*(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_LABEL_RE = re.compile(
    r"^(?:phrase|phrases|pattern|patterns|trigger|triggers|wake|wake words?|terms?|keywords?|medications?|conditions?|providers?|names?|examples?)\s*:\s*",
    re.IGNORECASE,
)
_DROP_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*")
_FENCE_RE = re.compile(r"^\s*```")


@dataclass
class KeytermCacheEntry:
    terms: list[str]
    agent_id: str
    fetched_at: float
    source: str
    error: Optional[str] = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _provision_url() -> str:
    return (
        os.getenv("ELLA_PROVISION_API_URL")
        or os.getenv("ELLA_PROVISION_URL")
        or os.getenv("PROVISION_API_URL")
        or DEFAULT_PROVISION_API_URL
    ).rstrip("/")


def _provision_token() -> str:
    return (
        os.getenv("ELLA_PROVISION_API_TOKEN")
        or os.getenv("ELLA_PROVISION_API_KEY")
        or os.getenv("PROVISION_API_TOKEN")
        or ""
    )


def _ttl_seconds() -> float:
    return _env_float("ELLA_SCANNER_KEYTERMS_TTL_SECONDS", DEFAULT_TTL_SECONDS)


def _timeout_seconds() -> float:
    return _env_float("ELLA_SCANNER_KEYTERMS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def _max_terms() -> int:
    return _env_int("ELLA_SCANNER_KEYTERMS_MAX_TERMS", DEFAULT_MAX_TERMS)


def _max_tokens() -> int:
    return _env_int("ELLA_SCANNER_KEYTERMS_MAX_TOKENS", DEFAULT_MAX_TOKENS)


def _deepgram_max_terms() -> int:
    return _env_int("ELLA_DEEPGRAM_KEYTERMS_MAX_TERMS", DEFAULT_DEEPGRAM_MAX_TERMS)


def _deepgram_max_tokens() -> int:
    return _env_int("ELLA_DEEPGRAM_KEYTERMS_MAX_TOKENS", DEFAULT_DEEPGRAM_MAX_TOKENS)


def _enabled() -> bool:
    return os.getenv("ELLA_SCANNER_KEYTERMS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def _allow_shared_fallback() -> bool:
    return os.getenv("ELLA_SCANNER_KEYTERMS_ALLOW_SHARED_FALLBACK", "false").lower() in {"1", "true", "yes", "on"}


async def _get_pool():
    global _pool
    if _pool is None:
        import asyncpg

        _pool = await asyncpg.create_pool(
            host=os.getenv("ELLA_POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("ELLA_POSTGRES_PORT", "5433")),
            user=os.getenv("ELLA_POSTGRES_USER", "postgres"),
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database=os.getenv("ELLA_POSTGRES_DB", "ella_ai"),
            min_size=1,
            max_size=4,
        )
    return _pool


async def _resolve_agent_id(uid: str) -> str:
    if not uid:
        return ""

    if uid in _uid_agent_ids:
        return _uid_agent_ids[uid]

    try:
        pool = await _get_pool()
        row = await pool.fetchrow(
            """
            SELECT ac.agents
            FROM users u
            LEFT JOIN agent_clusters ac ON ac.user_id = u.id
            WHERE u.omi_uid = $1
            """,
            uid,
        )
        if row and row["agents"]:
            agents = row["agents"]
            if isinstance(agents, str):
                agents = json.loads(agents)
            if isinstance(agents, dict):
                agent_id = agents.get("userAgentId") or agents.get("main_agent_id") or agents.get("agentId")
                if agent_id:
                    _uid_agent_ids[uid] = agent_id
                    return agent_id
    except Exception as e:
        logger.warning("scanner_keyterms_agent_resolve_failed uid=%s error=%s", uid, e)

    fallback = f"ella-omi-{uid.lower()}"
    _uid_agent_ids[uid] = fallback
    return fallback


def _section_is_active(header_state: Optional[str], lines: list[str]) -> bool:
    state = (header_state or "").upper()
    if "INACTIVE" in state or "DISABLED" in state:
        return False
    if header_state and "ACTIVE" not in state:
        return False

    today = date.today()
    for line in lines[:8]:
        match = _DATE_RE.search(line)
        if not match:
            continue
        try:
            if date.fromisoformat(match.group(1)) < today:
                return False
        except ValueError:
            continue
    return True


def _split_sections(content: str) -> list[tuple[str, str, Optional[str], list[str]]]:
    sections: list[tuple[str, str, Optional[str], list[str]]] = []
    current: Optional[list] = None
    for line in content.splitlines():
        header_line = re.sub(r"^\s*#+\s*", "", line)
        match = _SECTION_RE.match(header_line)
        if match:
            if current:
                sections.append((current[0], current[1], current[2], current[3]))
            current = [
                match.group("kind").strip().lower(),
                match.group("name").strip().lower(),
                match.group("state"),
                [],
            ]
            continue
        if current:
            state_match = re.match(r"^\s*\[(?P<state>[A-Z_ -]+)\]\s*$", line)
            if state_match and not current[3] and current[2] is None:
                current[2] = state_match.group("state")
                continue
            current[3].append(line)
    if current:
        sections.append((current[0], current[1], current[2], current[3]))
    return sections


def _clean_candidate(value: str) -> str:
    value = _DROP_PREFIX_RE.sub("", value or "").strip()
    value = re.sub(r"\s+#.*$", "", value).strip()
    value = _LABEL_RE.sub("", value).strip()
    value = value.strip(" \t-–—:;,.()[]{}")
    value = value.replace("“", '"').replace("”", '"').replace("’", "'")
    value = re.sub(r"\s+", " ", value)
    return value


def _is_good_term(term: str) -> bool:
    if not term:
        return False
    if len(term) > 50:
        return False
    words = re.findall(r"[A-Za-z0-9]+", term)
    if len(words) > 8:
        return False
    if len(term) < 2:
        return False
    lowered = term.lower()
    bad_fragments = {
        "active",
        "inactive",
        "media",
        "suppress",
        "ignore",
        "background",
        "podcast",
        "music",
        "movie",
        "tv",
        "radio",
        "false positive",
    }
    if lowered in bad_fragments:
        return False
    if any(marker in lowered for marker in ("http://", "https://", "=>", "->")):
        return False
    return True


def _add_candidate(result: list[str], seen: set[str], value: str):
    term = _clean_candidate(value)
    if not _is_good_term(term):
        return
    key = term.casefold()
    if key in seen:
        return
    seen.add(key)
    result.append(term)


def _extract_inline_terms(line: str) -> list[str]:
    terms = []
    for match in _INLINE_TERM_RE.finditer(line):
        term = next(group for group in match.groups() if group)
        terms.append(term)
    return terms


def _extract_line_terms(line: str, *, include_plain_bullets: bool) -> list[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or _FENCE_RE.match(stripped):
        return []

    terms = _extract_inline_terms(stripped)
    without_inline = _INLINE_TERM_RE.sub("", stripped)
    without_inline = re.split(r"\s*(?:→|=>|\|)\s*", without_inline, maxsplit=1)[0]
    cleaned = _clean_candidate(without_inline)
    if not cleaned:
        return terms

    label_match = _LABEL_RE.match(_DROP_PREFIX_RE.sub("", stripped).strip())
    if label_match or include_plain_bullets and _DROP_PREFIX_RE.match(stripped):
        for part in re.split(r"[,;|/]", cleaned):
            terms.append(part)
    return terms


def parse_scanner_tuning_keyterms(content: str) -> list[str]:
    """Extract Deepgram keyterm candidates from active scanner-tuning.md sections.

    The parser is intentionally deterministic and conservative. It prioritizes
    wake words and learned user phrases, skips prefilter suppressions, and only
    pulls short explicit terms from scanner sections.
    """
    prioritized: list[str] = []
    seen: set[str] = set()

    section_priority = {"wakeword": 0, "fastpath": 1, "scanner": 2}
    sections = _split_sections(content or "")
    sections.sort(key=lambda item: section_priority.get(item[0], 99))

    for kind, name, state, lines in sections:
        if kind not in section_priority:
            continue
        if kind == "prefilter":
            continue
        if not _section_is_active(state, lines):
            continue

        include_plain_bullets = kind in {"wakeword", "fastpath"} or "personal-learned" in name
        for line in lines:
            for term in _extract_line_terms(line, include_plain_bullets=include_plain_bullets):
                _add_candidate(prioritized, seen, term)

    return limit_keyterms(prioritized)


def _estimated_tokens(term: str) -> int:
    # Conservative approximation: punctuation-heavy names still consume at
    # least one token, while multi-word phrases count by word-like chunks.
    return max(1, len(re.findall(r"[A-Za-z0-9]+", term)))


def limit_keyterms(terms: list[str], *, max_terms: Optional[int] = None, max_tokens: Optional[int] = None) -> list[str]:
    term_limit = _max_terms() if max_terms is None else max_terms
    token_limit = _max_tokens() if max_tokens is None else max_tokens
    result: list[str] = []
    seen: set[str] = set()
    token_count = 0
    for term in terms:
        cleaned = _clean_candidate(term)
        if not _is_good_term(cleaned):
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        estimated = _estimated_tokens(cleaned)
        if len(result) >= term_limit or token_count + estimated > token_limit:
            break
        seen.add(key)
        result.append(cleaned)
        token_count += estimated
    return result


def combine_deepgram_keyterms(vocabulary: list[str], scanner_terms: list[str]) -> list[str]:
    """Merge scanner terms before generic user vocabulary within safe Deepgram limits.

    Deepgram documents a hard 500-token keyterm limit, but recommends keeping
    requests focused on the most important 20-50 terms. Live websocket opens
    can reject larger practical payloads with HTTP 400, so keep the provider
    payload conservative while preserving the full scanner cache separately.
    """
    return limit_keyterms(
        [*(scanner_terms or []), *(vocabulary or [])],
        max_terms=_deepgram_max_terms(),
        max_tokens=_deepgram_max_tokens(),
    )


async def _fetch_scanner_tuning(agent_id: str) -> str:
    token = _provision_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{_provision_url()}/workspace/{agent_id}/files/scanner-tuning.md"
    async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 404 and _allow_shared_fallback():
            shared = f"{_provision_url()}/workspace/shared/files/scanner-tuning.md"
            response = await client.get(shared, headers=headers)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            return response.text
        if isinstance(payload, dict):
            return str(payload.get("content") or payload.get("text") or "")
        return response.text


async def refresh_scanner_keyterms(uid: str, agent_id: Optional[str] = None) -> list[str]:
    """Refresh scanner keyterms now. Intended for tests/admin warm-up jobs."""
    if not _enabled() or not uid:
        return []

    resolved_agent_id = agent_id or await _resolve_agent_id(uid)
    key = resolved_agent_id or uid
    _refreshing.add(key)
    started = time.time()
    try:
        content = await _fetch_scanner_tuning(resolved_agent_id)
        terms = parse_scanner_tuning_keyterms(content)
        entry = KeytermCacheEntry(terms=terms, agent_id=resolved_agent_id, fetched_at=time.time(), source="provision_api")
        _cache[key] = entry
        _cache[uid] = entry
        _uid_agent_ids[uid] = resolved_agent_id
        logger.info(
            "scanner_keyterms_refreshed uid=%s agent_id=%s count=%s latency_ms=%s",
            uid,
            resolved_agent_id,
            len(terms),
            int((time.time() - started) * 1000),
        )
        return terms
    except Exception as e:
        stale = _cache.get(key) or _cache.get(uid)
        if stale:
            stale.error = str(e)
        logger.warning("scanner_keyterms_refresh_failed uid=%s agent_id=%s error=%s", uid, resolved_agent_id, e)
        return stale.terms if stale else []
    finally:
        _refreshing.discard(key)


def _schedule_refresh(uid: str, agent_id: Optional[str]):
    refresh_key = agent_id or _uid_agent_ids.get(uid) or uid
    if refresh_key in _refreshing:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(refresh_scanner_keyterms(uid, agent_id=agent_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def get_scanner_keyterms(uid: str, agent_id: Optional[str] = None) -> list[str]:
    """Return cached scanner keyterms without blocking WebSocket startup.

    Cache misses and stale entries trigger a background refresh. Call
    refresh_scanner_keyterms() from warm-up/admin code when a synchronous fetch
    is acceptable.
    """
    if not _enabled() or not uid:
        return []

    resolved_key = agent_id or _uid_agent_ids.get(uid) or uid
    entry = _cache.get(resolved_key) or _cache.get(uid)
    now = time.time()
    if entry and now - entry.fetched_at <= _ttl_seconds():
        return entry.terms

    _schedule_refresh(uid, agent_id)
    return entry.terms if entry else []


def cache_status(uid: str, agent_id: Optional[str] = None) -> dict:
    key = agent_id or _uid_agent_ids.get(uid) or uid
    entry = _cache.get(key) or _cache.get(uid)
    if not entry:
        return {"hit": False}
    return {
        "hit": True,
        "agent_id": entry.agent_id,
        "source": entry.source,
        "count": len(entry.terms),
        "age_seconds": max(0, int(time.time() - entry.fetched_at)),
        "error": entry.error,
    }


def clear_scanner_keyterm_cache():
    _cache.clear()
    _uid_agent_ids.clear()
    _refreshing.clear()
