import contextvars
import os
from typing import List

from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
import tiktoken

from models.conversation import Structured

# ====== ELLA LLM PROXY PATCH - START ======
# Injects user context into all LLM API calls when ELLA_LLM_BASE_URL is set.
# The n8n proxy receives user ID and routes to appropriate models + enhances with context.

_ella_context = contextvars.ContextVar('ella_context', default={})


def set_ella_context(uid: str = None, task: str = None):
    """Set Ella context for the current request/task."""
    ctx = {}
    if uid:
        ctx['uid'] = uid
    if task:
        ctx['task'] = task
    _ella_context.set(ctx)


def get_ella_context() -> dict:
    """Get current Ella context for this request/task."""
    return _ella_context.get()


def clear_ella_context():
    """Clear Ella context."""
    _ella_context.set({})


def _apply_ella_llm_patch():
    """Patch ChatOpenAI._generate to inject user ID into all API calls."""
    _original_generate = ChatOpenAI._generate

    def _patched_generate(self, messages, stop=None, run_manager=None, **kwargs):
        ctx = get_ella_context()
        if ctx.get('uid'):
            task = ctx.get('task', 'unknown')
            kwargs['user'] = f"ella:{ctx['uid']}:{task}"
        return _original_generate(self, messages, stop, run_manager, **kwargs)

    ChatOpenAI._generate = _patched_generate


_apply_ella_llm_patch()
# ====== ELLA LLM PROXY PATCH - END ======


# Base models for general use
# Priority: Ella LLM Proxy > xAI Grok > OpenAI (upstream defaults)
_ella_base_url = os.getenv('ELLA_LLM_BASE_URL')
_ella_api_key = os.getenv('ELLA_LLM_API_KEY', 'ella-internal')
_xai_api_key = os.getenv('XAI_API_KEY')
_xai_base_url = "https://api.x.ai/v1"
_default_mini = os.getenv('OMI_LLM_MINI', 'grok-4-1-fast-non-reasoning')
_default_medium = os.getenv('OMI_LLM_MEDIUM', 'grok-4-1-fast-non-reasoning')
_default_large = os.getenv('OMI_LLM_LARGE', 'grok-4-1-fast-reasoning')
_ella_model = os.getenv('ELLA_LLM_MODEL', 'ella-enhanced')

if _ella_base_url:
    llm_mini = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url)
    llm_mini_stream = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url, streaming=True)
    llm_medium = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url)
    llm_medium_stream = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url, streaming=True)
    llm_large = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url)
    llm_large_stream = ChatOpenAI(model=_ella_model, api_key=_ella_api_key, base_url=_ella_base_url, streaming=True)
elif _xai_api_key:
    llm_mini = ChatOpenAI(model=_default_mini, api_key=_xai_api_key, base_url=_xai_base_url)
    llm_mini_stream = ChatOpenAI(model=_default_mini, api_key=_xai_api_key, base_url=_xai_base_url, streaming=True)
    llm_medium = ChatOpenAI(model=_default_medium, api_key=_xai_api_key, base_url=_xai_base_url)
    llm_medium_stream = ChatOpenAI(model=_default_medium, api_key=_xai_api_key, base_url=_xai_base_url, streaming=True)
    llm_large = ChatOpenAI(model=_default_large, api_key=_xai_api_key, base_url=_xai_base_url)
    llm_large_stream = ChatOpenAI(model=_default_large, api_key=_xai_api_key, base_url=_xai_base_url, streaming=True)
else:
    llm_mini = ChatOpenAI(model='gpt-4.1-mini')
    llm_mini_stream = ChatOpenAI(model='gpt-4.1-mini', streaming=True)
    llm_medium = ChatOpenAI(model='gpt-4.1')
    llm_medium_stream = ChatOpenAI(model='gpt-4.1', streaming=True)
    llm_large = ChatOpenAI(model='o1-preview')
    llm_large_stream = ChatOpenAI(model='o1-preview', streaming=True, temperature=1)

# These models are NOT routed through Ella proxy (OpenRouter/specialized)
llm_high = ChatOpenAI(model='o4-mini')
llm_high_stream = ChatOpenAI(model='o4-mini', streaming=True, temperature=1)
llm_medium_experiment = ChatOpenAI(model='gpt-5.1')

# Specialized models for agentic workflows
llm_agent = ChatOpenAI(model='gpt-5.1')
llm_agent_stream = ChatOpenAI(model='gpt-5.1', streaming=True)
llm_persona_mini_stream = ChatOpenAI(
    temperature=0.8,
    model="google/gemini-flash-1.5-8b",
    api_key=os.environ.get('OPENROUTER_API_KEY'),
    base_url="https://openrouter.ai/api/v1",
    default_headers={"X-Title": "Omi Chat"},
    streaming=True,
)
llm_persona_medium_stream = ChatOpenAI(
    temperature=0.8,
    model="anthropic/claude-3.5-sonnet",
    api_key=os.environ.get('OPENROUTER_API_KEY'),
    base_url="https://openrouter.ai/api/v1",
    default_headers={"X-Title": "Omi Chat"},
    streaming=True,
)

# Gemini models for large context analysis
llm_gemini_flash = ChatOpenAI(
    temperature=0.7,
    model="google/gemini-3-flash-preview",
    api_key=os.environ.get('OPENROUTER_API_KEY'),
    base_url="https://openrouter.ai/api/v1",
    default_headers={"X-Title": "Omi Wrapped"},
)

embeddings = OpenAIEmbeddings(model="text-embedding-3-large")
parser = PydanticOutputParser(pydantic_object=Structured)

encoding = tiktoken.encoding_for_model('gpt-4')


def num_tokens_from_string(string: str) -> int:
    """Returns the number of tokens in a text string."""
    num_tokens = len(encoding.encode(string))
    return num_tokens


def generate_embedding(content: str) -> List[float]:
    return embeddings.embed_documents([content])[0]
