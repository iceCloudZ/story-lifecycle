"""Scope recommendation for init-knowledge.

Generates P0 recommended scope and candidate business domains
from the detection result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .detector import DetectionResult, ServiceInfo

# Services that are typically excluded from P0
_P0_EXCLUDE_PATTERNS = frozenset(
    {
        "audit",
        "log",
        "dms",
        "aiops",
        "ops",
        "admin",
        "monitor",
        "gateway",
        "auth-server",
        "config-server",
        "eureka",
        "nacos",
        "sentinel",
        "zipkin",
        "skywalking",
    }
)


@dataclass
class ScopeRecommendation:
    included: list[ServiceInfo]
    excluded: list[ServiceInfo]
    candidate_domains: list[CandidateDomain]


@dataclass
class CandidateDomain:
    domain: str
    source_service: str
    candidate_scenarios: list[str] = field(default_factory=list)


def recommend_scope(detection: DetectionResult) -> ScopeRecommendation:
    """Generate P0 scope recommendation from detection result."""
    included: list[ServiceInfo] = []
    excluded: list[ServiceInfo] = []

    for svc in detection.services:
        if _should_exclude_p0(svc.id):
            excluded_s = ServiceInfo(
                id=svc.id,
                path=svc.path,
                type=svc.type,
                included=False,
                reason="excluded from P0",
            )
            excluded.append(excluded_s)
        else:
            included.append(svc)

    # All frontends default excluded
    all_excluded = excluded + [
        ServiceInfo(
            id=f.id,
            path=f.path,
            type=f.type,
            included=False,
            reason="frontend excluded from P0",
        )
        for f in detection.frontends
    ]

    candidates = _generate_candidate_domains(included)

    return ScopeRecommendation(
        included=included,
        excluded=all_excluded,
        candidate_domains=candidates,
    )


def _should_exclude_p0(service_id: str) -> bool:
    """Check if a service should be excluded from P0 scope."""
    lower = service_id.lower()
    return any(pat in lower for pat in _P0_EXCLUDE_PATTERNS)


def _generate_candidate_domains(services: list[ServiceInfo]) -> list[CandidateDomain]:
    """Generate rough candidate domains from service names.

    This is a heuristic: parse service-id to guess business domain
    and common scenarios. These are NOT final boundaries.
    """
    domain_hints: dict[str, tuple[str, list[str]]] = {
        "user": (
            "user",
            ["register", "login", "profile", "account recovery", "logoff"],
        ),
        "order": (
            "order",
            ["withdraw", "repay", "overdue", "advance repay", "coolingoff"],
        ),
        "limit": ("limit", ["credit apply", "credit audit", "limit change"]),
        "message": ("message", ["sms", "voice", "push notification"]),
        "coupon": ("coupon", ["coupon issue", "coupon bind", "coupon budget"]),
        "marketing": ("marketing", ["campaign", "channel", "activity"]),
        "callback": ("callback", ["payment callback", "3rd-party callback"]),
        "third-party": (
            "third-party",
            ["payment channel", "channel routing", "channel balance"],
        ),
        "thirdparty": (
            "third-party",
            ["payment channel", "channel routing", "channel balance"],
        ),
        "risk": ("risk-management", ["risk check", "risk rule", "risk decision"]),
        "config": ("config", ["system config", "feature flag", "business rule"]),
        "job": ("job", ["scheduled task", "batch job", "retry job"]),
        "repay": ("repay", ["repay plan", "repay schedule", "repay calculation"]),
        "loan": ("loan", ["loan apply", "loan approval", "loan disburse"]),
        "payment": ("payment", ["pay", "refund", "payment channel"]),
        "notify": ("notification", ["sms", "push", "email"]),
    }

    candidates = []
    for svc in services:
        # Try to match domain from service id
        lower = svc.id.lower()
        # Strip common prefixes like "hc-"
        clean = lower.split("-", 1)[-1] if "-" in lower else lower

        domain_name = clean
        scenarios: list[str] = []

        # Check for known domain hints
        for key, (domain, hints) in domain_hints.items():
            if key in clean:
                domain_name = domain
                scenarios = hints
                break

        if not scenarios:
            # Generic: just list the service as a domain
            scenarios = [f"{clean} operations"]

        candidates.append(
            CandidateDomain(
                domain=domain_name,
                source_service=svc.id,
                candidate_scenarios=scenarios,
            )
        )

    return candidates
