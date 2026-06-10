"""Contact reachability validator — multi-channel reachability checks with DB persistence."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .email_validator import EmailConfig, EmailValidator
from .phone_validator import PhoneConfig, PhoneValidator


@dataclass
class ChannelStatus:
    channel: str
    reachable: bool
    score: float = 0.0
    error_code: str = ""
    error_message: str = ""
    detail: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "reachable": self.reachable,
            "score": self.score,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


@dataclass
class ContactInfo:
    contact_id: str = ""
    email: str = ""
    phone: str = ""
    occupation: str = ""


@dataclass
class ContactReachabilityConfig:
    enabled: bool = True
    check_contact_exists: bool = True
    check_email: bool = True
    check_phone: bool = True
    check_sms_channels: bool = True
    timeout_seconds: float = 30.0
    email_config: EmailConfig | None = None
    phone_config: PhoneConfig | None = None


@dataclass
class ContactReachabilityResult:
    contact_id: str
    overall_reachable: bool
    fully_reachable: bool
    channels: dict[str, ChannelStatus]
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "overall_reachable": self.overall_reachable,
            "fully_reachable": self.fully_reachable,
            "channels": {k: v.to_dict() for k, v in self.channels.items()},
            "checked_at": self.checked_at,
        }


# ── DB helpers ──


def _get_db_path() -> Path:
    home = os.environ.get("STORY_HOME", str(Path.home() / ".story-lifecycle"))
    Path(home).mkdir(parents=True, exist_ok=True)
    return Path(home) / "story.db"


def _get_conn() -> sqlite3.Connection:
    db = _get_db_path()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_reachability_db():
    """Create reachability_check_log table if not exists. Idempotent."""
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reachability_check_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id TEXT NOT NULL,
                overall_reachable INTEGER NOT NULL,
                fully_reachable INTEGER NOT NULL,
                channels_json TEXT NOT NULL,
                local_check_only INTEGER NOT NULL DEFAULT 1,
                provider_name TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reachability_contact
                ON reachability_check_log(contact_id);
            CREATE INDEX IF NOT EXISTS idx_reachability_checked
                ON reachability_check_log(checked_at);
        """)


def save_reachability_result(
    result: ContactReachabilityResult,
    local_check_only: bool = True,
    provider_name: str = "",
) -> int:
    """Persist a reachability check result to DB."""
    init_reachability_db()
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO reachability_check_log
               (contact_id, overall_reachable, fully_reachable, channels_json,
                local_check_only, provider_name, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                result.contact_id,
                1 if result.overall_reachable else 0,
                1 if result.fully_reachable else 0,
                json.dumps(
                    {k: v.to_dict() for k, v in result.channels.items()},
                    ensure_ascii=False,
                ),
                1 if local_check_only else 0,
                provider_name,
                result.checked_at,
            ),
        )
        return cur.lastrowid


def get_reachability_history(contact_id: str, limit: int = 10) -> list[dict]:
    """Get recent reachability check history for a contact."""
    init_reachability_db()
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM reachability_check_log
               WHERE contact_id = ?
               ORDER BY checked_at DESC
               LIMIT ?""",
            (contact_id, limit),
        ).fetchall()
        results = []
        for row in rows:
            channels = row["channels_json"]
            if isinstance(channels, str):
                try:
                    channels = json.loads(channels)
                except (json.JSONDecodeError, TypeError):
                    channels = {}
            results.append(
                {
                    "id": row["id"],
                    "contact_id": row["contact_id"],
                    "overall_reachable": bool(row["overall_reachable"]),
                    "fully_reachable": bool(row["fully_reachable"]),
                    "channels": channels,
                    "local_check_only": bool(row["local_check_only"]),
                    "provider_name": row["provider_name"],
                    "checked_at": row["checked_at"],
                }
            )
        return results


class ContactReachabilityValidator:
    def __init__(self, config: ContactReachabilityConfig | None = None):
        self.config = config or ContactReachabilityConfig()
        email_config = self.config.email_config or EmailConfig()
        phone_config = self.config.phone_config or PhoneConfig()
        self._email_validator = EmailValidator(email_config)
        self._phone_validator = PhoneValidator(phone_config)

    def validate(
        self,
        contact_id: str = "",
        email: str = "",
        phone: str = "",
        occupation: str = "",
        contact_exists: bool = True,
        sms_channels_available: bool = True,
    ) -> ContactReachabilityResult:
        if not self.config.enabled:
            return ContactReachabilityResult(
                contact_id=contact_id,
                overall_reachable=True,
                fully_reachable=True,
                channels={},
                checked_at=datetime.now(timezone.utc).isoformat(),
            )

        channels: dict[str, ChannelStatus] = {}
        now = datetime.now(timezone.utc).isoformat()

        # Existence check
        if self.config.check_contact_exists:
            if contact_exists:
                channels["existence"] = ChannelStatus(
                    channel="existence",
                    reachable=True,
                    score=1.0,
                    detail="联系人存在",
                    checked_at=now,
                )
            else:
                channels["existence"] = ChannelStatus(
                    channel="existence",
                    reachable=False,
                    score=0.0,
                    error_code="CONTACT_NOT_FOUND",
                    error_message="联系人不存在",
                    detail="联系人不存在",
                    checked_at=now,
                )

        # Email check
        if self.config.check_email:
            reachable, error_code, detail, score = self._email_validator.validate(
                email, occupation
            )
            channels["email"] = ChannelStatus(
                channel="email",
                reachable=reachable,
                score=score,
                error_code=error_code,
                error_message=detail if not reachable else "",
                detail=detail,
                checked_at=now,
            )

        # Phone check
        if self.config.check_phone:
            reachable, error_code, detail, score = self._phone_validator.validate(phone)
            channels["phone"] = ChannelStatus(
                channel="phone",
                reachable=reachable,
                score=score,
                error_code=error_code,
                error_message=detail if not reachable else "",
                detail=detail,
                checked_at=now,
            )

        # SMS check — only when phone is present
        if self.config.check_sms_channels and phone:
            if sms_channels_available:
                channels["sms"] = ChannelStatus(
                    channel="sms",
                    reachable=True,
                    score=0.90,
                    detail="短信通道可用",
                    checked_at=now,
                )
            else:
                carrier = ""
                if phone:
                    prefix = phone[:3]
                    from .phone_validator import CARRIER_PREFIXES

                    for c, prefixes in CARRIER_PREFIXES.items():
                        if prefix in prefixes:
                            carrier = c
                            break
                channels["sms"] = ChannelStatus(
                    channel="sms",
                    reachable=False,
                    score=0.0,
                    error_code="SMS_NO_CHANNEL",
                    error_message=f"无可用短信通道 ({carrier})"
                    if carrier
                    else "无可用短信通道",
                    detail=f"无可用短信通道 ({carrier})"
                    if carrier
                    else "无可用短信通道",
                    checked_at=now,
                )

        overall = any(ch.reachable for ch in channels.values())
        fully = all(ch.reachable for ch in channels.values()) if channels else True

        return ContactReachabilityResult(
            contact_id=contact_id,
            overall_reachable=overall,
            fully_reachable=fully,
            channels=channels,
            checked_at=now,
        )

    def check(self, contact: ContactInfo, **kwargs) -> ContactReachabilityResult:
        return self.validate(
            contact_id=contact.contact_id,
            email=contact.email,
            phone=contact.phone,
            occupation=contact.occupation,
            **kwargs,
        )


def validate_contact_reachability(**kwargs) -> ContactReachabilityResult:
    validator = ContactReachabilityValidator()
    return validator.validate(**kwargs)
