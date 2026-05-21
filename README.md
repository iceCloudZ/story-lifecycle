# Story Lifecycle Manager

AI-powered development workflow orchestrator — from TAPD/Jira story to production.

> **Work in progress — Phase 1 development.**

## Platform Support

| Platform | CLI + DB | AI Execution (tmux/ttyd) |
|----------|----------|--------------------------|
| **Linux** | Full | Full |
| **macOS** | Full | Full (install tmux: `brew install tmux ttyd`) |
| **Windows (native)** | Full | Not supported — use WSL2 |
| **Windows (WSL2)** | Full | Full |

### Why Windows native doesn't support AI execution

tmux and ttyd depend on Unix pseudo-terminals (PTY), `fork()`, and POSIX signals — APIs not available on native Windows. All major AI coding CLI tools (Claude Code, Aider) have the same limitation.

**Recommended setup on Windows:**

```powershell
# In WSL2 Ubuntu
sudo apt install tmux ttyd
pip install story-lifecycle
story serve
```

## Quick Start

```bash
# Install
pip install story-lifecycle       # not yet on PyPI — use `pip install -e .`

# Start orchestrator in one terminal
story serve

# Create a story in another terminal
story new STORY-123 --title "Add login feature"

# Watch progress
story board

# Interact with the AI (Linux/macOS/WSL only)
story enter STORY-123
```

## Profiles

- `minimal` (default): design → implement → test (3 stages)
- `standard`: full 14-stage flow (coming in Phase 2)
- Custom: drop a YAML in `~/.story-lifecycle/profiles/`

## CLI Commands

```
story new <KEY> --title "..."     Create a new story
story board                       Show all active stories
story enter <KEY>                 Open terminal to interact with AI
story status <KEY>                Show story details
story skip <KEY> --stage <NAME>   Skip a stage
story fail <KEY>                  Mark as blocked
story serve                       Start the orchestrator server
```

## License

MIT
