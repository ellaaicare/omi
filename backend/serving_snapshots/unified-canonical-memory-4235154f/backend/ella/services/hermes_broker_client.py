"""Private first-party Hermes webhook-broker client (ordinary prototype).

Submits one admit request to the merged Ella broker and waits for a terminal
result via the companion GET contract. Never calls unpublished Hermes
`/v1/responses` and never falls back to Plato/Mini.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import httpx

from ella.services.hermes_broker_prototype import (
    ADMIT_PATH,
    LOOPBACK_BROKER_BASE_URL,
    RESULT_PATH_PREFIX,
    STOCK_CALLBACK_SOURCE,
    STOCK_DELIVERY_PLATFORM,
    STOCK_WEBHOOK_ROUTE,
    HermesBrokerPrototypeConfig,
    resolve_service_token,
)
from ella.services.runtime_errors import ProvisioningError

MAX_ADMISSION_BODY_BYTES = 65_536
MAX_RESULT_BODY_BYTES = 262_144
DIAGNOSTIC_STAGES = frozenset(
    {
        "broker_request",
        "broker_dispatch",
        "broker_callback",
        "broker_writeback",
    }
)
DIAGNOSTIC_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


@dataclass(frozen=True)
class BrokerTerminalTurn:
    """Mapped terminal chat/enrichment result for existing OMI consumers."""

    text: str
    request_id: str
    correlation_id: str
    response_id: str
    usage: dict[str, int]
    model: str
    duplicate: bool
    diagnostic: Optional[dict[str, Any]] = None


class HermesBrokerClient:
    """Client pinned to HTTPS:443 or the one synthetic host-loopback route."""

    def __init__(
        self,
        config: HermesBrokerPrototypeConfig,
        *,
        http_client_factory=None,
        sleep=asyncio.sleep,
        clock=time.time,
    ):
        self.config = config
        self.http_client_factory = http_client_factory or (
            lambda timeout: httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
        )
        self.sleep = sleep
        self.clock = clock

    def _headers(self) -> dict[str, str]:
        token = resolve_service_token(self.config.service_token_ref)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": self.config.allowed_host,
        }

    def _assert_url(self, url: str, *, expected_path_prefix: str) -> None:
        parsed = urlparse(url)
        secure_remote = (
            parsed.scheme == "https" and parsed.hostname == self.config.allowed_host and parsed.port in (None, 443)
        )
        pinned_loopback = (
            self.config.base_url == LOOPBACK_BROKER_BASE_URL
            and self.config.allowed_host == "127.0.0.1"
            and parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 18097
        )
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not (secure_remote or pinned_loopback)
            or not parsed.path.startswith(expected_path_prefix)
        ):
            raise ProvisioningError(
                "hermes_broker_prototype_url_not_allowlisted",
                retryable=False,
            )

    async def admit(
        self,
        *,
        account_id: str,
        profile_id: str,
        runtime_binding_ref: str,
        lane: str,
        source_event_id: str,
        consent_epoch: str,
        payload: Mapping[str, Any],
        deadline_at: int,
        pass_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "account_id": account_id,
            "profile_id": profile_id,
            "runtime_binding_ref": runtime_binding_ref,
            "lane": lane,
            "source_event_id": source_event_id,
            "consent_epoch": consent_epoch,
            "payload": dict(payload),
            "deadline_at": int(deadline_at),
            "delivery_platform": STOCK_DELIVERY_PLATFORM,
            "callback_source": STOCK_CALLBACK_SOURCE,
            "webhook_route": STOCK_WEBHOOK_ROUTE,
        }
        if pass_kind is not None:
            body["pass_kind"] = pass_kind
        url = f"{self.config.base_url}{ADMIT_PATH}"
        self._assert_url(url, expected_path_prefix=ADMIT_PATH)
        try:
            async with self.http_client_factory(timeout=min(30.0, self.config.poll_timeout_seconds)) as client:
                response = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException as exc:
            raise ProvisioningError(
                "hermes_broker_prototype_admit_timeout",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProvisioningError(
                "hermes_broker_prototype_unavailable",
                retryable=True,
            ) from exc
        admission = self._parse_json(
            response,
            max_bytes=MAX_ADMISSION_BODY_BYTES,
            invalid_code="hermes_broker_prototype_admit_invalid",
        )
        if (
            admission.get("delivery_platform") != STOCK_DELIVERY_PLATFORM
            or admission.get("callback_source") != STOCK_CALLBACK_SOURCE
            or admission.get("terminal_proof") is not False
        ):
            raise ProvisioningError(
                "hermes_broker_prototype_stock_semantics_mismatch",
                retryable=False,
            )
        return admission

    async def wait_for_terminal(
        self,
        *,
        request_id: str,
        account_id: str,
        profile_id: str,
        lane: str,
        correlation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Poll the companion GET contract until terminal, error, or timeout."""
        if not request_id or "/" in request_id or ".." in request_id:
            raise ProvisioningError(
                "hermes_broker_prototype_request_id_invalid",
                retryable=False,
            )
        expected_lane = str(lane or "").strip()
        if not expected_lane:
            raise ProvisioningError(
                "hermes_broker_prototype_lane_required",
                retryable=False,
            )
        url = f"{self.config.base_url}{RESULT_PATH_PREFIX}{request_id}"
        self._assert_url(url, expected_path_prefix=RESULT_PATH_PREFIX)
        deadline = float(self.clock()) + float(self.config.poll_timeout_seconds)
        last: Optional[dict[str, Any]] = None
        while float(self.clock()) < deadline:
            try:
                async with self.http_client_factory(timeout=min(15.0, self.config.poll_timeout_seconds)) as client:
                    response = await client.get(
                        url,
                        headers=self._headers(),
                        params={
                            "account_id": account_id,
                            "profile_id": profile_id,
                        },
                    )
            except httpx.TimeoutException as exc:
                raise ProvisioningError(
                    "hermes_broker_prototype_poll_timeout",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProvisioningError(
                    "hermes_broker_prototype_unavailable",
                    retryable=True,
                ) from exc
            if response.status_code == 404:
                # Companion endpoint missing on deployed broker.
                raise ProvisioningError(
                    "hermes_broker_prototype_result_endpoint_missing",
                    retryable=False,
                    detail={
                        "companion": (
                            "GET /v1/ella/internal/hermes-webhook-broker/stock-canary/requests/"
                            "{request_id}?account_id=&profile_id= "
                            "must return owner-pinned stock_best_effort_v1 state "
                            "with terminal_proof=false (service auth)."
                        ),
                    },
                )
            last = self._parse_json(
                response,
                max_bytes=MAX_RESULT_BODY_BYTES,
                invalid_code="hermes_broker_prototype_result_invalid",
            )
            self._assert_terminal_envelope(
                last,
                request_id=request_id,
                account_id=account_id,
                profile_id=profile_id,
                correlation_id=correlation_id,
                lane=expected_lane,
                require_terminal_identity=False,
            )
            if last.get("callback_contract") != "stock_best_effort_v1" or last.get("terminal_proof") is not False:
                raise ProvisioningError(
                    "hermes_broker_prototype_stock_semantics_mismatch",
                    retryable=False,
                )
            self._validated_diagnostic(last)
            status = str(last.get("status") or "").strip()
            if not status:
                raise ProvisioningError(
                    "hermes_broker_prototype_status_omitted",
                    retryable=False,
                )
            if status == "writeback_completed":
                # Terminal success must carry full owner/request/correlation/lane pins.
                self._assert_terminal_envelope(
                    last,
                    request_id=request_id,
                    account_id=account_id,
                    profile_id=profile_id,
                    correlation_id=correlation_id,
                    lane=expected_lane,
                    require_terminal_identity=True,
                )
                return last
            if status in {
                "quarantined",
                "blocked",
                "expired",
                "writeback_blocked",
            }:
                self._assert_terminal_envelope(
                    last,
                    request_id=request_id,
                    account_id=account_id,
                    profile_id=profile_id,
                    correlation_id=correlation_id,
                    lane=expected_lane,
                    require_terminal_identity=True,
                )
                raise ProvisioningError(
                    f"hermes_broker_prototype_{status}",
                    retryable=False,
                )
            if status not in {
                "pending",
                "dispatching",
                "awaiting_callback",
                "callback_accepted",
                "writeback_pending",
                "writeback_retryable",
            }:
                raise ProvisioningError(
                    "hermes_broker_prototype_stock_status_invalid",
                    retryable=False,
                )
            await self.sleep(self.config.poll_interval_seconds)
        raise ProvisioningError(
            "hermes_broker_prototype_wait_timeout",
            retryable=True,
            detail={"last_status": (last or {}).get("status")},
        )

    async def run_chat_turn(
        self,
        *,
        account_id: str,
        profile_id: str,
        runtime_binding_ref: str,
        consent_epoch: str,
        message: str,
        session_key: str,
        session_id: str,
        source_event_id: str,
        expected_model: str,
        context: Optional[dict[str, Any]] = None,
    ) -> BrokerTerminalTurn:
        now = int(self.clock())
        payload: dict[str, Any] = {
            "message": message,
            "session_key": session_key,
            "session_id": session_id,
            "canonical_user_event_id": source_event_id,
        }
        if context is not None:
            payload["context"] = context
        admission = await self.admit(
            account_id=account_id,
            profile_id=profile_id,
            runtime_binding_ref=runtime_binding_ref,
            lane="chat_turn",
            source_event_id=source_event_id,
            consent_epoch=consent_epoch,
            payload=payload,
            deadline_at=now + int(self.config.deadline_seconds),
        )
        if str(admission.get("status")) == "skipped_mini_retained":
            raise ProvisioningError(
                "hermes_broker_prototype_skipped_mini",
                retryable=False,
            )
        request_id = str(admission.get("request_id") or "").strip()
        correlation_id = str(admission.get("correlation_id") or "").strip()
        if not request_id or not correlation_id:
            raise ProvisioningError(
                "hermes_broker_prototype_admit_invalid",
                retryable=True,
            )
        terminal = await self.wait_for_terminal(
            request_id=request_id,
            account_id=account_id,
            profile_id=profile_id,
            correlation_id=correlation_id,
            lane="chat_turn",
        )
        return self._map_chat_result(
            terminal,
            request_id=request_id,
            correlation_id=correlation_id,
            expected_model=expected_model,
            session_key=session_key,
            session_id=session_id,
            source_event_id=source_event_id,
            admission_duplicate=bool(admission.get("duplicate")),
        )

    async def run_transcript_user_summary(
        self,
        *,
        account_id: str,
        profile_id: str,
        runtime_binding_ref: str,
        consent_epoch: str,
        source_event_id: str,
        transcript_segments: list[dict[str, Any]],
        expected_model: str,
        existing_summary: Optional[dict[str, Any]] = None,
    ) -> BrokerTerminalTurn:
        now = int(self.clock())
        payload: dict[str, Any] = {"transcript_segments": transcript_segments}
        if existing_summary is not None:
            payload["existing_summary"] = existing_summary
        admission = await self.admit(
            account_id=account_id,
            profile_id=profile_id,
            runtime_binding_ref=runtime_binding_ref,
            lane="transcript_summary_enrichment",
            source_event_id=source_event_id,
            consent_epoch=consent_epoch,
            payload=payload,
            deadline_at=now + int(self.config.deadline_seconds),
            pass_kind="user_summary",
        )
        request_id = str(admission.get("request_id") or "").strip()
        correlation_id = str(admission.get("correlation_id") or "").strip()
        if not request_id or not correlation_id:
            raise ProvisioningError(
                "hermes_broker_prototype_admit_invalid",
                retryable=True,
            )
        terminal = await self.wait_for_terminal(
            request_id=request_id,
            account_id=account_id,
            profile_id=profile_id,
            correlation_id=correlation_id,
            lane="transcript_summary_enrichment",
        )
        return self._map_summary_result(
            terminal,
            request_id=request_id,
            correlation_id=correlation_id,
            expected_model=expected_model,
            admission_duplicate=bool(admission.get("duplicate")),
        )

    def _map_chat_result(
        self,
        terminal: Mapping[str, Any],
        *,
        request_id: str,
        correlation_id: str,
        expected_model: str,
        session_key: str,
        session_id: str,
        source_event_id: str,
        admission_duplicate: bool,
    ) -> BrokerTerminalTurn:
        outcome = terminal.get("outcome")
        if outcome is None or str(outcome).strip() == "":
            raise ProvisioningError(
                "hermes_broker_prototype_outcome_omitted",
                retryable=False,
            )
        if str(outcome) == "error":
            raise ProvisioningError(
                "hermes_broker_prototype_provider_error",
                retryable=True,
            )
        if str(outcome) != "success":
            raise ProvisioningError(
                "hermes_broker_prototype_outcome_invalid",
                retryable=False,
            )
        result = terminal.get("result")
        if not isinstance(result, dict):
            raise ProvisioningError(
                "hermes_broker_prototype_result_invalid",
                retryable=True,
            )
        # Exact lane-result identity is required (no optional omission).
        for key, expected in (
            ("session_key", session_key),
            ("session_id", session_id),
            ("canonical_user_event_id", source_event_id),
        ):
            if key not in result or result.get(key) is None or str(result.get(key)).strip() == "":
                raise ProvisioningError(
                    "hermes_broker_prototype_result_identity_omitted",
                    retryable=False,
                )
            if str(result.get(key)) != str(expected):
                raise ProvisioningError(
                    "hermes_broker_prototype_result_identity_mismatch",
                    retryable=False,
                )
        answer = result.get("answer")
        if answer is None or str(answer).strip() == "":
            raise ProvisioningError(
                "hermes_broker_prototype_empty_answer",
                retryable=True,
            )
        answer = str(answer).strip()
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        try:
            normalized_usage = {
                "input_tokens": max(0, int(usage.get("input_tokens", 0))),
                "output_tokens": max(0, int(usage.get("output_tokens", 0))),
            }
        except (TypeError, ValueError) as exc:
            raise ProvisioningError(
                "hermes_broker_prototype_usage_invalid",
                retryable=False,
            ) from exc
        model = str(result.get("model") or expected_model).strip() or expected_model
        response_id = str(
            result.get("response_id") or result.get("canonical_assistant_event_id") or f"broker:{request_id}"
        ).strip()
        return BrokerTerminalTurn(
            text=answer,
            request_id=request_id,
            correlation_id=correlation_id,
            response_id=response_id,
            usage=normalized_usage,
            model=model,
            duplicate=bool(terminal.get("duplicate")) or admission_duplicate,
            diagnostic=self._validated_diagnostic(terminal),
        )

    def _map_summary_result(
        self,
        terminal: Mapping[str, Any],
        *,
        request_id: str,
        correlation_id: str,
        expected_model: str,
        admission_duplicate: bool,
    ) -> BrokerTerminalTurn:
        outcome = terminal.get("outcome")
        if outcome is None or str(outcome).strip() == "":
            raise ProvisioningError(
                "hermes_broker_prototype_outcome_omitted",
                retryable=False,
            )
        if str(outcome) == "error":
            raise ProvisioningError(
                "hermes_broker_prototype_provider_error",
                retryable=True,
            )
        if str(outcome) != "success":
            raise ProvisioningError(
                "hermes_broker_prototype_outcome_invalid",
                retryable=False,
            )
        result = terminal.get("result")
        if not isinstance(result, dict) or not result:
            raise ProvisioningError(
                "hermes_broker_prototype_result_invalid",
                retryable=True,
            )
        # Map user-summary envelope into a single text payload for existing
        # enrichment validators (they re-parse JSON from the model text).
        text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not text or text == "{}":
            raise ProvisioningError(
                "hermes_broker_prototype_empty_answer",
                retryable=True,
            )
        return BrokerTerminalTurn(
            text=text,
            request_id=request_id,
            correlation_id=correlation_id,
            response_id=str(result.get("response_id") or f"broker:{request_id}"),
            usage={"input_tokens": 0, "output_tokens": 0},
            model=expected_model,
            duplicate=bool(terminal.get("duplicate")) or admission_duplicate,
            diagnostic=self._validated_diagnostic(terminal),
        )

    @staticmethod
    def _validated_diagnostic(body: Mapping[str, Any]) -> dict[str, Any]:
        diagnostic = body.get("diagnostic")
        if not isinstance(diagnostic, dict) or set(diagnostic) != {
            "stage",
            "reason",
            "generation",
        }:
            raise ProvisioningError(
                "hermes_broker_prototype_diagnostic_invalid",
                retryable=False,
            )
        stage = diagnostic.get("stage")
        reason = diagnostic.get("reason")
        generation = diagnostic.get("generation")
        if (
            stage not in DIAGNOSTIC_STAGES
            or not isinstance(reason, str)
            or DIAGNOSTIC_REASON_RE.fullmatch(reason) is None
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ProvisioningError(
                "hermes_broker_prototype_diagnostic_invalid",
                retryable=False,
            )
        return {
            "stage": stage,
            "reason": reason,
            "generation": generation,
        }

    @staticmethod
    def _require_exact_field(
        body: Mapping[str, Any],
        key: str,
        expected: str,
        *,
        omitted_code: str,
        mismatch_code: str,
    ) -> None:
        if key not in body or body.get(key) is None or str(body.get(key)).strip() == "":
            raise ProvisioningError(omitted_code, retryable=False)
        if str(body.get(key)) != str(expected):
            raise ProvisioningError(mismatch_code, retryable=False)

    @classmethod
    def _assert_terminal_envelope(
        cls,
        body: Mapping[str, Any],
        *,
        request_id: str,
        account_id: str,
        profile_id: str,
        correlation_id: Optional[str],
        lane: str,
        require_terminal_identity: bool,
    ) -> None:
        """Owner/request/lane pins are mandatory on terminal; omissions fail closed."""
        if require_terminal_identity:
            cls._require_exact_field(
                body,
                "request_id",
                request_id,
                omitted_code="hermes_broker_prototype_request_id_omitted",
                mismatch_code="hermes_broker_prototype_request_id_mismatch",
            )
            cls._require_exact_field(
                body,
                "account_id",
                account_id,
                omitted_code="hermes_broker_prototype_account_omitted",
                mismatch_code="hermes_broker_prototype_cross_account_result",
            )
            cls._require_exact_field(
                body,
                "profile_id",
                profile_id,
                omitted_code="hermes_broker_prototype_profile_omitted",
                mismatch_code="hermes_broker_prototype_cross_profile_result",
            )
            if not correlation_id:
                raise ProvisioningError(
                    "hermes_broker_prototype_correlation_required",
                    retryable=False,
                )
            cls._require_exact_field(
                body,
                "correlation_id",
                correlation_id,
                omitted_code="hermes_broker_prototype_correlation_omitted",
                mismatch_code="hermes_broker_prototype_correlation_mismatch",
            )
            cls._require_exact_field(
                body,
                "lane",
                lane,
                omitted_code="hermes_broker_prototype_lane_omitted",
                mismatch_code="hermes_broker_prototype_cross_lane_result",
            )
            return
        # Non-terminal polls still reject cross-owner / wrong-request / wrong-lane when present.
        if "request_id" in body and body.get("request_id") is not None:
            if str(body.get("request_id")).strip() and str(body.get("request_id")) != str(request_id):
                raise ProvisioningError(
                    "hermes_broker_prototype_request_id_mismatch",
                    retryable=False,
                )
        if "account_id" in body and body.get("account_id") is not None:
            if str(body.get("account_id")).strip() and str(body.get("account_id")) != str(account_id):
                raise ProvisioningError(
                    "hermes_broker_prototype_cross_account_result",
                    retryable=False,
                )
        if "profile_id" in body and body.get("profile_id") is not None:
            if str(body.get("profile_id")).strip() and str(body.get("profile_id")) != str(profile_id):
                raise ProvisioningError(
                    "hermes_broker_prototype_cross_profile_result",
                    retryable=False,
                )
        if correlation_id and "correlation_id" in body and body.get("correlation_id") is not None:
            if str(body.get("correlation_id")).strip() and str(body.get("correlation_id")) != str(correlation_id):
                raise ProvisioningError(
                    "hermes_broker_prototype_correlation_mismatch",
                    retryable=False,
                )
        if "lane" in body and body.get("lane") is not None:
            if str(body.get("lane")).strip() and str(body.get("lane")) != str(lane):
                raise ProvisioningError(
                    "hermes_broker_prototype_cross_lane_result",
                    retryable=False,
                )

    @staticmethod
    def _parse_json(response: httpx.Response, *, max_bytes: int, invalid_code: str) -> dict[str, Any]:
        if response.status_code in {401, 403}:
            raise ProvisioningError(
                "hermes_broker_prototype_auth_failed",
                retryable=False,
            )
        if response.status_code >= 500:
            raise ProvisioningError(
                "hermes_broker_prototype_unavailable",
                retryable=True,
            )
        if response.status_code not in {200, 202}:
            raise ProvisioningError(invalid_code, retryable=True)
        raw = response.content or b""
        if len(raw) > max_bytes:
            raise ProvisioningError(
                "hermes_broker_prototype_body_too_large",
                retryable=False,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProvisioningError(invalid_code, retryable=True) from exc
        if not isinstance(body, dict):
            raise ProvisioningError(invalid_code, retryable=True)
        return body
