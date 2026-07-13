#!/usr/bin/env python
"""Verify ``--resume`` preserves a task-relevant HITL input through compaction.

Drives a design-stage story's interactive claude session over WebSocket using the
documented WS-direct method (docs/handoff-design-hitl.md §10/§11) — bypassing the
front-end terminal (whose 2-col resize + queue-stealing corrupts programmatic
reads):

  spawn (NEW or RESUME)  ──WS──►  inject a hard constraint (bracketed paste)
  clean-exit (DELETE /api/pty → /exit so claude flushes its transcript)
  spawn (RESUME again)   ──WS──►  ask → check the answer recalls the constraint

Why this exists: claude only flushes ``~/.claude/projects/<proj>/<uuid>.jsonl`` on
a clean ``/exit``; a force-kill truncates it (this once masqueraded as
"--session-id is a no-op"). Long (400KB+) transcripts get **compacted** on resume
— this script checks whether a *task-relevant* HITL input survives that
compaction. Unrelated tangents are already known to be dropped (the "口令测试" in
the existing 406KB transcript); the open question is whether a task-relevant
decision survives.

Run from the repo root with the editable venv (it's a standalone dev script,
not part of the importable package):

    .venv-monorepo-test/Scripts/python.exe packages/story-lifecycle/scripts/verify_resume_hitl.py \\
        tapd-1144381896001066752 --smoke

    # full flow (inject → clean-exit → resume → ask):
    .venv-monorepo-test/Scripts/python.exe packages/story-lifecycle/scripts/verify_resume_hitl.py \\
        tapd-1144381896001066752 \\
        --constraint "评级字段必须用 tinyint(0-5),不用 varchar" \\
        --question   "评级字段我要求用什么数据类型"

Output: a UTF-8 capture file (``--out``, default tmp_resume_verify_capture.txt``)
so we can Read it back without the gbk console mangling emoji/CJK. Prints a
VERDICT line.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import time
import urllib.request
import urllib.error
import uuid

try:
    import websockets  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise SystemExit("websockets not installed (need .venv-monorepo-test)") from exc

BASE = os.environ.get("STORY_SERVE", "http://127.0.0.1:8180")
WS_BASE = BASE.replace("http://", "ws://")
NAMESPACE = uuid.NAMESPACE_DNS
BRACKET_OPEN = b"\x1b[200~"
BRACKET_CLOSE = b"\x1b[201~"


# ---- serve REST helpers (stdlib urllib — no extra deps) ----

def _http(method: str, path: str, body: dict | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def spawn_session(story_key: str):
    """POST /api/story/{key}/sessions/spawn → (session_id, resumed)."""
    code, resp = _http("POST", f"/api/story/{story_key}/sessions/spawn", body={}, timeout=120)
    if code != 200 or not isinstance(resp, dict):
        raise RuntimeError(f"spawn failed: HTTP {code} {resp}")
    return resp.get("session_id"), bool(resp.get("resumed"))


def clean_exit_all(timeout: float = 60.0):
    """DELETE /api/pty → cleanup_all(prefer_clean_exit=True). Sends /exit to every
    PTY, waits for them to die (flush transcript), force-kills the rest."""
    code, resp = _http("DELETE", "/api/pty", timeout=timeout)
    return code == 200, resp


def transcript_for(story_key: str, stage: str = "design"):
    """Locate claude's persisted transcript for story+stage (deterministic uuid5)."""
    sid = str(uuid.uuid5(NAMESPACE, f"{story_key}:{stage}"))
    home = os.path.expanduser("~/.claude/projects")
    cands = glob.glob(os.path.join(home, "*", sid + ".jsonl"))
    return cands[0] if cands else None, sid


def transcript_user_texts(path: str, limit: int = 200):
    """Extract human (non-tool-result) user messages from a transcript JSONL."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "user":
                continue
            c = o.get("message", {}).get("content")
            txt = ""
            if isinstance(c, str):
                txt = c
            elif isinstance(c, list):
                parts = []
                for p in c:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                txt = " ".join(parts)
            if txt.strip() and "tool_use_id" not in txt:
                out.append(txt)
            if len(out) >= limit:
                break
    return out


# ---- WS helpers ----

async def ws_read_for(ws, seconds: float, sink: list[str]):
    """Read PTY output for `seconds`, decode into `sink`. Best-effort, non-fatal."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.2, deadline - time.time()))
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            sink.append(f"[ws recv error: {e}]")
            break
        if isinstance(msg, bytes):
            sink.append(msg.decode("utf-8", "replace"))
        else:
            sink.append(str(msg))


async def ws_send_line(ws, text: str, paste_delay: float = 0.4):
    """Submit a line to claude's Ink TUI via bracketed paste + \\r.

    Bare PTY writes are treated as paste and don't submit (claude-code#15553);
    bracketed paste fills the input box, then \\r submits it.
    """
    payload = BRACKET_OPEN + text.encode("utf-8") + BRACKET_CLOSE
    await ws.send(payload)
    await asyncio.sleep(paste_delay)
    await ws.send(b"\r")


# ---- main flow ----

async def run(args):
    out = []
    out.append(f"=== resume HITL verify @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    out.append(f"story={args.story_key} mode={'smoke' if args.smoke else 'full'}")

    tpath, sid = transcript_for(args.story_key, args.stage)
    out.append(f"session uuid5={sid}")
    out.append(f"transcript={tpath}")
    pre_size = os.path.getsize(tpath) if tpath and os.path.exists(tpath) else 0
    pre_mtime = os.path.getmtime(tpath) if tpath and os.path.exists(tpath) else 0
    out.append(f"transcript pre: size={pre_size} mtime={pre_mtime}")

    # --- phase 1: spawn (NEW or RESUME) + capture ---
    out.append("\n--- phase 1: spawn + capture ---")
    sid1, resumed1 = spawn_session(args.story_key)
    out.append(f"spawn1: session_id={sid1} resumed={resumed1}")
    cap1 = []
    async with websockets.connect(f"{WS_BASE}/ws/pty/{args.story_key}/{sid1}",
                                  max_size=None) as ws:
        await ws_read_for(ws, args.boot_read, cap1)
    out.append(f"captured phase1 {len(''.join(cap1))} chars")
    out.append("".join(cap1)[-3000:])  # tail

    if args.smoke:
        # smoke: just clean-exit + confirm transcript flushed, then stop
        ok, resp = clean_exit_all(timeout=90)
        out.append(f"\n--- smoke: clean-exit DELETE /api/pty -> ok={ok} resp={resp}")
        await asyncio.sleep(1.0)
        post_size = os.path.getsize(tpath) if tpath and os.path.exists(tpath) else 0
        post_mtime = os.path.getmtime(tpath) if tpath and os.path.exists(tpath) else 0
        flushed = post_mtime > pre_mtime or post_size > pre_size
        out.append(f"transcript post: size={post_size} mtime={post_mtime}")
        out.append(f"VERDICT: smoke flushed={flushed} (resumed={resumed1})")
        _write(args.out, out)
        return

    # --- phase 2: inject constraint (still phase-1 session) ---
    # Re-spawn to get a fresh WS handle (the previous one closed). On RESUME this
    # continues the same session; on NEW it's the seeded one.
    out.append("\n--- phase 2: inject constraint ---")
    sid2, resumed2 = spawn_session(args.story_key)
    out.append(f"spawn2: session_id={sid2} resumed={resumed2}")
    cap2 = []
    async with websockets.connect(f"{WS_BASE}/ws/pty/{args.story_key}/{sid2}",
                                  max_size=None) as ws:
        await ws_read_for(ws, args.boot_read, cap2)  # let it settle
        await ws_send_line(ws, args.constraint)
        await ws_read_for(ws, args.ack_read, cap2)
    out.append(f"phase2 captured {len(''.join(cap2))} chars")
    out.append("".join(cap2)[-2000:])

    # --- phase 3: clean-exit (flush transcript) ---
    out.append("\n--- phase 3: clean-exit (flush) ---")
    ok, resp = clean_exit_all(timeout=90)
    out.append(f"clean-exit ok={ok} resp={resp}")
    await asyncio.sleep(1.0)
    mid_size = os.path.getsize(tpath) if tpath and os.path.exists(tpath) else 0
    out.append(f"transcript after inject: size={mid_size} (was {pre_size})")

    # --- phase 4: resume again + ask ---
    out.append("\n--- phase 4: resume + ask ---")
    sid3, resumed3 = spawn_session(args.story_key)
    out.append(f"spawn3: session_id={sid3} resumed={resumed3}")
    cap3 = []
    async with websockets.connect(f"{WS_BASE}/ws/pty/{args.story_key}/{sid3}",
                                  max_size=None) as ws:
        await ws_read_for(ws, args.boot_read, cap3)  # let resume settle
        await ws_send_line(ws, args.question)
        await ws_read_for(ws, args.ack_read, cap3)
    joined = "".join(cap3)
    out.append(f"phase4 captured {len(joined)} chars")

    # --- verdict ---
    needle = args.expect or "tinyint"
    found = needle.lower() in joined.lower()
    # also check the persisted transcript's user messages for the constraint
    tmsgs = transcript_user_texts(tpath, limit=400) if tpath and os.path.exists(tpath) else []
    constraint_in_transcript = any(args.constraint[:20] in m for m in tmsgs)
    out.append(f"\nconstraint in transcript user-msgs: {constraint_in_transcript}")
    out.append(f"answer mentions '{needle}': {found}")
    out.append("".join(cap3)[-2500:])
    if found:
        out.append("VERDICT: PASS — task-relevant HITL input survived resume (recalled).")
    else:
        out.append("VERDICT: NEEDS-REVIEW — answer did not mention the constraint; "
                   "check capture (claude may still be booting/compacting).")
    _write(args.out, out)


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[wrote {path}]")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("story_key")
    p.add_argument("--stage", default="design")
    p.add_argument("--constraint", default="记住一条硬约束:评级字段必须用 tinyint(0-5),不用 varchar。回复\"已记住\"后停下。")
    p.add_argument("--question", default="评级字段我要求用什么数据类型?只回答类型名。")
    p.add_argument("--expect", default="tinyint")
    p.add_argument("--smoke", action="store_true",
                   help="just spawn+capture+clean-exit+flush-check (no inject/ask)")
    p.add_argument("--boot-read", type=float, default=90.0, help="seconds to read after spawn/resume")
    p.add_argument("--ack-read", type=float, default=45.0, help="seconds to read after send")
    p.add_argument("--out", default="tmp_resume_verify_capture.txt")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
