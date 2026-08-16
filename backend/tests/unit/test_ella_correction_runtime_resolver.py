import ast
from pathlib import Path
from typing import Optional

import pytest


class _ProvisioningError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _load_self_hosted_target_mode():
    source_path = Path(__file__).resolve().parents[2] / "ella" / "services" / "runtime_resolver.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_self_hosted_target_mode"
    )
    namespace = {
        "Optional": Optional,
        "ProvisioningError": _ProvisioningError,
        "SELF_HOSTED_RUNTIME_TARGET_MODES": {"hermes-chat", "hermes-voice"},
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_self_hosted_target_mode"]


def test_self_hosted_correction_transcript_mode_maps_to_hermes_chat():
    resolve_mode = _load_self_hosted_target_mode()

    assert resolve_mode("hermes-cloud-transcript") == "hermes-chat"
    assert resolve_mode("hermes-cloud-chat") == "hermes-chat"
    assert resolve_mode("hermes-cloud-voice") == "hermes-voice"
    with pytest.raises(_ProvisioningError, match="self_hosted_runtime_target_mode_required"):
        resolve_mode("unsupported")
