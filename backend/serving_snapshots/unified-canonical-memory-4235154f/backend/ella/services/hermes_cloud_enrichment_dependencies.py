"""Production dependency wiring for Hermes Cloud OMI enrichment."""

import database.conversations as conversations_db
from database.ella_provisioning import EllaProvisioningRepository
from ella.routers.canonical_events import PostgresCanonicalEventStore, _get_pool
from ella.services.hermes_cloud_enrichment import HermesCloudEnrichmentService
from ella.services.memory_artwork import enqueue_after_terminal_enrichment
from ella.services.summary_recovery import apply_summary_update


async def create_default_hermes_cloud_enrichment_service() -> HermesCloudEnrichmentService:
    pool = await _get_pool()
    repository = EllaProvisioningRepository(pool)
    cloud = HermesCloudEnrichmentService(
        repository=repository,
        event_store=PostgresCanonicalEventStore(),
        conversation_reader=conversations_db.get_conversation,
        summary_applier=apply_summary_update,
        terminal_enrichment_handler=enqueue_after_terminal_enrichment,
    )

    from ella.services.retained_completion_enrichment import (
        CompletionEnrichmentDispatcher, create_retained_completion_service)
    from ella.services.normal_account_routing import admitted_subject
    selected = admitted_subject
    retained = create_retained_completion_service(repository=repository,
        read_conversation=conversations_db.get_conversation,
        update_conversation=conversations_db.update_conversation_with_builder, selected=selected)
    return CompletionEnrichmentDispatcher(cloud=cloud, retained=retained, selected=selected, repository=repository)
