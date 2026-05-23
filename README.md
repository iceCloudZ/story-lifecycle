# Story Lifecycle Manager

**Story-level AI orchestration** — hand a TAPD/Jira story to AI agents, let them design, implement, and test through multi-stage workflows.

Most AI coding tools work at the **task** level: "write a function", "fix this bug", "refactor this file". Story Lifecycle works at the **story** level: a complete requirement that goes through design → implementation → test → review, with each stage handled by a dedicated AI session.

```
STORY-123 "Add login feature"
  ├─ [design]    Claude Code  → spec + complexity assessment
  ├─ [implement] Codex CLI    → code changes
  └─ [test]      Aider        → verification + smoke test
```

## Why Story Lifecycle?

| | Story Lifecycle | Babysitter | Brain-dev |
|---|---|---|---|
| **Unit of work** | Story (requirement lifecycle) | Task (code→test→fix loop) | Task (code generation) |
| **Multi-stage** | Design → Implement → Test → Review | Single iterative loop | Single shot |
| **Mix AI CLIs** | Claude Code / Codex / Aider / Gemini per stage | Claude Code only | Claude Code only |
| **Auto-split** | Complex stories → sub-tasks with dependencies | No | No |
| **Custom workflow** | YAML profiles (3 to 14+ stages) | Fixed flow | Fixed flow |
| **Orchestration** | LangGraph state machine + LLM router | Agent loop | Agent loop |

Story Lifecycle is not another "AI writes code" tool. It's a **project manager for AI agents** — deciding *which* agent does *what*, tracking progress, handling failures, and escalating when needed.

## Key Features

### 1. Each Stage Uses a Different AI CLI

The adapter pattern lets you assign different AI tools to different stages. Design benefits from Claude Code's architectural thinking, implementation from Codex's code generation, and testing from Aider's test-first approach.

```yaml
# profiles/minimal.yaml
stages:
  design:
    cli: claude          # Claude Code for architecture analysis
    skill: "/brainstorming"
  implement:
    cli: codex           # Codex CLI for code generation
  test:
    cli: aider           # Aider for test-driven verification
```

Adding a new AI tool requires only a small adapter class — see `src/story_lifecycle/adapters/`.

### 2. Story-Driven, Not Task-Driven

Stories come from your project management tool (TAPD, Jira) and carry real business context: title, PRD link, acceptance criteria. The orchestrator treats each story as a **lifecycle** — it progresses through stages, accumulates knowledge, and produces auditable artifacts at every step.

```bash
# Create from a real requirement
story create STORY-1065520 -t "职业邮箱限制"

# Each stage produces structured output
.story-done/STORY-1065520/design.json    # {"complexity": "M", "spec_path": "docs/..."}
.story-done/STORY-1065520/implement.json # {"files_changed": [...], "summary": "..."}
```

### 3. Complexity-Aware with Auto-Subtask Delegation

The design stage evaluates story complexity (S/M/L). Large stories are automatically split into parallel sub-tasks with dependency management:

```
STORY-100 (L: "Refactor auth system")
  ├─ STORY-100-A (M) → depends on: none
  ├─ STORY-100-B (S) → depends on: STORY-100-A
  └─ STORY-100-C (M) → depends on: STORY-100-A
```

Sub-stories inherit parent knowledge and run in parallel via `ThreadPoolExecutor`. The parent story waits for all children before advancing.

### 4. Profile-Driven Workflows

Define your team's process in YAML. Start simple, add stages as needed:

```yaml
# Minimal: 3 stages for quick iterations
# Standard: 14 stages for production pipelines
# Custom: drop a YAML in ~/.story-lifecycle/profiles/
```

Each stage configures its own AI CLI, allowed providers, max retries, review gates, and expected outputs.

### 5. LangGraph State Machine + LLM Router

The orchestration engine is a proper state machine built on LangGraph:

```
plan → execute → poll → review → router → advance/retry/skip/fail
  ↑                                                  │
  └──────────────────────────────────────────────────┘
```

The **router node** decides what happens after each stage:
- **With API key**: LLM evaluates the result and decides advance/retry/skip
- **Without API key**: Rule-based fallback — works out of the box

### 6. One-Command Setup

```bash
pip install -e .
story              # First run: auto-check + offer to install missing tools
story --fix        # Or run doctor fix directly: detects package managers, installs what's missing
```

Auto-detects your platform (brew / apt / npm / pip / winget) and installs missing AI CLIs with confirmation prompts.

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. First-run setup (LLM config + environment check)
story setup

# 3. See it work — simulated lifecycle, no AI needed
story demo

# 4. Create your first real story
story create MY-001 -t "Add dark mode toggle"

# 5. Watch the board
story
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  story serve                     │
│              (FastAPI + LangGraph)               │
│                                                  │
│  Story ──► plan ──► execute ──► poll ──► review  │
│               │                              │   │
│               ◄─── advance/retry/skip/fail ◄─┘   │
│                          │                       │
│                   ┌──────┴──────┐                │
│                   │ LLM Router  │                │
│                   │ + fallback  │                │
│                   └─────────────┘                │
└──────────┬───────────┬──────────────┬────────────┘
           │           │              │
     ┌─────┴──┐  ┌─────┴──┐   ┌─────┴──┐
     │Claude  │  │Codex   │   │Aider   │   ← Adapters
     │Code    │  │CLI     │   │        │
     └────────┘  └────────┘   └────────┘
         │           │             │
     ┌───┴───────────┴─────────────┴───┐
     │     tmux / zellij + ttyd        │  ← Session management
     └─────────────────────────────────┘
```

**Module layout:**
- `orchestrator/graph.py` — LangGraph StateGraph (plan → execute → poll → review → router)
- `orchestrator/nodes.py` — node implementations + prompt rendering
- `orchestrator/router.py` — LLM routing decisions with rule-based fallback
- `adapters/` — adapter pattern for AI CLI tools (`BaseAdapter` → `ClaudeAdapter`)
- `cli/` — Click CLI with Rich TUI board, doctor, and setup wizard
- `db/models.py` — SQLite with raw SQL (story, stage_log, gate_result)
- `profiles/` — YAML stage definitions
- `prompts/` — per-stage markdown templates with `{variable}` substitution

## CLI Reference

```
story                                    Launch TUI board (first run: setup wizard)
story demo                               Run simulated lifecycle (no AI needed)
story create <KEY> -t <TITLE>            Create and start a story
story create <KEY> -t <TITLE> --dry-run  Preview prompts without executing
story create <KEY> --no-start            Create but don't start (for later resume)
story --fix                              Auto-install missing dependencies
story --serve                            Start API server (port 8180)
story setup                              Configure LLM provider & API key
story doctor                             Check environment
story --version                          Show version

# Inside TUI board:
  [n]     Create new story
  [N]     Create sub-story
  [i]     Inbox (import from TAPD/Jira)
  [e]     Enter AI session
  [s]     Skip current stage
  [f]     Mark story as failed
  [r]     Resume blocked story
  [a]     Abort story
  [q]     Quit
```

## Platform Support

| Platform | CLI + DB + TUI | AI Execution |
|----------|----------------|--------------|
| **Linux** | Full | tmux / zellij + ttyd |
| **macOS** | Full | tmux / zellij + ttyd |
| **Windows (WSL2)** | Full | tmux / zellij + ttyd |
| **Windows (native)** | Full | Git Bash pop-up window |

Linux/macOS/WSL2 use tmux or zellij for persistent sessions with web terminal (ttyd) access. On native Windows, AI sessions open in a new Git Bash window — same workflow, different window management.

## License

MIT
