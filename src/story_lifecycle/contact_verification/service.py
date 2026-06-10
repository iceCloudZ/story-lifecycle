"""Contact verification service — orchestrates third-party reachability checks."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .models import (
    ContactType,
    ReachabilityStatus,
    VerificationResult,
    VerificationRequest,
    VerificationResponse,
    VerificationProviderConfig,
    _db,
    init_verification_db,
)
from .client import (
    BaseVerificationProvider,
    HttpVerificationProvider,
)
from .exceptions import (
    ContactVerificationError,
    ContactValidationError,
)

log = logging.getLogger("story-lifecycle.contact-verification.service")


class ProviderRegistry:
    """Registry of verification providers."""

    def __init__(self):
        self._providers: dict[str, BaseVerificationProvider] = {}
        self._configs: dict[str, VerificationProviderConfig] = {}

    def register(
        self,
        provider: BaseVerificationProvider,
        config: VerificationProviderConfig | None = None,
    ) -> None:
        name = provider.provider_name
        if name in self._providers:
            raise ValueError(f"Provider '{name}' already registered")
        self._providers[name] = provider
        if config:
            self._configs[name] = config
        log.info(f"Registered verification provider: {name}")

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)
        self._configs.pop(name, None)
        log.info(f"Unregistered verification provider: {name}")

    def get(self, name: str) -> BaseVerificationProvider | None:
        return self._providers.get(name)

    def get_config(self, name: str) -> VerificationProviderConfig | None:
        return self._configs.get(name)

    def set_config(self, name: str, config: VerificationProviderConfig) -> None:
        if name not in self._providers:
            raise ValueError(f"Provider '{name}' not registered, register it first")
        self._configs[name] = config

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def get_default_provider(
        self,
    ) -> tuple[BaseVerificationProvider | None, VerificationProviderConfig | None]:
        for name, provider in self._providers.items():
            return provider, self._configs.get(name)
        return None, None


class ContactVerificationService:
    """Service for verifying contact reachability via third-party providers."""

    def __init__(self, registry: ProviderRegistry):
        self._registry = registry

    async def verify_contacts(
        self, request: VerificationRequest
    ) -> VerificationResponse:
        overall_start = time.monotonic()
        provider_name = request.provider
        provider, config = self._resolve_provider(provider_name)

        results: list[VerificationResult] = []
        for contact in request.contacts:
            if contact.phone:
                result = await self._verify_single(
                    ContactType.PHONE, contact.phone, provider, config
                )
                results.append(result)
            if contact.email:
                result = await self._verify_single(
                    ContactType.EMAIL, contact.email, provider, config
                )
                results.append(result)

        total_latency_ms = (time.monotonic() - overall_start) * 1000
        success_count = sum(
            1 for r in results if r.status != ReachabilityStatus.VERIFICATION_FAILED
        )
        failure_count = len(results) - success_count

        return VerificationResponse(
            results=results,
            total_latency_ms=round(total_latency_ms, 1),
            success_count=success_count,
            failure_count=failure_count,
        )

    async def verify_single_contact(
        self,
        contact_type: ContactType,
        contact_value: str,
        provider_name: str = "default",
    ) -> VerificationResult:
        provider, config = self._resolve_provider(provider_name)
        return await self._verify_single(contact_type, contact_value, provider, config)

    async def verify_and_merge(
        self,
        contact_type: ContactType,
        contact_value: str,
        provider_name: str = "default",
    ) -> dict:
        """Verify a single contact and return a merged result dict.

        Returns dict with keys: verified, status, provider, provider_message, latency_ms.
        """
        result = await self.verify_single_contact(
            contact_type, contact_value, provider_name
        )
        return {
            "verified": result.status == ReachabilityStatus.REACHABLE,
            "status": result.status.value,
            "provider": result.provider,
            "provider_message": result.provider_message,
            "latency_ms": result.latency_ms,
        }

    async def _verify_single(
        self,
        contact_type: ContactType,
        contact_value: str,
        provider: BaseVerificationProvider | None,
        config: VerificationProviderConfig | None,
    ) -> VerificationResult:
        start_time = time.monotonic()

        if provider is None:
            log.warning(
                f"No verification provider registered, cannot verify {contact_type.value}={contact_value}"
            )
            latency = (time.monotonic() - start_time) * 1000
            return VerificationResult(
                contact_type=contact_type,
                contact_value=contact_value,
                status=ReachabilityStatus.VERIFICATION_FAILED,
                provider="",
                provider_message="No verification provider configured",
                latency_ms=round(latency, 1),
            )

        provider_name = provider.provider_name
        log.info(f"[{provider_name}] verifying {contact_type.value}={contact_value}")

        attempt = 1
        try:
            response = await provider.verify(contact_type, contact_value, config)
            latency = (time.monotonic() - start_time) * 1000
        except ContactValidationError as e:
            latency = (time.monotonic() - start_time) * 1000
            self._record_log(
                contact_type=contact_type.value,
                contact_value=contact_value,
                provider=provider_name,
                status=ReachabilityStatus.VERIFICATION_FAILED.value,
                provider_message=e.message,
                latency_ms=latency,
                attempt=1,
                error_message=e.message,
            )
            return VerificationResult(
                contact_type=contact_type,
                contact_value=contact_value,
                status=ReachabilityStatus.VERIFICATION_FAILED,
                provider=provider_name,
                provider_message=e.message,
                latency_ms=round(latency, 1),
            )
        except ValueError as e:
            latency = (time.monotonic() - start_time) * 1000
            self._record_log(
                contact_type=contact_type.value,
                contact_value=contact_value,
                provider=provider_name,
                status=ReachabilityStatus.VERIFICATION_FAILED.value,
                provider_message=str(e),
                latency_ms=latency,
                attempt=1,
                error_message=str(e),
            )
            return VerificationResult(
                contact_type=contact_type,
                contact_value=contact_value,
                status=ReachabilityStatus.VERIFICATION_FAILED,
                provider=provider_name,
                provider_message=str(e),
                latency_ms=round(latency, 1),
            )
        except ContactVerificationError as e:
            latency = (time.monotonic() - start_time) * 1000
            log.warning(f"[{provider_name}] verification error: {e.message}")
            self._record_log(
                contact_type=contact_type.value,
                contact_value=contact_value,
                provider=provider_name,
                status=ReachabilityStatus.VERIFICATION_FAILED.value,
                provider_message=e.message,
                latency_ms=latency,
                attempt=1,
                error_message=str(e),
            )
            return VerificationResult(
                contact_type=contact_type,
                contact_value=contact_value,
                status=ReachabilityStatus.VERIFICATION_FAILED,
                provider=provider_name,
                provider_message=e.message,
                latency_ms=round(latency, 1),
            )
        except Exception as e:
            latency = (time.monotonic() - start_time) * 1000
            log.exception(f"[{provider_name}] verification error: {e}")
            self._record_log(
                contact_type=contact_type.value,
                contact_value=contact_value,
                provider=provider_name,
                status=ReachabilityStatus.VERIFICATION_FAILED.value,
                provider_message=str(e),
                latency_ms=latency,
                attempt=1,
                error_message=str(e),
            )
            return VerificationResult(
                contact_type=contact_type,
                contact_value=contact_value,
                status=ReachabilityStatus.VERIFICATION_FAILED,
                provider=provider_name,
                provider_message=f"Verification error: {e}",
                latency_ms=round(latency, 1),
            )

        if response.success:
            status = (
                ReachabilityStatus.REACHABLE
                if response.reachable
                else ReachabilityStatus.UNREACHABLE
            )
        else:
            status = ReachabilityStatus.VERIFICATION_FAILED

        result = VerificationResult(
            contact_type=contact_type,
            contact_value=contact_value,
            status=status,
            provider=provider_name,
            provider_message=response.message,
            latency_ms=round(response.latency_ms, 1),
            raw_response=response.raw_response,
        )

        self._record_log(
            contact_type=contact_type.value,
            contact_value=contact_value,
            provider=provider_name,
            status=status.value,
            provider_message=response.message,
            latency_ms=response.latency_ms,
            attempt=attempt,
            error_message="" if response.success else response.message,
        )

        log.info(
            f"[{provider_name}] {contact_type.value}={contact_value} -> {status.value} "
            f"({response.latency_ms:.0f}ms)"
        )
        return result

    def _resolve_provider(
        self, provider_name: str
    ) -> tuple[BaseVerificationProvider | None, VerificationProviderConfig | None]:
        if provider_name == "default":
            return self._registry.get_default_provider()
        provider = self._registry.get(provider_name)
        if provider is None:
            log.warning(f"Provider '{provider_name}' not found, trying default")
            return self._registry.get_default_provider()
        return provider, self._registry.get_config(provider_name)

    def _record_log(
        self,
        contact_type: str,
        contact_value: str,
        provider: str,
        status: str,
        provider_message: str,
        latency_ms: float,
        attempt: int,
        error_message: str,
    ) -> int:
        init_verification_db()
        now = datetime.now(timezone.utc).isoformat()
        with _db() as conn:
            cur = conn.execute(
                """INSERT INTO verification_log
                   (contact_type, contact_value, provider, status,
                    provider_message, latency_ms, attempt, error_message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact_type,
                    contact_value,
                    provider,
                    status,
                    provider_message,
                    latency_ms,
                    attempt,
                    error_message,
                    now,
                ),
            )
            return cur.lastrowid


def create_default_service(
    provider_name: str = "http",
    api_url: str = "",
    api_key: str = "",
    timeout_ms: int = 10000,
    max_retries: int = 3,
) -> ContactVerificationService:
    registry = ProviderRegistry()
    provider = HttpVerificationProvider(provider_name)
    config = VerificationProviderConfig(
        name=provider_name,
        api_url=api_url,
        api_key=api_key,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
    )
    registry.register(provider, config)
    return ContactVerificationService(registry)
