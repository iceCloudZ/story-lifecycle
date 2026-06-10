"""Phone validator — format, prefix, carrier detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# China mobile number: 11 digits starting with 1
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

CARRIER_PREFIXES: dict[str, set[str]] = {
    "中国移动": {
        "134",
        "135",
        "136",
        "137",
        "138",
        "139",
        "147",
        "148",
        "150",
        "151",
        "152",
        "157",
        "158",
        "159",
        "172",
        "178",
        "182",
        "183",
        "184",
        "187",
        "188",
        "195",
        "197",
        "198",
    },
    "中国联通": {
        "130",
        "131",
        "132",
        "145",
        "146",
        "155",
        "156",
        "166",
        "171",
        "175",
        "176",
        "185",
        "186",
        "196",
    },
    "中国电信": {
        "133",
        "149",
        "153",
        "173",
        "174",
        "177",
        "180",
        "181",
        "189",
        "190",
        "191",
        "193",
        "199",
    },
}

ALL_PREFIXES = set()
for _prefixes in CARRIER_PREFIXES.values():
    ALL_PREFIXES |= _prefixes


@dataclass
class PhoneConfig:
    allowed_prefixes: set[str] = field(default_factory=set)


class PhoneValidator:
    def __init__(self, config: PhoneConfig | None = None):
        self.config = config or PhoneConfig()

    def validate(self, phone: str) -> tuple[bool, str, str, float]:
        """Validate a phone number.

        Returns (reachable, error_code, detail, score).
        """
        if not phone:
            return False, "PHONE_EMPTY", "手机号为空", 0.0

        if not PHONE_RE.match(phone):
            return False, "PHONE_FORMAT_INVALID", "手机号格式无效", 0.0

        prefix = phone[:3]
        if self.config.allowed_prefixes and prefix not in self.config.allowed_prefixes:
            return (
                False,
                "PHONE_PREFIX_NOT_ALLOWED",
                f"号段 {prefix} 不在允许列表中",
                0.0,
            )

        carrier = self._detect_carrier(prefix)
        score = 0.95
        detail = f"手机号格式有效 ({carrier})" if carrier else "手机号格式有效"
        return True, "", detail, score

    def _detect_carrier(self, prefix: str) -> str:
        for carrier, prefixes in CARRIER_PREFIXES.items():
            if prefix in prefixes:
                return carrier
        return ""
