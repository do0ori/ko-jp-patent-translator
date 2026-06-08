"""Best-effort Discord webhook alerts.

Kept dependency-light and fully fail-safe: ``requests`` is imported lazily and
every failure is swallowed + logged, so a missing webhook / network blip / bad
URL can NEVER break the translation flow. Configure via
``st.secrets["discord_webhook_url"]`` or the ``DISCORD_WEBHOOK_URL`` env var.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Discord rejects messages over 2000 chars; stay well under.
_MAX_CONTENT = 1900


def _webhook_url() -> str | None:
    try:
        import streamlit as st

        url = st.secrets.get("discord_webhook_url")
        if url:
            return str(url)
    except Exception:
        pass
    return os.environ.get("DISCORD_WEBHOOK_URL") or None


def notify_discord(content: str, *, username: str = "한일 특허 번역기") -> bool:
    """POST ``content`` to the configured Discord webhook. Returns success.

    No-op (returns ``False``) when no webhook is configured. Never raises.
    """
    url = _webhook_url()
    if not url:
        log.info("[discord] webhook not configured; skipping alert")
        return False
    try:
        import requests

        resp = requests.post(
            url,
            json={"content": content[:_MAX_CONTENT], "username": username},
            timeout=5,
        )
        if resp.status_code >= 300:
            log.warning(
                "[discord] webhook returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        return True
    except Exception:
        log.exception("[discord] failed to send webhook")
        return False


def notify_discord_failure(
    *,
    doc_name: str,
    consecutive_failures: int,
    reason: str,
    workers: int,
    model: str,
) -> bool:
    """Send a formatted '번역 실패' alert. Never raises."""
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    content = (
        "🔴 **번역 실패 알림**\n"
        f"• 문서: `{doc_name or '(이름 없음)'}`\n"
        f"• 연속 실패: **{consecutive_failures}회**\n"
        f"• 사유: {reason}\n"
        f"• 워커/모델: {workers} / {model}\n"
        f"• 시각: {ts}"
    )
    return notify_discord(content)
