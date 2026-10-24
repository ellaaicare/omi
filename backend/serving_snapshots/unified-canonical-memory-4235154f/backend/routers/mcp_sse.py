"""
Pre-hosted MCP Server via Streamable HTTP Transport

This module provides a streamable HTTP transport for MCP (Model Context Protocol),
allowing clients to connect without running a local MCP server.

Implements the MCP 2025-03-26 Streamable HTTP Transport specification.
"""

import asyncio
import os
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Union, List, Any

from fastapi import APIRouter, HTTPException, Header, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

import database.memories as memories_db
import database.conversations as conversations_db
import database.mcp_api_key as mcp_api_key_db
from ella.services.summary_sanitizer import SummarySanitizationError, sanitize_summary_update
from ella.services.mcp_summary_enrichment import EnrichmentError, enrich_summary, preconditions, CONTRACT as ENRICHMENT_V1, CONTRACT_V2 as ENRICHMENT_V2
from ella.services.mcp_source_report import REPORT_SCHEMA, SourceReportError, transcript_projection
from ella.services.mcp_work_queue import tools as work_tools
from ella.services.mcp_redacted_observation import observe_request, observe_rpc
from ella.services.mcp_current_conversation import (
    CurrentConversationError, TOOL as current_conversation_tool, read_current_conversation,
)
from models.memories import MemoryDB, Memory, MemoryCategory
from models.conversation import CategoryEnum
from utils.llm.memories import identify_category_for_memory

router = APIRouter()

# Store active sessions
active_sessions: dict = {}


class MCPSession:
    """Represents an active MCP session."""

    def __init__(self, session_id: str, user_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self.created_at = datetime.utcnow()
        self.initialized = False


def authenticate_api_key(authorization: Optional[str]) -> Optional[str]:
    """Validate API key from Authorization header and return user_id if valid."""
    if not authorization:
        return None

    # Support both "Bearer <key>" and just "<key>" formats
    token = authorization
    if authorization.startswith("Bearer "):
        token = authorization[7:]

    if not token.startswith("omi_mcp_"):
        return None

    return mcp_api_key_db.get_user_id_by_api_key(token)


# MCP Tool Definitions
MCP_TOOLS = [
    {
        "name": "get_memories",
        "description": "Retrieve a list of memories. A memory is a known fact about the user across multiple domains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": [c.value for c in MemoryCategory]},
                    "description": "Categories to filter by",
                    "default": [],
                },
                "limit": {"type": "integer", "description": "Number of memories to retrieve", "default": 100},
                "offset": {"type": "integer", "description": "Offset for pagination", "default": 0},
            },
        },
    },
    {
        "name": "create_memory",
        "description": "Create a new memory. A memory is a known fact about the user across multiple domains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The content of the memory"},
                "category": {
                    "type": "string",
                    "enum": [c.value for c in MemoryCategory],
                    "description": "The category of the memory",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete a memory by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string", "description": "The ID of the memory to delete"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "edit_memory",
        "description": "Edit a memory's content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "The ID of the memory to edit"},
                "content": {"type": "string", "description": "The new content for the memory"},
            },
            "required": ["memory_id", "content"],
        },
    },
    {
        "name": "get_conversations",
        "description": "Retrieve a list of conversation metadata. To get full transcripts, use get_conversation_by_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Filter after this date (yyyy-mm-dd)"},
                "end_date": {"type": "string", "description": "Filter before this date (yyyy-mm-dd)"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": [c.value for c in CategoryEnum]},
                    "description": "Categories to filter by",
                    "default": [],
                },
                "limit": {"type": "integer", "description": "Number of conversations to retrieve", "default": 20},
                "offset": {"type": "integer", "description": "Offset for pagination", "default": 0},
            },
        },
    },
    {
        "name": "get_conversation_by_id",
        "description": "Retrieve a conversation by ID including each segment of the transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "The ID of the conversation to retrieve"},
                "include_enrichment_preconditions": {
                    "type": "boolean",
                    "description": "Opt in to owner-bound hash/version preconditions for safe summary enrichment.",
                },
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "update_conversation_summary",
        "description": "Update the structured summary of a conversation. Can update title, overview, emoji, and/or category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "The ID of the conversation to update"},
                "title": {"type": "string", "description": "New title for the conversation"},
                "overview": {"type": "string", "description": "New overview/summary text for the conversation"},
                "emoji": {"type": "string", "description": "New emoji for the conversation"},
                "category": {
                    "type": "string",
                    "enum": [c.value for c in CategoryEnum],
                    "description": "New category for the conversation",
                },
                "queue_claim": {
                    "type": "object", "additionalProperties": False,
                    "description": "Optional matching enrichment queue lease; job completion and summary commit are atomic.",
                    "properties": {"job_id": {"type": "string"}, "lease_token": {"type": "string"}, "policy_epoch": {"type": "string", "format": "uuid"}},
                    "required": ["job_id", "lease_token", "policy_epoch"],
                },
                "enrichment": {
                    "type": "object",
                    "description": "Optional safe enrichment mode: atomic hash/version CAS, preserved original and generated results, idempotent operation. No canonical-memory publication.",
                    "additionalProperties": False,
                    "properties": {
                        "contract": {"type": "string", "enum": ["omi-mcp-summary-enrichment-v1"]},
                        "expected_snapshot_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "expected_active_summary_version_id": {"type": ["string", "null"]},
                        "operation_id": {"type": "string", "format": "uuid"},
                        "producer": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
                    },
                    "required": ["contract", "expected_snapshot_sha256", "expected_active_summary_version_id", "operation_id", "producer"],
                },
            },
            "required": ["conversation_id"],
        },
    },
]

# Both optional v2 selectors fail closed in execution; v1 defaults are unchanged.
for _tool in MCP_TOOLS:
    if _tool["name"] == "get_conversation_by_id":
        _tool["inputSchema"]["properties"]["enrichment_contract"] = {
            "type": "string", "enum": [ENRICHMENT_V1, ENRICHMENT_V2],
            "description": "Explicit contract for include_enrichment_preconditions=true; v2 returns exact source text/hash."}
        _tool["inputSchema"]["allOf"] = [{
            "if": {"required": ["enrichment_contract"]},
            "then": {"required": ["include_enrichment_preconditions"],
                     "properties": {"include_enrichment_preconditions": {"const": True}}}}]
    elif _tool["name"] == "update_conversation_summary":
        _props = _tool["inputSchema"]["properties"]
        _props["enrichment"]["properties"]["contract"]["enum"] = [ENRICHMENT_V1, ENRICHMENT_V2]
        _props["source_report"] = REPORT_SCHEMA
        _tool["inputSchema"]["allOf"] = [{
            "if": {"required": ["enrichment"], "properties": {"enrichment": {
                "required": ["contract"], "properties": {"contract": {"const": ENRICHMENT_V2}}}}},
            "then": {"required": ["title", "overview", "source_report"]},
            "else": {"not": {"required": ["source_report"]}}}]

MCP_TOOLS.extend(work_tools())
MCP_TOOLS.append(current_conversation_tool)


THIN_PENDING_DISCOVERY_TEST_OWNER_SHA256 = '040d2202f2fffdc92888395713a03c13fb8a54159c90d3563d391d9789ed060a'


def _thin_pending_discovery_configured():
    return os.getenv('ELLA_THIN_PENDING_DISCOVERY_ENABLED', 'false') == 'true'


def _thin_pending_discovery_enabled(user_id):
    # Test account only. Expanding the cohort requires a separate reviewed patch.
    return (_thin_pending_discovery_configured() and type(user_id) is str
            and hashlib.sha256(user_id.encode()).hexdigest() == THIN_PENDING_DISCOVERY_TEST_OWNER_SHA256)


def _thin_pending_discovery_catalog(user_id):
    return [tool for tool in MCP_TOOLS if tool['name'] != 'get_pending_recordings'
            or _thin_pending_discovery_enabled(user_id)]


if _thin_pending_discovery_configured():
    from ella.services.thin_pending_discovery import tool_definition
    MCP_TOOLS.append(tool_definition())


class ToolExecutionError(Exception):
    """Exception raised when a tool execution fails."""

    def __init__(self, message: str, code: int = -32000):
        self.message = message
        self.code = code
        super().__init__(self.message)


def execute_tool(user_id: str, tool_name: str, arguments: dict, *, thin_authority=None) -> dict:
    """Execute an MCP tool and return the result. Raises ToolExecutionError on failure."""

    if not isinstance(arguments, dict):
        raise ToolExecutionError('invalid_tool_arguments', code=-32602)
    if tool_name == 'get_pending_recordings':
        # No source IDs or owner/scope supplied by caller. Gate before any DB read.
        if not _thin_pending_discovery_enabled(user_id):
            raise ToolExecutionError('thin_pending_discovery_not_enabled', code=-32012)
        try:
            from database._client import db
            from ella.services.thin_pending_discovery import (
                discover, FirestorePendingRepository, execute_for_authenticated_owner)
            if thin_authority is not None:
                if thin_authority.uid != user_id:
                    raise ValueError('thin_discovery_authenticated_owner_changed')
                return discover(thin_authority, arguments, FirestorePendingRepository(db))
            return execute_for_authenticated_owner(db, user_id, arguments, enabled=True)
        except Exception as error:
            raise ToolExecutionError('thin_pending_discovery_unavailable', code=-32012) from error
    if thin_authority is None and tool_name in {'get_current_conversation_by_id','get_conversation_by_id','update_conversation_summary'}:
        cid = arguments.get('conversation_id')
        if isinstance(cid, str) and cid and '/' not in cid:
            from database._client import db
            from ella.services.thin_cloudbot_capture import FIELD, source_ref
            from ella.services.thin_cloudbot_request_authority import require_mcp_request
            stored = source_ref(db, user_id, cid).get()
            binding = (stored.to_dict() or {}).get(FIELD) if stored.exists else None
            if binding is not None:
                if not isinstance(binding,dict):
                    raise ToolExecutionError('Source target unavailable', code=-32000)
                # Owner comes only from authenticated MCP principal, never tool JSON.
                try:
                    thin_authority = require_mcp_request(user_id, binding.get('consent_scope'), db=db)
                except ValueError as error:
                    raise ToolExecutionError('thin_mcp_target_or_operation_hold', code=-32012) from error
    if thin_authority is not None:
        from database._client import db
        from ella.services.thin_cloudbot_mcp import execute_tool as execute_thin_tool
        try:
            return execute_thin_tool(db, thin_authority, tool_name, arguments, CategoryEnum)
        except ValueError as error:
            raise ToolExecutionError('thin_mcp_target_or_operation_hold', code=-32012) from error
    if tool_name == "get_current_conversation_by_id":
        try:
            return read_current_conversation(conversations_db, user_id, arguments)
        except CurrentConversationError as error:
            raise ToolExecutionError(str(error), code=-32012) from error

    if tool_name == "get_memories":
        categories = arguments.get("categories", [])
        limit = arguments.get("limit", 100)
        offset = arguments.get("offset", 0)

        # Validate categories
        valid_categories = []
        for cat in categories:
            try:
                valid_categories.append(MemoryCategory(cat).value)
            except ValueError:
                raise ToolExecutionError(f"Invalid memory category: '{cat}'", code=-32602)

        memories = memories_db.get_memories(user_id, limit, offset, valid_categories)
        # Apply locked content truncation
        for memory in memories:
            if memory.get('is_locked', False):
                content = memory.get('content', '')
                memory['content'] = (content[:70] + '...') if len(content) > 70 else content

        return {"memories": memories}

    elif tool_name == "create_memory":
        content = arguments.get("content")
        if not content:
            raise ToolExecutionError("Content is required")

        # Auto-categorize memories from MCP clients
        category = identify_category_for_memory(content)
        memory = Memory(content=content, category=category)
        memory_db = MemoryDB.from_memory(memory, user_id, None, True)
        memories_db.create_memory(user_id, memory_db.model_dump())

        return {"success": True, "memory": memory_db.model_dump()}

    elif tool_name == "delete_memory":
        memory_id = arguments.get("memory_id")
        if not memory_id:
            raise ToolExecutionError("memory_id is required")

        memories_db.delete_memory(user_id, memory_id)
        return {"success": True}

    elif tool_name == "edit_memory":
        memory_id = arguments.get("memory_id")
        content = arguments.get("content")
        if not memory_id or not content:
            raise ToolExecutionError("memory_id and content are required")

        memories_db.edit_memory(user_id, memory_id, content)
        return {"success": True}

    elif tool_name == "get_conversations":
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")
        categories = arguments.get("categories", [])
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)

        # Parse dates
        start_dt = None
        end_dt = None
        if start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                raise ToolExecutionError(
                    f"Invalid start_date format: '{start_date}'. Expected YYYY-MM-DD.", code=-32602
                )
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise ToolExecutionError(f"Invalid end_date format: '{end_date}'. Expected YYYY-MM-DD.", code=-32602)

        # Validate categories
        valid_categories = []
        for cat in categories:
            try:
                valid_categories.append(CategoryEnum(cat).value)
            except ValueError:
                pass

        conversations = conversations_db.get_conversations(
            user_id,
            limit,
            offset,
            include_discarded=False,
            statuses=["completed"],
            start_date=start_dt,
            end_date=end_dt,
            categories=valid_categories,
        )

        # Simplify conversation data
        simple_conversations = []
        for conv in conversations:
            if conv.get('thin_cloudbot_source_binding') is not None:
                continue
            simple_conversations.append(
                {
                    "id": conv.get("id"),
                    "started_at": conv.get("started_at"),
                    "finished_at": conv.get("finished_at"),
                    "structured": conv.get("structured"),
                    "language": conv.get("language"),
                }
            )

        return {"conversations": simple_conversations}

    elif tool_name in {tool["name"] for tool in work_tools()}:
        from ella.services.mcp_work_queue import WorkError, configured_queue, execute
        try:
            return execute(configured_queue(user_id), tool_name, arguments)
        except WorkError as error:
            raise ToolExecutionError(str(error), code=-32011) from error

    elif tool_name == "get_conversation_by_id":
        conversation_id = arguments.get("conversation_id")
        if not conversation_id:
            raise ToolExecutionError("conversation_id is required")

        selected_contract = arguments.get("enrichment_contract", ENRICHMENT_V1)
        if "enrichment_contract" in arguments and (
                selected_contract not in (ENRICHMENT_V1, ENRICHMENT_V2) or
                arguments.get("include_enrichment_preconditions") is not True or
                set(arguments) != {"conversation_id", "include_enrichment_preconditions", "enrichment_contract"}):
            raise ToolExecutionError("invalid_enrichment_read_contract", code=-32602)
        conversation = conversations_db.get_conversation(user_id, conversation_id)
        if not conversation:
            raise ToolExecutionError("Conversation not found", code=-32001)
        if conversation.get('thin_cloudbot_source_binding') is not None:
            raise ToolExecutionError('thin_exact_target_authority_required', code=-32012)


        if conversation.get('is_locked', False):
            raise ToolExecutionError("Unlimited Plan Required to access this conversation.", code=-32002)

        if "include_enrichment_preconditions" in arguments:
            if type(arguments["include_enrichment_preconditions"]) is not bool:
                raise ToolExecutionError("invalid_enrichment_read_flag", code=-32602)
            if arguments["include_enrichment_preconditions"]:
                try:
                    result = {"conversation": conversation,
                              "enrichment_preconditions": preconditions(user_id, conversation_id, conversation,
                                                                         contract=selected_contract)}
                    if selected_contract == ENRICHMENT_V2:
                        if any(k in conversation and conversation[k] != user_id for k in ("uid", "user_id")) or (
                                "id" in conversation and conversation["id"] != conversation_id):
                            raise EnrichmentError("source_report_identity_mismatch")
                        result["source_transcript"] = transcript_projection(conversation)
                        from ella.services import mcp_synthetic_pending as pending
                        if pending.FIELD in conversation:
                            try:
                                result['pending_synthetic_run'] = pending.project(conversation[pending.FIELD], user_id, conversation_id, datetime.now(timezone.utc))
                            except pending.BindingError as error:
                                raise EnrichmentError(str(error)) from error
                    return result
                except (EnrichmentError, SourceReportError) as error:
                    raise ToolExecutionError(str(error), code=-32010) from error
        return {"conversation": conversation}

    elif tool_name == "update_conversation_summary":
        if "source_report" in arguments and "enrichment" not in arguments:
            raise ToolExecutionError("source_report_requires_safe_enrichment", code=-32010)
        if "queue_claim" in arguments:
            from ella.services.mcp_work_queue import WorkError, configured_queue
            try:
                if "enrichment" not in arguments:
                    raise WorkError("work_claim_requires_safe_enrichment")
                return configured_queue(user_id).enrich(arguments, CategoryEnum)
            except (EnrichmentError, WorkError) as error:
                raise ToolExecutionError(str(error), code=-32010) from error
        # Presence of the opt-in field is decisive: malformed requests must not
        # fall back to the legacy destructive overview path.
        if "enrichment" in arguments:
            try:
                return enrich_summary(conversations_db, user_id, arguments, CategoryEnum)
            except EnrichmentError as error:
                raise ToolExecutionError(str(error), code=-32010) from error
        conversation_id = arguments.get("conversation_id")
        if not conversation_id:
            raise ToolExecutionError("conversation_id is required")

        # Verify conversation exists
        conversation = conversations_db.get_conversation(user_id, conversation_id)
        if not conversation:
            raise ToolExecutionError("Conversation not found", code=-32001)

        if conversation.get('thin_cloudbot_source_binding') is not None:
            raise ToolExecutionError('thin_mcp_safe_target_required', code=-32012)
        try:
            sanitized_update = sanitize_summary_update(
                title=arguments.get("title") if "title" in arguments else None,
                overview=arguments.get("overview") if "overview" in arguments else None,
                emoji=arguments.get("emoji") if "emoji" in arguments else None,
                category=arguments.get("category") if "category" in arguments else None,
            )
        except SummarySanitizationError as e:
            raise ToolExecutionError(f"Unsafe conversation summary: {', '.join(e.violations)}", code=-32602)

        # Build update dict with dot-notation for Firestore nested fields
        update_data = {}
        if "title" in arguments:
            update_data["structured.title"] = sanitized_update.title
        if "overview" in arguments:
            update_data["structured.overview"] = sanitized_update.overview
        if "emoji" in arguments:
            update_data["structured.emoji"] = sanitized_update.emoji
        if "category" in arguments:
            # Validate category
            try:
                CategoryEnum(sanitized_update.category)
            except ValueError:
                raise ToolExecutionError(f"Invalid category: '{sanitized_update.category}'", code=-32602)
            update_data["structured.category"] = sanitized_update.category

        if not update_data:
            raise ToolExecutionError("At least one field (title, overview, emoji, category) must be provided")

        # When overview is updated, clear apps_results so the iOS app
        # falls through to structured.overview in getSummarizedApp().
        # If apps_results[0] exists, the Flutter app shows it under the
        # "Ella AI" app label; clearing it shows structured.overview as
        # a plain "Summary" instead.
        if "overview" in arguments:
            update_data["apps_results"] = []
            update_data["plugins_results"] = []

        conversations_db.update_conversation(user_id, conversation_id, update_data)

        return {
            "success": True,
            "updated_fields": list(update_data.keys()),
            "sanitizer_warnings": sanitized_update.warnings,
        }

    else:
        raise ToolExecutionError(f"Unknown tool: {tool_name}", code=-32601)


def create_mcp_response(id: Any, result: dict) -> dict:
    """Create a JSON-RPC 2.0 response."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def create_mcp_error(id: Any, code: int, message: str) -> dict:
    """Create a JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


@observe_rpc
def handle_mcp_message(
    user_id: str, message: dict, session: Optional[MCPSession] = None, *, thin_authority=None
) -> tuple[Optional[dict], Optional[str]]:
    """
    Process an incoming MCP JSON-RPC message and return a response.
    Returns (response, new_session_id) tuple.
    """
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})
    new_session_id = None

    if method == "initialize":
        # Create a new session
        session_id = str(uuid.uuid4())
        new_session = MCPSession(session_id, user_id)
        new_session.initialized = True
        new_session.thin_scope = thin_authority.scope if thin_authority is not None else None
        active_sessions[session_id] = new_session
        new_session_id = session_id

        return (
            create_mcp_response(
                msg_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "omi-mcp-server", "version": "1.0.0"},
                },
            ),
            new_session_id,
        )

    elif method == "notifications/initialized":
        # This is a notification, no response needed
        return None, None

    elif method == "tools/list":
        return create_mcp_response(msg_id, {"tools": [t for t in _thin_pending_discovery_catalog(user_id) if thin_authority is None or
            t['name'] in ({'get_current_conversation_by_id','get_conversation_by_id','update_conversation_summary'} | ({'get_pending_recordings'} if _thin_pending_discovery_enabled(user_id) else set()))]}), None

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return create_mcp_error(msg_id, -32602, "Tool name is required"), None

        try:
            result = execute_tool(user_id, tool_name, arguments, thin_authority=thin_authority)
        except ToolExecutionError as e:
            return create_mcp_error(msg_id, e.code, e.message), None

        return (
            create_mcp_response(
                msg_id, {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]}
            ),
            None,
        )

    elif method == "ping":
        return create_mcp_response(msg_id, {}), None

    else:
        return create_mcp_error(msg_id, -32601, f"Method not found: {method}"), None


@router.get("/authorize", tags=["mcp"])
async def mcp_authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
):
    """OAuth authorize endpoint."""
    if client_id != "omi":
        raise HTTPException(status_code=400, detail="Invalid client_id")

    redirect_url = f"{redirect_uri}?code=omi"
    if state:
        redirect_url += f"&state={state}"

    return Response(status_code=302, headers={"Location": redirect_url})


@router.post("/token", tags=["mcp"])
async def mcp_token(request: Request):
    """OAuth token endpoint."""
    try:
        form_data = await request.form()
        client_secret = form_data.get("client_secret")
        client_id = form_data.get("client_id")
    except Exception:
        try:
            body = await request.json()
            client_secret = body.get("client_secret")
            client_id = body.get("client_id")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid request body")

    if not client_secret:
        raise HTTPException(status_code=400, detail="client_secret is required")

    if client_id != "omi":
        raise HTTPException(status_code=400, detail="Invalid client_id")

    user_id = authenticate_api_key(client_secret)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return {
        "access_token": client_secret,
        "token_type": "Bearer",
    }


@router.post("/v1/mcp/sse", tags=["mcp"])
@observe_request
async def mcp_streamable_http(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    mcp_session_id: Optional[str] = Header(None, alias="Mcp-Session-Id"),
    accept: Optional[str] = Header(None, alias="Accept"),
):
    """
    Streamable HTTP Transport endpoint for MCP clients.

    This implements the MCP 2025-03-26 Streamable HTTP Transport specification.

    - POST JSON-RPC messages to this endpoint
    - Responses are returned as SSE stream or JSON depending on Accept header
    - Session ID is returned in Mcp-Session-Id header after initialization
    """
    # Authenticate
    user_id = authenticate_api_key(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing API key. Provide via Authorization header.")

    from ella.services.thin_cloudbot_request_authority import parse_scope, require_mcp_request
    try:
        thin_scope = parse_scope(request.headers)
        thin_authority = require_mcp_request(user_id, thin_scope)
    except ValueError as error:
        raise HTTPException(status_code=409, detail={'code':'thin_mcp_consent_required'}) from error
    # Parse request body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Get session if provided
    session = None
    if mcp_session_id and mcp_session_id in active_sessions:
        session = active_sessions[mcp_session_id]
        if getattr(session, 'thin_scope', None) != thin_scope:
            raise HTTPException(status_code=409, detail={'code':'thin_mcp_session_target_changed'})
        # Verify session belongs to this user
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to this user")

    # Handle batch requests (array of messages)
    messages = body if isinstance(body, list) else [body]

    # Check if all messages are notifications/responses (no id)
    all_notifications = all(msg.get("id") is None for msg in messages)

    if all_notifications:
        # Process notifications without response
        for msg in messages:
            handle_mcp_message(user_id, msg, session, thin_authority=thin_authority)
        return Response(status_code=202)

    # Process messages and collect responses
    responses = []
    new_session_id = None

    for msg in messages:
        response, session_id = handle_mcp_message(user_id, msg, session, thin_authority=thin_authority)
        if session_id:
            new_session_id = session_id
        if response:
            responses.append(response)

    # Prepare headers
    headers = {}
    if new_session_id:
        headers["Mcp-Session-Id"] = new_session_id

    # Check if client accepts SSE
    wants_sse = accept and "text/event-stream" in accept

    if wants_sse:
        # Return as SSE stream
        async def event_generator():
            for resp in responses:
                yield f"event: message\ndata: {json.dumps(resp, default=str)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    else:
        # Return as JSON
        if len(responses) == 1:
            return JSONResponse(content=responses[0], headers=headers)
        else:
            return JSONResponse(content=responses, headers=headers)


@router.get("/v1/mcp/sse", tags=["mcp"])
async def mcp_sse_get(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    mcp_session_id: Optional[str] = Header(None, alias="Mcp-Session-Id"),
):
    """
    SSE endpoint for server-initiated messages (optional).

    Clients can GET this endpoint to listen for server-initiated notifications.
    This is optional per the MCP spec and mainly used for long-polling scenarios.
    """
    # Authenticate
    user_id = authenticate_api_key(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing API key. Provide via Authorization header.")

    # For backwards compatibility, also support the old SSE flow
    # Return an empty SSE stream that just sends keepalives
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                yield f"event: ping\ndata: {{}}\n\n"
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            # Normal cancellation when client disconnects
            pass
        except Exception as e:
            logging.warning(f"MCP SSE event generator error: {e}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.delete("/v1/mcp/sse", tags=["mcp"])
async def mcp_delete_session(
    request: Request,
    mcp_session_id: Optional[str] = Header(None, alias="Mcp-Session-Id"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """
    Delete/terminate an MCP session.
    """
    user_id = authenticate_api_key(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    if not mcp_session_id:
        raise HTTPException(status_code=400, detail="Mcp-Session-Id header required")

    if mcp_session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = active_sessions[mcp_session_id]
    if session.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this session")

    from ella.services.thin_cloudbot_request_authority import parse_scope
    try:
        scope = parse_scope(request.headers)
    except ValueError as error:
        raise HTTPException(status_code=409, detail={'code':'thin_mcp_cleanup_target_invalid'}) from error
    if getattr(session, 'thin_scope', None) != scope:
        raise HTTPException(status_code=409, detail={'code':'thin_mcp_cleanup_target_changed'})
    # Exact authenticated owner+scope cleanup remains allowed after revoke.
    # Delete the session
    del active_sessions[mcp_session_id]
    return Response(status_code=204)


@router.get("/v1/mcp/sse/info", tags=["mcp"])
async def mcp_sse_info(request: Request):
    """
    Get information about the pre-hosted MCP server.
    """
    base_url = str(request.base_url).rstrip("/")
    return {
        "endpoint": "/v1/mcp/sse",
        "transport": "streamable-http",
        "protocol_version": "2025-03-26",
        "authentication": {
            "methods": ["api_key"],
            "api_key": {"header": "Authorization", "format": "Bearer <api_key>"},
        },
        "instructions": {
            "step1": "Create an MCP API key in the Omi app (Settings > Developer > MCP)",
            "step2": f"Set Server URL to: {base_url}/v1/mcp/sse",
            "step3": "Set Authorization header to your key",
        },
    }
