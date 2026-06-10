"""Verification provider client — HTTP-based third-party verification."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .models import ContactType, VerificationProviderConfig
from .exceptions import ContactVerificationError

log = logging.getLogger("story-lifecycle.contact-verification.client")


@dataclass
class ProviderResponse:
    success: bool
    reachable: bool
    message: str
    latency_ms: float = 0.0
    raw_response: dict = field(default_factory=dict)


class BaseVerificationProvider(ABC):
    """Abstract base for verification providers."""

    def __init__(self, provider_name: str):
        self.provider_name = provider_name

    @abstractmethod
    async def verify(
        self,
        contact_type: ContactType,
        contact_value: str,
        config: VerificationProviderConfig | None = None,
    ) -> ProviderResponse: ...


class HttpVerificationProvider(BaseVerificationProvider):
    """HTTP-based verification provider with retry and timeout."""

    def __init__(self, provider_name: str = "http"):
        super().__init__(provider_name)

    async def verify(
        self,
        contact_type: ContactType,
        contact_value: str,
        config: VerificationProviderConfig | None = None,
    ) -> ProviderResponse:
        if not config or not config.api_url:
            raise ContactVerificationError(
                f"No API URL configured for provider '{self.provider_name}'",
                provider=self.provider_name,
            )

        import httpx

        start = time.monotonic()
        headers = {
            config.auth_header_name: f"{config.auth_header_value_prefix}{config.api_key}"
        }
        if config.extra_headers:
            headers.update(config.extra_headers)

        payload = {
            "contact_type": contact_type.value,
            "contact_value": contact_value,
        }

        last_error = ""
        for attempt in range(1, config.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=config.timeout_ms / 1000.0
                ) as client:
                    resp = await client.post(
                        config.api_url,
                        json=payload,
                        headers=headers,
                    )
                latency = (time.monotonic() - start) * 1000

                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    log.warning(
                        f"[{self.provider_name}] attempt {attempt} failed: {last_error}"
                    )
                    continue

                data = resp.json()
                reachable = data.get("reachable", False)
                message = data.get("message", "")

                return ProviderResponse(
                    success=True,
                    reachable=reachable,
                    message=message,
                    latency_ms=latency,
                    raw_response=data,
                )

            except httpx.TimeoutException:
                latency = (time.monotonic() - start) * 1000
                last_error = f"Timeout after {config.timeout_ms}ms"
                log.warning(f"[{self.provider_name}] attempt {attempt} timeout")
            except Exception as e:
                latency = (time.monotonic() - start) * 1000
                last_error = str(e)
                log.warning(f"[{self.provider_name}] attempt {attempt} error: {e}")

        return ProviderResponse(
            success=False,
            reachable=False,
            message=f"All {config.max_retries} attempts failed: {last_error}",
            latency_ms=(time.monotonic() - start) * 1000,
        )
