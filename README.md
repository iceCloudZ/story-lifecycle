# Story Lifecycle Manager

AI-powered development workflow orchestrator — from TAPD/Jira story to production.

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

# First-run setup: configure LLM provider & API key
story setup

# Check system environment
story doctor

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

## LLM Router

The orchestrator uses an LLM API for routing decisions (provider selection, prompt generation). If no API key is configured, it falls back to rule-based routing automatically.

- **With API key**: LLM-driven routing with intelligent provider/model selection
- **Without API key**: Rule-based fallback — works out of the box for basic flows

Configure via `story setup` or edit `~/.story-lifecycle/config.yaml` directly.

## CLI Commands

```
story setup                        Configure LLM provider & API key
story doctor                       Check system environment
story new <KEY> --title "..."      Create a new story
story board                        Show all active stories
story enter <KEY>                  Open terminal to interact with AI
story status <KEY>                 Show story details
story skip <KEY> --stage <NAME>   Skip a stage
story fail <KEY>                   Mark as blocked
story resume <KEY>                 Resume a blocked story
story serve                        Start the orchestrator server
```

## License

MIT
