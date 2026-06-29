"""
Vobiz implementation of the TelephonyProvider interface.
"""

import json
import random
from decimal import ROUND_CEILING, Decimal
from html import escape
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiohttp
from fastapi import HTTPException
from loguru import logger

from api.enums import WorkflowRunMode
from api.services.telephony.base import (
    CallInitiationResult,
    NormalizedInboundData,
    ProviderSyncResult,
    TelephonyProvider,
)
from api.utils.common import get_backend_endpoints
from api.utils.telephony_address import normalize_telephony_address

if TYPE_CHECKING:
    from fastapi import WebSocket


def _get_header(headers: Dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _first_value(data: dict[str, Any], keys: list[str]) -> tuple[str | None, Any]:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return key, value
    return None, None


class VobizProvider(TelephonyProvider):
    """
    Vobiz implementation of TelephonyProvider.
    Vobiz uses Plivo-compatible API and WebSocket protocol.
    """

    PROVIDER_NAME = WorkflowRunMode.VOBIZ.value
    WEBHOOK_ENDPOINT = "vobiz-xml"

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize VobizProvider with configuration.

        Args:
            config: Dictionary containing:
                - auth_id: Vobiz Account ID (e.g., MA_SYQRLN1K)
                - auth_token: Vobiz Auth Token
                - application_id: Vobiz Application ID whose answer_url is
                    updated by ``configure_inbound``
                - from_numbers: List of phone numbers to use (E.164 format without +)
        """
        self.auth_id = config.get("auth_id")
        self.auth_token = config.get("auth_token")
        self.application_id = config.get("application_id")
        self.from_numbers = config.get("from_numbers", [])

        # Handle both single number (string) and multiple numbers (list)
        if isinstance(self.from_numbers, str):
            self.from_numbers = [self.from_numbers]

        self.base_url = "https://api.vobiz.ai/api"

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        """
        Initiate an outbound call via Vobiz.

        Vobiz API differences from Twilio:
        - Uses X-Auth-ID and X-Auth-Token headers instead of Basic Auth
        - Expects JSON body instead of form data
        - Phone numbers in E.164 format WITHOUT + prefix (e.g., 14155551234)
        - Returns "call_uuid" instead of "sid"
        """
        if not self.validate_config():
            raise ValueError("Vobiz provider not properly configured")

        endpoint = f"{self.base_url}/v1/Account/{self.auth_id}/Call/"

        # Use provided from_number or select a random one
        if from_number is None:
            from_number = random.choice(self.from_numbers)
        logger.info(f"Selected Vobiz phone number {from_number} for outbound call")

        # Remove + prefix if present (Vobiz expects E.164 without +)
        to_number_clean = to_number.lstrip("+")
        from_number_clean = from_number.lstrip("+")

        # Prepare call data (JSON format)
        data = {
            "from": from_number_clean,
            "to": to_number_clean,
            "answer_url": webhook_url,
            "answer_method": "POST",
        }

        # Add hangup callback if workflow_run_id provided
        if workflow_run_id:
            backend_endpoint, _ = await get_backend_endpoints()
            hangup_url = f"{backend_endpoint}/api/v1/telephony/vobiz/hangup-callback/{workflow_run_id}"
            ring_url = f"{backend_endpoint}/api/v1/telephony/vobiz/ring-callback/{workflow_run_id}"
            data.update(
                {
                    "hangup_url": hangup_url,
                    "hangup_method": "POST",
                    "ring_url": ring_url,
                    "ring_method": "POST",
                }
            )

        # Add optional parameters
        data.update(kwargs)

        # Make the API request
        headers = {
            "X-Auth-ID": self.auth_id,
            "X-Auth-Token": self.auth_token,
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=data, headers=headers) as response:
                if response.status != 201:
                    error_data = await response.text()
                    logger.error(f"Vobiz API error: {error_data}")
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to initiate Vobiz call: {error_data}",
                    )

                response_data = await response.json()
                logger.info(f"Vobiz API response: {response_data}")

                # Extract call_uuid with multiple fallback options
                call_id = (
                    response_data.get("call_uuid")
                    or response_data.get("CallUUID")
                    or response_data.get("request_uuid")
                    or response_data.get("RequestUUID")
                )

                if not call_id:
                    logger.error(
                        f"No call ID found in Vobiz response. Available keys: {list(response_data.keys())}"
                    )
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Vobiz API response missing call identifier. Response: {response_data}"
                        f"Vobiz API response missing call identifier. Response: {response_data}",
                    )

                logger.info(f"Vobiz call initiated successfully. Call ID: {call_id}")

                return CallInitiationResult(
                    call_id=call_id,
                    status="queued",  # Vobiz returns "message": "call fired"
                    caller_number=from_number,
                    provider_metadata={"call_id": call_id},
                    raw_response=response_data,
                )

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """
        Get the current status of a Vobiz call (CDR).

        Vobiz returns:
        - call_uuid, status, duration, billed_duration
        - call_rate, total_cost (for billing)
        """
        if not self.validate_config():
            raise ValueError("Vobiz provider not properly configured")

        endpoint = f"{self.base_url}/v1/Account/{self.auth_id}/Call/{call_id}/"

        headers = {"X-Auth-ID": self.auth_id, "X-Auth-Token": self.auth_token}

        async with aiohttp.ClientSession() as session:
            async with session.get(endpoint, headers=headers) as response:
                if response.status != 200:
                    error_data = await response.text()
                    logger.error(f"Failed to get Vobiz call status: {error_data}")
                    raise Exception(f"Failed to get call status: {error_data}")

                return await response.json()

    async def get_available_phone_numbers(self) -> List[str]:
        """
        Get list of available Vobiz phone numbers.
        """
        return self.from_numbers

    def validate_config(self) -> bool:
        """
        Validate Vobiz configuration.
        """
        return bool(self.auth_id and self.auth_token and self.from_numbers)

    async def verify_webhook_signature(
        self,
        url: str,
        params: Dict[str, Any],
        signature: str,
        timestamp: str = None,
        body: str = "",
    ) -> bool:
        """
        Verify Vobiz webhook signature for security.

        Vobiz uses HMAC-SHA256 signature verification with timestamp validation:
        - Header: x-vobiz-signature (HMAC-SHA256 hash)
        - Header: x-vobiz-timestamp (timestamp for replay protection)
        - Signature = HMAC-SHA256(auth_token, timestamp + '.' + body)
        """
        import hashlib
        import hmac
        from datetime import datetime, timezone

        if not signature or not timestamp:
            logger.warning("Missing signature or timestamp headers for Vobiz webhook")
            return False

        if not self.auth_token:
            logger.error(
                "No auth_token available for Vobiz webhook signature verification"
            )
            return False

        try:
            # 1. Timestamp validation (within 5 minutes)
            webhook_timestamp = int(timestamp)
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            time_diff = abs(current_timestamp - webhook_timestamp)

            if time_diff > 300:  # 5 minutes = 300 seconds
                logger.warning(f"Vobiz webhook timestamp too old: {time_diff}s > 300s")
                return False

            # 2. Signature verification
            # Create expected signature: HMAC-SHA256(auth_token, timestamp + '.' + body)
            payload = f"{timestamp}.{body}"
            expected_signature = hmac.new(
                self.auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()

            # 3. Compare signatures (timing-safe comparison)
            is_valid = hmac.compare_digest(expected_signature, signature)

            if not is_valid:
                logger.warning(
                    f"Vobiz webhook signature mismatch. Expected: {expected_signature[:8]}..., Got: {signature[:8]}..."
                )

            return is_valid

        except Exception as e:
            logger.error(f"Error verifying Vobiz webhook signature: {e}")
            return False

    async def get_webhook_response(
        self, workflow_id: int, user_id: int, workflow_run_id: int
    ) -> str:
        """
        Generate Vobiz XML response for starting a call session.

        Vobiz uses <Stream> element similar to Twilio but with Plivo-compatible attributes:
        - bidirectional: Enable two-way audio
        - audioTrack: Which audio to stream (inbound, outbound, both)
        - contentType: audio/x-mulaw;rate=8000
        """
        _, wss_backend_endpoint = await get_backend_endpoints()

        vobiz_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">{wss_backend_endpoint}/api/v1/telephony/ws/{workflow_id}/{user_id}/{workflow_run_id}</Stream>
</Response>"""
        return vobiz_xml

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        """
        Get cost information for a completed Vobiz call.

        Vobiz exposes authoritative costs through CDRs. Current India CDRs
        return INR major units, e.g. total_cost=0.45 and currency=INR.

        Args:
            call_id: The Vobiz call_uuid

        Returns:
            Dict containing cost information
        """
        endpoint = f"{self.base_url}/v1/Account/{self.auth_id}/cdr/{call_id}"
        legacy_endpoint = f"{self.base_url}/v1/Account/{self.auth_id}/Call/{call_id}/"

        try:
            headers = {"X-Auth-ID": self.auth_id, "X-Auth-Token": self.auth_token}

            async with aiohttp.ClientSession() as session:
                call_data, status_error = await self._fetch_vobiz_json(
                    session, endpoint, headers
                )
                source = "cdr"
                if call_data is None:
                    logger.warning(
                        f"Vobiz CDR lookup failed for {call_id}; trying legacy call endpoint"
                    )
                    call_data, status_error = await self._fetch_vobiz_json(
                        session, legacy_endpoint, headers
                    )
                    source = "legacy_call_endpoint"

                if call_data is None:
                    pricing = await self._fetch_account_pricing(session, headers)
                    if pricing:
                        return self._estimate_call_cost_from_pricing(
                            call_id=call_id,
                            pricing=pricing,
                            source_error=status_error,
                        )
                    logger.error(f"Failed to get Vobiz call cost: {status_error}")
                    return {
                        "cost_usd": 0.0,
                        "cost_inr": 0.0,
                        "duration": 0,
                        "status": "error",
                        "error": str(status_error),
                    }

                return self._normalize_call_cost(call_data, source=source)

        except Exception as e:
            logger.error(f"Exception fetching Vobiz call cost: {e}")
            return {
                "cost_usd": 0.0,
                "cost_inr": 0.0,
                "duration": 0,
                "status": "error",
                "error": str(e),
            }

    async def _fetch_vobiz_json(
        self, session: aiohttp.ClientSession, endpoint: str, headers: dict[str, str]
    ) -> tuple[dict[str, Any] | None, str | None]:
        async with session.get(endpoint, headers=headers) as response:
            if response.status != 200:
                return None, await response.text()
            body = await response.json()
            return self._extract_data_object(body), None

    async def _fetch_account_pricing(
        self, session: aiohttp.ClientSession, headers: dict[str, str]
    ) -> dict[str, Any] | None:
        endpoint = f"{self.base_url}/v1/auth/me"
        account, error = await self._fetch_vobiz_json(session, endpoint, headers)
        if account is None:
            logger.warning(f"Failed to fetch Vobiz account pricing: {error}")
            return None
        pricing = account.get("pricing_tier")
        return pricing if isinstance(pricing, dict) else None

    @staticmethod
    def _extract_data_object(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            return {}
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return body

    @staticmethod
    def _amount_from_cdr(data: dict[str, Any]) -> tuple[Decimal, str | None]:
        key, raw_value = _first_value(data, ["total_cost", "cost", "totalCost"])
        amount = _decimal_or_none(raw_value) or Decimal("0")
        if key == "totalCost" or (
            key == "cost"
            and "ratePerMinute" in data
            and "total_cost" not in data
            and amount >= Decimal("10")
        ):
            amount = amount / Decimal("100")
        return amount, key

    @staticmethod
    def _rate_from_cdr(data: dict[str, Any]) -> Decimal | None:
        key, raw_value = _first_value(
            data, ["rate_per_minute", "call_rate", "ratePerMinute"]
        )
        rate = _decimal_or_none(raw_value)
        if rate is not None and key == "ratePerMinute":
            rate = rate / Decimal("100")
        return rate

    @classmethod
    def _normalize_call_cost(
        cls, call_data: dict[str, Any], *, source: str
    ) -> dict[str, Any]:
        data = cls._extract_data_object(call_data)
        amount, amount_key = cls._amount_from_cdr(data)
        currency = str(data.get("currency") or "INR").upper()
        duration = _int_or_zero(
            data.get("billsec")
            or data.get("billableSeconds")
            or data.get("billed_duration")
            or data.get("duration")
        )
        rate = cls._rate_from_cdr(data)
        result = {
            "cost_usd": float(amount if currency == "USD" else Decimal("0")),
            "cost_inr": float(amount if currency == "INR" else Decimal("0")),
            "cost": float(amount),
            "currency": currency,
            "duration": duration,
            "status": data.get("status") or data.get("call_status") or "unknown",
            "price_unit": currency,
            "call_rate": float(rate) if rate is not None else None,
            "rate_inr_per_minute": float(rate) if currency == "INR" and rate else None,
            "rate_usd_per_minute": float(rate) if currency == "USD" and rate else None,
            "billing_increment_seconds": data.get("billing_increment_seconds"),
            "minimum_duration_seconds": data.get("minimum_duration_seconds"),
            "source": source,
            "source_url": "https://docs.vobiz.ai/cdr/get-cdr",
            "raw_response": data,
        }
        if amount_key:
            result["cost_field"] = amount_key
        return result

    @staticmethod
    def _estimate_call_cost_from_pricing(
        *,
        call_id: str,
        pricing: dict[str, Any],
        source_error: str | None,
    ) -> dict[str, Any]:
        currency = str(pricing.get("currency") or "INR").upper()
        rate = _decimal_or_none(pricing.get("rate_per_minute")) or Decimal("0")
        streaming_rate = _decimal_or_none(
            pricing.get("streaming_rate_per_minute")
        ) or Decimal("0")
        total_rate = rate + streaming_rate
        billing_increment = max(
            _int_or_zero(pricing.get("billing_increment_seconds")), 1
        )
        minimum_duration = _int_or_zero(pricing.get("minimum_duration_seconds"))
        duration = max(minimum_duration, 0)
        billable_seconds = (
            Decimal(str(duration)) / Decimal(str(billing_increment))
        ).to_integral_value(rounding=ROUND_CEILING) * Decimal(str(billing_increment))
        amount = billable_seconds * total_rate / Decimal("60")
        return {
            "cost_usd": float(amount if currency == "USD" else Decimal("0")),
            "cost_inr": float(amount if currency == "INR" else Decimal("0")),
            "cost": float(amount),
            "currency": currency,
            "duration": int(billable_seconds),
            "status": "unknown",
            "price_unit": currency,
            "call_rate": float(total_rate),
            "rate_inr_per_minute": float(total_rate) if currency == "INR" else None,
            "rate_usd_per_minute": float(total_rate) if currency == "USD" else None,
            "billing_increment_seconds": billing_increment,
            "minimum_duration_seconds": minimum_duration,
            "estimated": True,
            "source": "account_pricing_tier",
            "source_url": "https://www.docs.vobiz.ai/account/retrieve-account",
            "error": source_error,
            "raw_response": {"call_id": call_id, "pricing_tier": pricing},
        }

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse Vobiz status callback data into generic format.

        Vobiz sends callbacks to hangup_url and ring_url with:
        - call_uuid (instead of CallSid)
        - status, from, to, duration, etc.
        """
        return {
            "call_id": data.get("CallUUID") or data.get("call_uuid") or "",
            "status": data.get("CallStatus") or data.get("call_status") or "",
            "from_number": data.get("From") or data.get("from"),
            "to_number": data.get("To") or data.get("to"),
            "direction": data.get("Direction") or data.get("direction"),
            "duration": data.get("Duration") or data.get("duration"),
            "extra": data,
        }

    async def handle_websocket(
        self,
        websocket: "WebSocket",
        workflow_id: int,
        user_id: int,
        workflow_run_id: int,
    ) -> None:
        """
        Handle Vobiz WebSocket connection using Vobiz WebSocket protocol.

        Extracts stream_id and call_id from the start event and delegates
        message handling to VobizFrameSerializer.
        """
        from api.services.pipecat.run_pipeline import run_pipeline_telephony

        first_msg = await websocket.receive_text()
        start_msg = json.loads(first_msg)
        logger.debug(f"Received the first message: {start_msg}")

        # Validate that this is a start event
        if start_msg.get("event") != "start":
            logger.error(f"Expected 'start' event, got: {start_msg.get('event')}")
            await websocket.close(code=4400, reason="Expected start event")
            return

        logger.debug(f"Vobiz WebSocket connected for workflow_run {workflow_run_id}")

        try:
            # Extract stream_id and call_id from the start event
            start_data = start_msg.get("start", {})
            stream_id = start_data.get("streamId")
            call_id = start_data.get("callId")

            if not stream_id or not call_id:
                logger.error(f"Missing streamId or callId in start event: {start_data}")
                await websocket.close(code=4400, reason="Missing streamId or callId")
                return

            logger.info(
                f"[run {workflow_run_id}] Starting Vobiz WebSocket handler - "
                f"stream_id: {stream_id}, call_id: {call_id}"
            )

            await run_pipeline_telephony(
                websocket,
                provider_name=self.PROVIDER_NAME,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                user_id=user_id,
                call_id=call_id,
                transport_kwargs={"stream_id": stream_id, "call_id": call_id},
            )

            logger.info(f"[run {workflow_run_id}] Vobiz pipeline completed")

        except Exception as e:
            logger.error(
                f"[run {workflow_run_id}] Error in Vobiz WebSocket handler: {e}"
            )
            raise

    # ======== INBOUND CALL METHODS ========

    @classmethod
    def can_handle_webhook(
        cls, webhook_data: Dict[str, Any], headers: Dict[str, str]
    ) -> bool:
        """
        Determine if this provider can handle the incoming webhook.
        Vobiz webhooks contain CallUUID field.
        """
        return "vobiz" in headers.get("user-agent", "").lower()

    @staticmethod
    def parse_inbound_webhook(webhook_data: Dict[str, Any]) -> NormalizedInboundData:
        """
        Parse Vobiz-specific inbound webhook data into normalized format.
        """
        # Vobiz webhooks don't carry country info, and our deployment is
        # India-only today — hardcode "IN" so leading-0 trunk-prefix numbers
        # (e.g. "02271264296") normalize to the right E.164 ("+912271264296").
        # Revisit if/when we onboard a non-Indian Vobiz customer.
        country = "IN"
        from_raw = webhook_data.get("From", "")
        to_raw = webhook_data.get("To", "")
        return NormalizedInboundData(
            provider=VobizProvider.PROVIDER_NAME,
            call_id=webhook_data.get("CallUUID", ""),
            from_number=normalize_telephony_address(
                from_raw, country_hint=country
            ).canonical
            if from_raw
            else "",
            to_number=normalize_telephony_address(
                to_raw, country_hint=country
            ).canonical
            if to_raw
            else "",
            direction=webhook_data.get("Direction", ""),
            call_status=webhook_data.get("CallStatus", ""),
            account_id=webhook_data.get("ParentAuthID"),
            from_country=country,
            to_country=country,
            raw_data=webhook_data,
        )

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        """Validate Vobiz auth_id from webhook matches configuration"""
        if not webhook_account_id:
            return False

        stored_auth_id = config_data.get("auth_id")
        return stored_auth_id == webhook_account_id

    async def verify_inbound_signature(
        self,
        url: str,
        webhook_data: Dict[str, Any],
        headers: Dict[str, str],
        body: str = "",
    ) -> bool:
        """
        Verify the signature of an inbound Vobiz webhook for security.

        Vobiz inbound webhooks in the wild may omit the timestamp, or omit
        both signature headers. The dispatcher has already matched the
        provider account and called number before this method runs, so accept
        incomplete signing headers and enforce HMAC only when the complete
        header pair is present.
        """
        signature = _get_header(headers, "x-vobiz-signature")
        timestamp = _get_header(headers, "x-vobiz-timestamp")
        if not signature or not timestamp:
            missing = []
            if not signature:
                missing.append("X-Vobiz-Signature")
            if not timestamp:
                missing.append("X-Vobiz-Timestamp")
            logger.warning(
                "Inbound Vobiz webhook missing "
                f"{', '.join(missing)}; accepting after account/number route match"
            )
            return True
        return await self.verify_webhook_signature(
            url, webhook_data, signature, timestamp, body
        )

    async def configure_inbound(
        self, address: str, webhook_url: Optional[str]
    ) -> ProviderSyncResult:
        """Update answer_url on the Vobiz Application (Plivo-compatible model).

        Vobiz's update is partial so we POST only ``answer_url`` and
        ``answer_method`` — ``app_name``, ``hangup_url``, etc. stay as the
        user set them. The URL is shared across every number on the
        application — clearing is a no-op to avoid silently breaking
        inbound for sibling numbers.
        """
        if webhook_url is None:
            logger.info(
                f"Vobiz configure_inbound clear for {address}: skipping "
                f"application update (answer_url is shared across all numbers "
                f"on application {self.application_id})"
            )
            return ProviderSyncResult(ok=True)

        if not self.validate_config():
            return ProviderSyncResult(
                ok=False, message="Vobiz provider not properly configured"
            )

        if not self.application_id:
            return ProviderSyncResult(
                ok=False,
                message=(
                    "Vobiz application_id is not configured. Set it in the "
                    "telephony configuration so inbound webhooks can be "
                    "synced to the right Application."
                ),
            )

        app_endpoint = (
            f"{self.base_url}/v1/Account/{self.auth_id}/Application/"
            f"{self.application_id}/"
        )
        data = {
            "answer_url": webhook_url,
            "answer_method": "POST",
        }
        headers = {
            "X-Auth-ID": self.auth_id,
            "X-Auth-Token": self.auth_token,
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    app_endpoint, json=data, headers=headers
                ) as response:
                    if response.status not in (200, 202):
                        body = await response.text()
                        logger.error(
                            f"Vobiz application update failed for "
                            f"{self.application_id}: {response.status} {body}"
                        )
                        return ProviderSyncResult(
                            ok=False,
                            message=f"Vobiz API {response.status}: {body}",
                        )
        except Exception as e:
            logger.error(
                f"Exception updating Vobiz application {self.application_id}: {e}"
            )
            return ProviderSyncResult(ok=False, message=f"Vobiz update failed: {e}")

        logger.info(
            f"Vobiz answer_url set on application {self.application_id} "
            f"(triggered by address {address})"
        )
        return ProviderSyncResult(ok=True)

    async def start_inbound_stream(
        self,
        *,
        websocket_url: str,
        workflow_run_id: int,
        normalized_data,
        backend_endpoint: str,
    ):
        """
        Generate Vobiz XML response for an inbound webhook.

        Note: For hangup callbacks, configure the hangup_url manually in Vobiz dashboard
        to point to: /api/v1/telephony/vobiz/hangup-callback/workflow/{workflow_id}
        """
        from fastapi import Response

        vobiz_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">{websocket_url}</Stream>
</Response>"""

        return Response(content=vobiz_xml, media_type="application/xml")

    @staticmethod
    def generate_error_response(error_type: str, message: str) -> tuple:
        """
        Generate a Vobiz-specific error response.
        """
        from fastapi import Response

        # Vobiz error responses should be valid XML like Plivo
        vobiz_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN">Sorry, there was an error processing your call. {message}</Speak>
    <Hangup/>
</Response>"""

        return Response(content=vobiz_xml, media_type="application/xml")

    @staticmethod
    def generate_validation_error_response(error_type) -> tuple:
        """
        Generate Vobiz-specific error response for validation failures with organizational debugging info.
        """
        from fastapi import Response

        from api.errors.telephony_errors import TELEPHONY_ERROR_MESSAGES, TelephonyError

        message = TELEPHONY_ERROR_MESSAGES.get(
            error_type, TELEPHONY_ERROR_MESSAGES[TelephonyError.GENERAL_AUTH_FAILED]
        )

        vobiz_xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN">{message}</Speak>
    <Hangup/>
</Response>"""

        return Response(content=vobiz_xml_content, media_type="application/xml")

    @staticmethod
    def generate_queue_wait_response(
        *, position: int, redirect_url: str, retry_seconds: int
    ):
        from fastapi import Response

        safe_url = escape(redirect_url, quote=True)
        vobiz_xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN">All agents are currently busy. You are number {position} in the queue. Please stay on the line.</Speak>
    <Wait length="{retry_seconds}"/>
    <Redirect method="POST">{safe_url}</Redirect>
</Response>"""

        return Response(content=vobiz_xml_content, media_type="application/xml")

    @staticmethod
    def generate_queue_timeout_response():
        from fastapi import Response

        vobiz_xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN">All agents are still busy. Please call again later.</Speak>
    <Hangup/>
</Response>"""

        return Response(content=vobiz_xml_content, media_type="application/xml")

    # ======== CALL TRANSFER METHODS ========

    async def transfer_call(
        self,
        destination: str,
        transfer_id: str,
        conference_name: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Vobiz provider does not support call transfers.

        Raises:
            NotImplementedError: Vobiz call transfers are yet to be implemented
        """
        raise NotImplementedError("Vobiz provider does not support call transfers")

    def supports_transfers(self) -> bool:
        """
        Vobiz does not support call transfers.

        Returns:
            False - Vobiz provider does not support call transfers
        """
        return False
