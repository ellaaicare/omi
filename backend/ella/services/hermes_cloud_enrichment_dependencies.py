"""Production dependency wiring for Hermes Cloud OMI enrichment."""

import database.conversations as conversations_db
from database.ella_provisioning import EllaProvisioningRepository
from ella.routers.canonical_events import PostgresCanonicalEventStore, _get_pool
from ella.services.hermes_cloud_enrichment import HermesCloudEnrichmentService
from ella.services.summary_recovery import apply_summary_update


async def create_default_hermes_cloud_enrichment_service() -> HermesCloudEnrichmentService:
    pool = await _get_pool()
    return HermesCloudEnrichmentService(
        repository=EllaProvisioningRepository(pool),
        event_store=PostgresCanonicalEventStore(),
        conversation_reader=conversations_db.get_conversation,
        summary_applier=apply_summary_update,
    )
