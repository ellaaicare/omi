from datetime import datetime
from typing import Optional
import uuid

import database.users as users_db
import database.action_items as action_items_db
import database.task_sync as task_sync_db
from utils.notifications import send_apple_reminders_sync_push

TASK_SYNC_AMBIGUOUS_REASON = "task_sync_outbound_ambiguous"


def _ambiguous_task_sync_result(platform: str) -> dict:
    return {
        "synced": False,
        "platform": platform,
        "reason": TASK_SYNC_AMBIGUOUS_REASON,
        "receipt_state": task_sync_db.TASK_SYNC_OUTBOUND_STARTED,
        "automatic_retry_safe": False,
        "operator_reconciliation_required": True,
    }


async def auto_sync_action_item(uid: str, action_item: dict, idempotency_key: Optional[str] = None) -> dict:
    """
    Auto-sync a single action item to user's default integration.

    Args:
        uid: User ID
        action_item: Dict containing at minimum 'id' and 'description'

    Returns:
        dict: {"synced": bool, "platform": str, "external_task_id": str, "error": str}
    """
    try:
        default_app = users_db.get_default_task_integration(uid)
        if not default_app:
            return {"synced": False, "reason": "no_default_integration"}

        integration = users_db.get_task_integration(uid, default_app)
        if not integration:
            return {"synced": False, "reason": "integration_not_found"}

        if not integration.get("connected"):
            return {"synced": False, "reason": "integration_not_connected"}

        claim_token = None
        if idempotency_key:
            claim_token = str(uuid.uuid4())
            claim = task_sync_db.claim_task_sync(
                uid,
                idempotency_key,
                action_item['id'],
                default_app,
                claim_token,
            )
            if claim.get('outcome') == 'completed':
                return claim.get('result') or {"synced": False, "reason": "completed_without_result"}
            if claim.get('outcome') == 'ambiguous':
                return _ambiguous_task_sync_result(default_app)
            if claim.get('outcome') != 'claimed':
                return {"synced": False, "platform": default_app, "reason": "sync_in_progress"}

        # Route to appropriate handler
        if default_app == "apple_reminders":
            result = _sync_to_apple_reminders(uid, action_item, idempotency_key, claim_token)
        else:
            result = await _sync_to_cloud_service(
                uid,
                default_app,
                integration,
                action_item,
                idempotency_key,
                claim_token,
            )

        if not idempotency_key:
            return result
        if result.get('reason') == TASK_SYNC_AMBIGUOUS_REASON:
            return result

        completion = task_sync_db.complete_task_sync(
            uid,
            idempotency_key,
            action_item['id'],
            default_app,
            claim_token,
            result,
        )
        if completion.get('outcome') == 'completed':
            return completion.get('result') or result
        return {"synced": False, "platform": default_app, "reason": "stale_sync_claim"}

    except Exception as e:
        print(f"Auto-sync failed for user {uid}: {e}")
        return {"synced": False, "error": str(e)}


async def _sync_to_cloud_service(
    uid: str,
    app_key: str,
    integration: dict,
    action_item: dict,
    idempotency_key: Optional[str] = None,
    claim_token: Optional[str] = None,
) -> dict:
    """Create task in external service using existing task_integrations logic."""
    from routers.task_integrations import _create_task_internal

    result = await _create_task_internal(
        uid=uid,
        app_key=app_key,
        integration=integration,
        title=action_item["description"],
        due_date=action_item.get("due_at"),
        idempotency_key=idempotency_key,
        action_item_id=action_item['id'],
        sync_claim_token=claim_token,
    )

    if result.get("success"):
        # Mark action item as exported
        action_items_db.update_action_item(
            uid,
            action_item["id"],
            {
                "exported": True,
                "export_platform": app_key,
                "export_date": datetime.utcnow(),
            },
        )
        return {"synced": True, "platform": app_key, "external_task_id": result.get("external_task_id")}

    if result.get("error_code") == "ambiguous_outbound":
        return _ambiguous_task_sync_result(app_key)
    return {"synced": False, "platform": app_key, "error": result.get("error")}


def _sync_to_apple_reminders(
    uid: str,
    action_item: dict,
    idempotency_key: Optional[str] = None,
    claim_token: Optional[str] = None,
) -> dict:
    """Send silent push to device for Apple Reminders."""
    if idempotency_key:
        boundary = task_sync_db.begin_task_sync_egress(
            uid,
            idempotency_key,
            action_item['id'],
            'apple_reminders',
            claim_token,
        )
        if boundary.get('outcome') == 'completed':
            return boundary.get('result') or {"synced": False, "reason": "completed_without_result"}
        if boundary.get('outcome') == 'ambiguous':
            return _ambiguous_task_sync_result("apple_reminders")
        if boundary.get('outcome') != task_sync_db.TASK_SYNC_OUTBOUND_STARTED:
            return {"synced": False, "platform": "apple_reminders", "reason": "stale_sync_claim"}

    try:
        success = send_apple_reminders_sync_push(
            user_id=uid,
            action_item_id=action_item["id"],
            description=action_item["description"],
            due_at=action_item.get("due_at"),
            idempotency_key=idempotency_key,
        )
    except Exception:
        if idempotency_key:
            return _ambiguous_task_sync_result("apple_reminders")
        raise

    return {"synced": success, "platform": "apple_reminders", "pending_device": True}


async def auto_sync_action_items_batch(
    uid: str,
    action_items: list,
    idempotency_key: Optional[str] = None,
) -> list:
    """
    Batch sync multiple action items.

    Args:
        uid: User ID
        action_items: List of action item dicts, each containing at minimum 'id' and 'description'

    Returns:
        list: Results for each action item
    """
    results = []
    for item in action_items:
        item_idempotency_key = (
            task_sync_db.task_sync_operation_key(uid, idempotency_key, item['id']) if idempotency_key else None
        )
        result = await auto_sync_action_item(uid, item, idempotency_key=item_idempotency_key)
        results.append(result)
    return results
