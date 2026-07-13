# Explicit Story Execution Mode Design

## Problem

Claude headless execution was introduced for SWE-bench, then became the
unconditional default when the TUI integration was removed. Ordinary stories
therefore run through `claude -p`, consume unattended quota, and cannot be
observed or controlled from the Web Board terminal.

The graph also assumes every dispatched command finishes synchronously. An
interactive command that returns control to the graph without a done file is
incorrectly classified as `HeadlessNoDoneFile`.

## Decision

Execution mode is explicit profile data:

- `interactive_pty` is the default for ordinary stories.
- `headless` must be explicitly selected by benchmark or CI profiles.
- Stage-level configuration may override the profile default.
- Adapter capability never selects the mode.

SWE-bench and Headless Smoke profiles explicitly use `headless`. Existing
ordinary profiles explicitly use `interactive_pty` for readability, while the
resolver also defaults missing values to `interactive_pty`.

## Interactive Data Flow

1. The graph resolves the stage execution mode and passes it to the tool.
2. The tool starts or reuses a per-story agent PTY and injects the rendered
   prompt.
3. The tool records an active execution marker containing stage, mode, adapter,
   and attempt.
4. The graph ends the current invocation without an error and leaves the story
   active.
5. A server task watches only active stories with an interactive execution
   marker.
6. When `.story/done/{story_key}/{stage}.json` appears, the watcher resumes the
   story.
7. The graph consumes the done file, clears the marker, and continues through
   review and routing.

Opening the Web Board terminal starts or reuses the profile's interactive agent
PTY. It never creates a placeholder shell and never replaces a running Claude
process.

## Restart And Duplicate Protection

The active execution marker is persisted in story context. Before planning, a
story with a current-stage marker or done file skips repeated planner work.
Before dispatch, an alive agent PTY plus a matching marker is treated as an
already-running stage, so the prompt is not injected twice.

After a server restart the in-memory PTY is absent. The marker allows the graph
to recognize the pending stage, and it starts a replacement interactive agent
instead of switching to headless.

## Error Handling

- Explicit `headless` with an unsupported adapter fails visibly.
- Unknown execution mode values fail before launching a process.
- Interactive PTY launch failures set `last_error` and are routed normally.
- An interactive process exiting without a done file remains diagnosable, but
  is never reported as a headless failure.

## Tests

Regression tests cover:

- Ordinary execution defaults to interactive PTY even when Claude supports
  headless mode.
- Explicit headless execution still calls the headless adapter command.
- Interactive dispatch routes to graph end without `HeadlessNoDoneFile`.
- Matching active executions are not dispatched twice.
- Profile resolution preserves profile and stage execution modes.
- The done watcher resumes only ready interactive stories.
