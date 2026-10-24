"""
Ella Services - Business logic and external integrations.

Services:
- n8n_client: HTTP client for n8n webhook calls
- grok_pipeline: Grok V2V voice pipeline logic

During migration, these import from legacy locations.
"""

# Import from legacy locations during migration
try:
    from integrations.pipecat.services.n8n_client import N8NClient
except ImportError:
    N8NClient = None

try:
    from integrations.pipecat.pipeline.grok_v2v_pipeline import (
        GrokVoicePipeline,
        run_grok_v2v_session,
    )
except ImportError:
    GrokVoicePipeline = None
    run_grok_v2v_session = None

__all__ = [
    'N8NClient',
    'GrokVoicePipeline',
    'run_grok_v2v_session',
]
