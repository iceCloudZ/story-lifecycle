"""Headless Kimi CLI adapter for vision/multimodal tasks.

Kimi's `kimi-for-coding` model is restricted to Coding Agents and cannot be
called through the OpenAI-compatible HTTP API. This adapter shells out to the
local `kimi` CLI in non-interactive prompt mode and parses the `stream-json`
output to extract the final assistant response.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger("story-lifecycle.llm.kimi-cli")


def _find_kimi_bin() -> str | None:
    """Locate the Kimi CLI executable."""
    candidates = [
        Path.home() / ".kimi-code" / "bin" / "kimi",
        Path.home() / ".kimi-code" / "bin" / "kimi.EXE",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("kimi")


def _download_image(url: str, timeout: float = 20.0) -> Path:
    """Download a remote image to a temporary file and return its path."""
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        ext = ".png"
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            resp.raise_for_status()
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return Path(tmp_path)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise


class KimiCliClient:
    """Headless wrapper around the local `kimi` CLI for vision tasks."""

    def __init__(self, model: str = "kimi-for-coding"):
        self.model = model
        self._bin = _find_kimi_bin()
        if not self._bin:
            raise RuntimeError("Kimi CLI not found. Install it from https://kimi.com")

    def invoke_vision(
        self,
        prompt: str,
        images: list[str],
        *,
        timeout: int = 180,
        max_tokens: int | None = None,
    ) -> str:
        """Run a headless Kimi CLI prompt with image paths and return the answer.

        Args:
            prompt: Text prompt.
            images: List of image URLs or local file paths.
        Returns:
            The final assistant text content.
        """
        # Convert URLs to local temp files so the CLI can read them.
        local_images: list[str] = []
        temp_files: list[Path] = []
        try:
            for img in images:
                if img.startswith("data:"):
                    local_images.append(_data_url_to_temp_file(img))
                elif img.startswith("http://") or img.startswith("https://"):
                    p = _download_image(img)
                    temp_files.append(p)
                    local_images.append(str(p))
                else:
                    local_images.append(img)

            full_prompt = self._build_prompt(prompt, local_images)
            return self._run(full_prompt, timeout=timeout)
        finally:
            for p in temp_files:
                try:
                    p.unlink()
                except Exception:
                    pass

    @staticmethod
    def _build_prompt(prompt: str, image_paths: list[str]) -> str:
        parts = [prompt]
        if image_paths:
            parts.append("\n\n图片附件：")
            for p in image_paths:
                parts.append(f"- {p}")
            parts.append("\n请阅读以上图片，并只根据图片内容和前文要求输出结果。")
        return "\n".join(parts)

    def _run(self, prompt: str, timeout: int) -> str:
        cmd = [
            self._bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
        ]
        log.info("Running Kimi CLI vision prompt (timeout=%ds)", timeout)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            stderr = result.stderr[:500] if result.stderr else ""
            raise RuntimeError(f"Kimi CLI failed (code {result.returncode}): {stderr}")

        content = self._extract_answer(result.stdout)
        if not content:
            raise RuntimeError("Kimi CLI returned empty answer")
        return content

    @staticmethod
    def _extract_answer(stdout: str) -> str:
        """Parse stream-json output and return the last assistant content."""
        last_content = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("role") == "assistant" and "content" in msg:
                text = msg["content"]
                if text:
                    last_content = text
        return last_content


def _data_url_to_temp_file(data_url: str) -> str:
    """Convert a base64 data URL to a temporary file path."""
    m = re.match(r"data:image/(\w+);base64,(.+)", data_url)
    if not m:
        raise ValueError("Invalid image data URL")
    ext = m.group(1)
    b64 = m.group(2)
    data = base64.b64decode(b64)
    fd, tmp_path = tempfile.mkstemp(suffix=f".{ext}")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp_path
