"""Cross-platform desktop notification via plyer."""

import logging

log = logging.getLogger("story-lifecycle.notify")


def send(title: str, message: str) -> None:
    """Send a desktop notification. Silent fallback if plyer unavailable."""
    try:
        from plyer import notification

        notification.notify(title=title, message=message, timeout=5)
    except Exception:
        log.debug(f"Notification skipped (plyer unavailable): {title} — {message}")
