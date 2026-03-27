import contextvars
import os
import logging
from typing import List

from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
import tiktoken

from models.conversation import Structured

logger = logging.getLogger(__name__)

# ====== ELLA LLM PROXY PATCH - START ======
_ella_context = contextvars.ContextVar('ella_context', default={})


def set_ella_context(uid: str = None, task: str = None):
    ctx = {}
    if uid:
        ctx['uid'] = uid
    if task:
        ctx['task'] = task
    _ella_context.set(ctx)


def get_ella_context() -> dict:
    return _ella_context.get()


def clear_ella_context():
    _ella_context.set({})


def _apply_ella_llm_patch():
    _original_generate = ChatOpenAI._generate
    _original_stream = ChatOpenAI._stream

    def _patched_generate(self, messages, stop=None, run_manager=None, **kwargs):
        ctx = get_ella_context()
        if ctx.get('uid'):
            task = ctx.get('task', 'unknown')
            kwargs['user'] = f"ella:{ctx['uid']}:{task}"
        return _original_generate(self, messages, stop, run_manager, **kwargs)

    def _patched_stream(self, messages, stop=None, run_manager=None, **kwargs):
        ctx = get_ella_context()
        if ctx.get('uid'):
            task = ctx.get('task', 'unknown')
            kwargs['user'] = f"ella:{ctx['uid']}:{task}"
        return _original_stream(self, messages, stop, run_manager, **kwargs)

    ChatOpenAI._generate = _patched_generate
    ChatOpenAI._stream = _patched_stream


_apply_ella_llm_patch()
# ====== ELLA LLM PROXY PATCH - END ======


# ====== RUNTIME FALLBACK PROVIDER CHAIN ======
# Each LLM call automatically cascades through providers on failure (402, 429, 5xx, timeout).
# Priority: OpenAI (paid, spending limits) → Ollama Cloud (free, included in sub) → Groq (free tier)
#
# Uses LangChain's .with_fallbacks() — on any exception from primary, tries next provider.
# All exported llm_* variables are RunnableWithFallbacks (or plain ChatOpenAI if only 1 provider).
# Callers use .invoke(), .ainvoke(), pipe (|), .stream() — all work transparently.
#
# Dead providers (out of credits): OpenRouter, xAI — NOT included in chain.

_openai_api_key = os.getenv('OPENAI_API_KEY')
_ollama_api_key = os.getenv('OLLAMA_API_KEY')
_groq_api_key = os.getenv('GROQ_API_KEY')
_ella_base_url = os.getenv('ELLA_LLM_BASE_URL')
_ella_api_key = os.getenv('ELLA_LLM_API_KEY', 'ella-internal')
_ella_model = os.getenv('ELLA_LLM_MODEL', 'ella-enhanced')

# Ollama Cloud endpoint (direct to ollama.com — local proxy had auth issues)
_ollama_cloud_base_url = "https://ollama.com/v1"

# Model selections per provider
_openai_mini = os.getenv('OMI_OPENAI_MINI', 'gpt-4.1-mini')
_openai_medium = os.getenv('OMI_OPENAI_MEDIUM', 'gpt-4.1-mini')
_ollama_model = os.getenv('OMI_OLLAMA_MODEL', 'nemotron-3-super')
_ollama_fallback_model = os.getenv('OMI_OLLAMA_FALLBACK', 'gemma3:27b')
_groq_model = os.getenv('OMI_GROQ_MODEL', 'llama-3.1-8b-instant')


def _make_openai(model=None, **kwargs):
    """Create a ChatOpenAI instance using OpenAI direct."""
    return ChatOpenAI(api_key=_openai_api_key, model=model or _openai_medium, **kwargs)


def _make_ollama_cloud(model=None, **kwargs):
    """Create a ChatOpenAI instance using Ollama Cloud (ollama.com direct)."""
    return ChatOpenAI(
        api_key=_ollama_api_key or 'ollama-local',
        base_url=_ollama_cloud_base_url,
        model=model or _ollama_model,
        **kwargs
    )


def _make_groq(model=None, **kwargs):
    """Create a ChatOpenAI instance using Groq free tier."""
    return ChatOpenAI(
        api_key=_groq_api_key,
        base_url="https://api.groq.com/openai/v1",
        model=model or _groq_model,
        **kwargs
    )


def _with_fallbacks(primary, fallbacks):
    """Wrap a primary LLM with fallback providers. Returns primary if no fallbacks."""
    available = [f for f in fallbacks if f is not None]
    if not available:
        return primary
    return primary.with_fallbacks(available)


# ====== BUILD RUNTIME FALLBACK CHAINS ======
# Each tier: create primary + fallback instances, wrap with .with_fallbacks()

_providers_available = []
if _openai_api_key:
    _providers_available.append('openai')
if _ollama_api_key:
    _providers_available.append('ollama_cloud')
if _groq_api_key:
    _providers_available.append('groq')

print(f"[FLOW:LLM-INIT] providers_available={_providers_available} mode=runtime_fallback", flush=True)


def _build_chain(openai_model=None, ollama_model=None, groq_model=None, **kwargs):
    """Build a primary→fallback chain from all available providers."""
    openai_model = openai_model or _openai_medium
    ollama_model = ollama_model or _ollama_model
    groq_model = groq_model or _groq_model

    primary = None
    fallbacks = []

    if _openai_api_key:
        inst = _make_openai(model=openai_model, **kwargs)
        if primary is None:
            primary = inst
        else:
            fallbacks.append(inst)

    if _ollama_api_key:
        inst = _make_ollama_cloud(model=ollama_model, **kwargs)
        if primary is None:
            primary = inst
        else:
            fallbacks.append(inst)

    if _groq_api_key:
        inst = _make_groq(model=groq_model, **kwargs)
        if primary is None:
            primary = inst
        else:
            fallbacks.append(inst)

    if primary is None:
        # No providers — return a bare ChatOpenAI that will fail on use
        primary = ChatOpenAI(model='gpt-4.1-mini', **kwargs)

    return _with_fallbacks(primary, fallbacks)


# ====== EXPORTED LLM INSTANCES (all with runtime fallback) ======

# Mini tier: lightweight tasks (should_discard, memories, onboarding)
llm_mini = _build_chain(openai_model=_openai_mini, ollama_model=_ollama_fallback_model)
llm_mini_stream = _build_chain(openai_model=_openai_mini, ollama_model=_ollama_fallback_model, streaming=True)

# Medium tier: conversation processing, structured output
llm_medium = _build_chain()
llm_large = _build_chain()  # same as medium, save cost
llm_large_stream = _build_chain(streaming=True)
llm_high = _build_chain()
llm_high_stream = _build_chain(streaming=True)
llm_medium_experiment = _build_chain()
llm_agent = _build_chain()
llm_agent_stream = _build_chain(streaming=True)

# Chat streaming: Ella proxy if available, else fallback chain
if _ella_base_url:
    llm_medium_stream = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url, streaming=True)
    print(f"[FLOW:LLM-INIT] medium_stream=ella_proxy model={_ella_model}", flush=True)
else:
    llm_medium_stream = _build_chain(streaming=True)

# Persona models: prefer Ollama Cloud (free, no token spend) as primary, then OpenAI, then Groq
def _build_persona_chain(ollama_model=None, temperature=0.8, **kwargs):
    """Persona chain: Ollama Cloud primary (free) → OpenAI → Groq."""
    ollama_model = ollama_model or _ollama_model
    primary = None
    fallbacks = []

    if _ollama_api_key:
        inst = _make_ollama_cloud(model=ollama_model, temperature=temperature, **kwargs)
        if primary is None:
            primary = inst
        else:
            fallbacks.append(inst)

    if _openai_api_key:
        inst = _make_openai(model=_openai_medium, temperature=temperature, **kwargs)
        if primary is None:
            primary = inst
        else:
            fallbacks.append(inst)

    if _groq_api_key:
        inst = _make_groq(temperature=temperature, **kwargs)
        if primary is None:
            primary = inst
        else:
            fallbacks.append(inst)

    if primary is None:
        primary = ChatOpenAI(model='gpt-4.1-mini', temperature=temperature, **kwargs)

    return _with_fallbacks(primary, fallbacks)


llm_persona_mini_stream = _build_persona_chain(ollama_model=_ollama_fallback_model, streaming=True)
llm_persona_medium_stream = _build_persona_chain(streaming=True)
llm_gemini_flash = _build_persona_chain(temperature=0.7)

_primary = _providers_available[0] if _providers_available else 'none'
_persona_primary = 'ollama_cloud' if _ollama_api_key else _primary
print(f"[FLOW:LLM-INIT] primary={_primary} persona={_persona_primary} fallbacks={len(_providers_available)} ella_patch=active", flush=True)

# ====== EMBEDDINGS (always OpenAI — Pinecone index is 3072-dim) ======
embeddings = OpenAIEmbeddings(model="text-embedding-3-large")
parser = PydanticOutputParser(pydantic_object=Structured)

encoding = tiktoken.encoding_for_model('gpt-4')


def num_tokens_from_string(string: str) -> int:
    num_tokens = len(encoding.encode(string))
    return num_tokens


def generate_embedding(content: str) -> List[float]:
    return embeddings.embed_documents([content])[0]
