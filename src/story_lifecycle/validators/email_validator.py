"""Email validator — format, disposable, occupation-domain matching."""

from __future__ import annotations

from dataclasses import dataclass, field

DISPOSABLE_DOMAINS = {
    "mailinator.com",
    "guerrillamail.com",
    "guerrillamailblock.com",
    "sharklasers.com",
    "grr.la",
    "guerrillamail.info",
    "guerrillamail.net",
    "guerrillamail.biz",
    "guerrillamail.de",
    "guerrillamail.org",
    "spam4.me",
    "trashmail.ws",
    "yopmail.com",
    "yopmail.fr",
    "jetable.org",
    "tempmail.com",
    "throwaway.email",
}

OCCUPATION_DOMAINS: dict[str, list[str]] = {
    "civil_servant": [".gov.cn"],
    "student": [".edu.cn", ".edu"],
}


@dataclass
class EmailConfig:
    domain_whitelist: list[str] = field(default_factory=list)
    domain_blacklist: list[str] = field(default_factory=list)
    block_disposable: bool = True
    check_occupation_domain: bool = True


class EmailValidator:
    def __init__(self, config: EmailConfig | None = None):
        self.config = config or EmailConfig()

    def validate(
        self,
        email: str,
        occupation: str = "",
    ) -> tuple[bool, str, str, float]:
        """Validate an email address.

        Returns (reachable, error_code, detail, score).
        """
        if not email:
            return False, "EMAIL_EMPTY", "邮箱为空", 0.0

        if "@" not in email or email.count("@") != 1:
            return False, "EMAIL_FORMAT_INVALID", "邮箱格式无效", 0.0

        local, domain = email.rsplit("@", 1)
        if not local or not domain:
            return False, "EMAIL_FORMAT_INVALID", "邮箱格式无效", 0.0

        if "." not in domain:
            return False, "EMAIL_FORMAT_INVALID", "邮箱格式无效", 0.0

        # Whitelist / blacklist
        if self.config.domain_whitelist:
            if not any(
                domain == wl or domain.endswith("." + wl)
                for wl in self.config.domain_whitelist
            ):
                return (
                    False,
                    "EMAIL_DOMAIN_NOT_ALLOWED",
                    f"邮箱域名 {domain} 不在白名单中",
                    0.0,
                )

        if self.config.domain_blacklist:
            if any(
                domain == bl or domain.endswith("." + bl)
                for bl in self.config.domain_blacklist
            ):
                return (
                    False,
                    "EMAIL_DOMAIN_BLOCKED",
                    f"邮箱域名 {domain} 在黑名单中",
                    0.0,
                )

        # Disposable check
        if self.config.block_disposable and domain.lower() in DISPOSABLE_DOMAINS:
            return False, "EMAIL_DISPOSABLE", f"一次性邮箱 {domain}", 0.0

        score = 0.85

        # Occupation-domain check
        if self.config.check_occupation_domain and occupation:
            required_suffixes = OCCUPATION_DOMAINS.get(occupation)
            if required_suffixes:
                if not any(domain.endswith(s) for s in required_suffixes):
                    suffix_str = "/".join(required_suffixes)
                    return (
                        False,
                        f"EMAIL_SUFFIX_NOT_MATCH_{occupation.upper()}",
                        f"职业 {occupation} 要求邮箱后缀 {suffix_str}",
                        0.0,
                    )
                score = 0.95

        return True, "", "邮箱格式有效", score
