# Evaluator Loop TUI Context

## Background

`profiles/minimal.yaml` now enables adversarial evaluator-optimizer loops by default.

There are two loop types:

- Plan loop: runs inside `plan_stage_node` for `design` and `implement`.
- Code loop: runs once in `review_stage_node`; retries happen through the existing router retry path.

The loops write structured events to `event_log`:

- `evaluator_loop_started`
- `evaluator_loop_round`
- `evaluator_loop_completed`
- `evaluator_loop_fallback`

Currently the TUI does not explicitly show these loop events. Users can only infer activity from story status, retry count, `review_summary`, `repair_packet_path`, and open quality findings.

## Goal

Add a small TUI section in the story detail panel that shows recent evaluator-loop activity for the selected story.

The UI should answer:

- Is this story currently using adversarial plan/review?
- Which loop ran: `plan` or `code`?
- Which round is it on?
- What was the reviewer decision?
- Were findings new, repeated, or resolved?
- Did no-progress trigger manual confirmation?
- Is there a repair packet path to inspect?

## Data Source

Use:

```python
db.get_story_events(story_key)
```

Filter events where:

```python
event_type.startswith("evaluator_loop_")
```

Payload is JSON stored in `event_log.payload`.

Important payload fields:

- `loop_id`
- `loop_type`
- `stage`
- `mode`
- `round_id`
- `decision`
- `reason`
- `findings.open_before`
- `findings.new`
- `findings.resolved`
- `findings.repeated`
- `no_progress`
- `repair_packet_path`
- `remaining_findings`

## Suggested UI

In `_render_detail(story)` in `src/story_lifecycle/cli/tui.py`, add a compact section after Context and before Quality Findings:

```text
Evaluator Loop:
  plan/design round 1: pass
  code/implement round 2: revise, repeated=1, new=0
  code/implement completed: wait_confirm (no_progress_on_high_findings)
  repair: .story-context/KEY/repair_implement_round2.md
```

Keep it short:

- Show only the latest 5 loop events.
- Use color for decisions:
  - pass: green
  - revise: yellow
  - fail / no_progress / wait_confirm: red or magenta
- If `no_progress` is true, show a clear marker like `NO PROGRESS`.
- If there are no loop events, omit the section.

## Acceptance

- Selecting a story and pressing `d` shows recent evaluator-loop events.
- Existing story detail rendering still works when payload JSON is invalid or missing fields.
- No new DB schema is needed.
- The TUI remains readable; do not dump full JSON payloads.
