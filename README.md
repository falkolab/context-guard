# context-guard

A Claude Code hook that watches how full a session's context window is and makes
the session write a structured checkpoint in time, before auto-compaction throws the
details away. After a compaction or a resume it puts that checkpoint back into context.

Plain Python 3 and `/bin/sh`, no dependencies.

## What happens at each threshold

| Threshold | Event | What it does |
|---|---|---|
| 80% | PostToolUse | Asks the model to keep the work freezable: no whole-file reads without need, important facts go into replies |
| 90% | PostToolUse / Stop | Demands a checkpoint written to a strict template at `~/.claude/checkpoints/<date>-<first 8 chars of session id>.md` |
| 95% | PostToolUse / Stop | Demands the checkpoint be rewritten in full, folding in everything since the previous one |
| before compaction | PreCompact | Tells the compactor what the summary must keep, and to spell out in detail what happened AFTER the checkpoint |
| after compaction, resume, fork | SessionStart | Puts this session's checkpoint back into context, marked with the time it covers up to; if the session has none, says nothing |

Each threshold fires once per session. Stop is the end-of-turn safety net: a threshold
can be crossed inside one long turn, so PostToolUse can demand the checkpoint on its own
without waiting for the reply to end.

## Install

```sh
git clone https://github.com/falkolab/context-guard ~/.claude/hooks/context-guard
sh ~/.claude/hooks/context-guard/selftest.sh
```

Then add the hooks to `~/.claude/settings.json` (merge with any `hooks` you already have):

```json
{
  "hooks": {
    "PostToolUse": [
      { "hooks": [ { "type": "command", "timeout": 15,
        "command": "/bin/sh ~/.claude/hooks/context-guard/context_guard.sh" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "timeout": 15,
        "command": "/usr/bin/python3 ~/.claude/hooks/context-guard/context_guard.py" } ] }
    ],
    "PreCompact": [
      { "hooks": [ { "type": "command", "timeout": 15,
        "command": "/usr/bin/python3 ~/.claude/hooks/context-guard/context_guard.py" } ] }
    ],
    "SessionStart": [
      { "matcher": "compact|resume|fork",
        "hooks": [ { "type": "command", "timeout": 15,
        "command": "/usr/bin/python3 ~/.claude/hooks/context-guard/context_guard.py" } ] }
    ]
  }
}
```

If your shell does not expand `~` in hook commands, use the absolute path. The shell
prefilter expects the default `checkpoint_dir`; if you change it, change `D` in
`context_guard.sh` too.

## Files

| File | Purpose |
|---|---|
| `context_guard.py` | All the logic |
| `context_guard.sh` | Prefilter for PostToolUse: the fast path starts no process |
| `context_guard.json` | Thresholds and the on/off switch |
| `selftest.sh` | 30 checks; touches no live session |
| `~/.claude/checkpoints/` | The checkpoints themselves |
| `~/.claude/checkpoints/.state/` | Per-session state, skip counters, window table cache |

## How context usage is counted

The hook takes the last main-thread assistant record from the session transcript
(subagents are filtered out) and adds up
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`.
That is exactly what goes into the next request, the same number `/context` shows.
Against Claude Code itself: 116 029/116 734, 192 345/191 153, 222 598/221 363. The gap is
a steady ~0.6%, because the hook reads the file one reply later.

The transcript tail is read in chunks of 256 KB, then 2 MB, then the whole file: large
tool outputs easily push the last assistant reply megabytes back.

## How the window is determined

The harness passes the hook neither the model nor the window. The model comes from the
transcript, so a model switch mid-session is picked up from the next reply. The window
is resolved through a chain, most trusted first:

1. session env: `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`;
2. the `autoCompactWindow` setting (project local, then project, then user);
3. `"window"` in `context_guard.json`;
4. the `[1m]` suffix in the model name;
5. the window measured on a real auto-compaction of this model (`tokens / 0.97`): a fact beats the table;
6. the `context:{window:...}` table extracted from the installed Claude Code binary and
   cached by its signature (`path:mtime:size`). It rebuilds itself when Claude Code is
   updated; the scan takes ~0.1 s and runs only on rare events, never on PostToolUse;
7. a built-in snapshot of the table, and aliases for short names;
8. a conservative 200 000.

On top: `CLAUDE_CODE_DISABLE_1M_CONTEXT` cuts the window to 200k; if actual usage is
already above the computed window, the window is raised, since otherwise compaction
would have happened. Neither adjustment applies when the value is set explicitly (1–3).

The source of the window is written to `.state/<session_id>.json` as `window_source`.

## The checkpoint

Written in English, because more facts fit into the same token budget; the reply to the
user stays in the user's language. Sections: the task and what counts as done; what was
done; where it stopped; decisions and rejected options; environment and coordinates;
dead ends; next steps; how to verify; open questions. Rules: no placeholders, absolute
paths, verbatim errors, "not verified" instead of guesses, secrets by variable name only,
up to ~400 lines.

Only `<date>-<first 8 chars of session id>.md` counts as the session's own file; if its
`session:` line holds the full id, that id must match the session. A session without its
own file gets nothing back on SessionStart, and PreCompact tells the compactor that the
summary will be the only record.

## Configuration

```json
{
  "enabled": true,
  "window": null,
  "windows": {},
  "default_window": 200000,
  "warn_at": 0.80,
  "checkpoint_at": 0.90,
  "final_at": 0.95,
  "checkpoint_dir": "~/.claude/checkpoints"
}
```

`"enabled": false` turns the hook off; edits take effect at once, without restarting
sessions. To remove it entirely, delete its entries from `hooks` in `~/.claude/settings.json`.

## Testing

```sh
sh ~/.claude/hooks/context-guard/selftest.sh
```

30 checks: thresholds and that each fires once, subagent filtering, the
`stop_hook_active` loop guard, stdin without a trailing newline, rewriting an existing
checkpoint, the PreCompact and SessionStart instructions, restoring only the session's
own checkpoint (seven cases), eight window resolution cases. Window checks run on a clean
temporary state, so windows learned by live sessions do not affect them. The test creates
and removes its own temporary files.

## Performance

- PostToolUse fast path: ~3.6 ms, no child processes.
- Full path through python: ~31 ms, at most once per 25 tool calls, and on every call
  near a threshold. The skip counter is computed from the headroom to the next threshold
  not yet passed and lives in `.state/<session_id>.skip`.
- Average cost per tool call: about 5 ms.

## Known limitations

- The hook can only demand a checkpoint; the model writes it.
- If a model is newer than the installed Claude Code build, its window is unknown: 200k
  is used, and raised as soon as actual usage proves it wrong.
- State files in `.state/` are not cleaned up automatically (tens of bytes per session).

## You might also like

[chekhov-skill](https://github.com/falkolab/chekhov-skill) — a Claude Code skill that makes
replies terse the way Chekhov edited prose: no preamble, no summing up, only the details
that fire.

[agent-mail-skill](https://github.com/falkolab/agent-mail-skill) — a Claude Code skill for
file-based mail between agents across repositories, worktrees and sessions.

## License

MIT

## Author

Andrei Tkachenko, Telegram channel "Automate It" (Rus): [@aitomateit](https://t.me/aitomateit)
