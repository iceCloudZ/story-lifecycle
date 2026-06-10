"""Pydantic models and SQLite schema for contact reachability verification."""

from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ContactType(str, Enum):
    PHONE = "phone"
    EMAIL = "email"


class ReachabilityStatus(str, Enum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    VERIFICATION_FAILED = "verification_failed"


class ContactInfo(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None

    @model_validator(mode="after")
    def check_at_least_one(self) -> "ContactInfo":
        if not self.phone and not self.email:
            raise ValueError("At least one of phone or email must be provided")
        return self


class VerificationResult(BaseModel):
    contact_type: ContactType
    contact_value: str
    status: ReachabilityStatus
    provider: str = ""
    provider_message: str = ""
    latency_ms: float = 0.0
    raw_response: dict = Field(default_factory=dict)


class VerificationRequest(BaseModel):
    contacts: list[ContactInfo] = Field(..., min_length=1)
    provider: str = "default"
    metadata: dict = Field(default_factory=dict)


class VerificationResponse(BaseModel):
    results: list[VerificationResult]
    total_latency_ms: float = 0.0
    success_count: int = 0
    failure_count: int = 0


class VerificationProviderConfig(BaseModel):
    name: str
    api_url: str
    api_key: str = ""
    auth_header_name: str = "Authorization"
    auth_header_value_prefix: str = "Bearer "
    timeout_ms: int = 10000
    max_retries: int = 3
    retry_backoff_base_ms: int = 1000
    retry_backoff_multiplier: float = 2.0
    max_retry_delay_ms: int = 60000
    enabled: bool = True
    extra_headers: dict = Field(default_factory=dict)


class VerificationLog(BaseModel):
    id: int = 0
    contact_type: str = ""
    contact_value: str = ""
    provider: str = ""
    status: str = ""
    provider_message: str = ""
    latency_ms: float = 0.0
    attempt: int = 0
    error_message: str = ""
    created_at: str = ""


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


def init_verification_db():
    """Create verification-related tables if they don't exist. Idempotent."""
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS verification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_type TEXT NOT NULL,
                contact_value TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                provider_message TEXT NOT NULL DEFAULT '',
                latency_ms REAL NOT NULL DEFAULT 0.0,
                attempt INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_verification_log_contact
                ON verification_log(contact_value);
            CREATE INDEX IF NOT EXISTS idx_verification_log_provider
                ON verification_log(provider);
            CREATE INDEX IF NOT EXISTS idx_verification_log_created
                ON verification_log(created_at);
        """)


def _row_to_log(row: sqlite3.Row) -> VerificationLog:
    return VerificationLog(
        id=row["id"],
        contact_type=row["contact_type"],
        contact_value=row["contact_value"],
        provider=row["provider"],
        status=row["status"],
        provider_message=row["provider_message"],
        latency_ms=row["latency_ms"],
        attempt=row["attempt"],
        error_message=row["error_message"],
        created_at=row["created_at"],
    )
