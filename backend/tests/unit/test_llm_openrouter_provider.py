import importlib.util
from pathlib import Path
import sys
import types


def _load_clients(monkeypatch):
    class FakeMessage:
        def __init__(self, content=None):
            self.content = content

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.model_name = kwargs.get("model")

        def _generate(self, *args, **kwargs):
            return None

        def _stream(self, *args, **kwargs):
            return None

        def with_fallbacks(self, fallbacks):
            return self, fallbacks

    class FakeOpenAIEmbeddings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def embed_documents(self, documents):
            return [[0.0] for _ in documents]

    class FakeParser:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeEncoding:
        def encode(self, value):
            return value.split()

    langchain_core = types.ModuleType("langchain_core")
    output_parsers = types.ModuleType("langchain_core.output_parsers")
    output_parsers.PydanticOutputParser = FakeParser
    messages = types.ModuleType("langchain_core.messages")
    messages.HumanMessage = FakeMessage
    messages.SystemMessage = FakeMessage
    langchain_openai = types.ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = FakeChatOpenAI
    langchain_openai.OpenAIEmbeddings = FakeOpenAIEmbeddings
    tiktoken = types.ModuleType("tiktoken")
    tiktoken.encoding_for_model = lambda model: FakeEncoding()
    conversation = types.ModuleType("models.conversation")
    conversation.Structured = type("Structured", (), {})

    for name, module in {
        "langchain_core": langchain_core,
        "langchain_core.output_parsers": output_parsers,
        "langchain_core.messages": messages,
        "langchain_openai": langchain_openai,
        "tiktoken": tiktoken,
        "models.conversation": conversation,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OMI_LLM_PROVIDER_ORDER", "openrouter")

    module_path = Path(__file__).resolve().parents[2] / "utils" / "llm" / "clients.py"
    spec = importlib.util.spec_from_file_location("test_llm_openrouter_clients", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openrouter_client_uses_compatible_endpoint_and_attribution(monkeypatch):
    clients = _load_clients(monkeypatch)
    clients._openrouter_base_url = "https://openrouter.example/v1"
    clients._openrouter_site_url = "https://ella.example"
    clients._openrouter_app_name = "Ella Summary Test"

    result = clients._make_openrouter(temperature=0.2)

    assert result.kwargs["api_key"] == "test-openrouter-key"
    assert result.kwargs["base_url"] == "https://openrouter.example/v1"
    assert result.kwargs["model"] == "google/gemini-3.1-flash-lite"
    assert result.kwargs["temperature"] == 0.2
    assert result.kwargs["default_headers"] == {
        "HTTP-Referer": "https://ella.example",
        "X-Title": "Ella Summary Test",
    }


def test_openrouter_provider_is_skipped_without_key(monkeypatch):
    clients = _load_clients(monkeypatch)
    clients._openrouter_api_key = None

    assert clients._provider_instance("openrouter") is None


def test_provider_chain_keeps_openrouter_primary(monkeypatch):
    clients = _load_clients(monkeypatch)
    clients._provider_order = ["openrouter", "groq"]
    clients._groq_api_key = "test-groq-key"

    primary, fallbacks = clients._build_chain(openrouter_model="google/gemini-3.1-flash-lite")

    assert primary.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert primary.kwargs["model"] == "google/gemini-3.1-flash-lite"
    assert len(fallbacks) == 1
    assert fallbacks[0].kwargs["base_url"] == "https://api.groq.com/openai/v1"
