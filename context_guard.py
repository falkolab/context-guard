#!/usr/bin/env python3
"""
context_guard.py: watches how full the context window of a Claude Code session is.

At 60% it asks the model to keep the work compact, at 70% and 80% it demands a
structured checkpoint, before auto-compaction it tells the compactor what the summary must keep,
and after a compaction or resume it puts the session's own checkpoint back into context.

Events: PostToolUse, Stop, PreCompact, SessionStart. Dispatch is by hook_event_name
in the input JSON (stdin).

Config: context_guard.json next to this file
  {
    "enabled": true,
    "window": null,          // force the window size in tokens (null = by model)
    "windows": {},           // per-model-id overrides
    "default_window": 200000,// for unknown models (better early than never)
    "warn_at": 0.60,
    "checkpoint_at": [0.70, 0.80], // a number or a list of thresholds
    "final_at": 0.95,
    "checkpoint_dir": "~/.claude/checkpoints"
  }
"""
import json
import os
import re
import shutil
import sys
import time

HOME = os.path.expanduser("~")
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context_guard.json")

# Frozen snapshot of the window table (Claude Code 2.1.274), used only when the
# binary cannot be read. The primary source is catalog_windows(): the same table
# is extracted from the installed build and cached per build.
MODEL_WINDOWS = {
    "claude-fable-5": 1000000,
    "claude-fable-5-1": 1000000,
    "claude-mythos-5": 1000000,
    "claude-mythos-5-1": 1000000,
    "claude-opus-5": 1000000,
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4-6": 200000,
    "claude-opus-4-5": 200000,
    "claude-opus-4-1": 200000,
    "claude-opus-4": 200000,
    "claude-sonnet-5": 1000000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-5": 200000,
    "claude-sonnet-4": 200000,
    "claude-haiku-4-5": 200000,
    "claude-3-7-sonnet": 200000,
    "claude-3-5-sonnet": 200000,
    "claude-3-5-haiku": 200000,
}
def claude_binary():
    """Path to the Claude Code executable the window table is read from."""
    p = shutil.which("claude")
    if p:
        try:
            return os.path.realpath(p)
        except Exception:
            return p
    d = os.path.join(HOME, ".local", "share", "claude", "versions")
    try:
        vs = [os.path.join(d, x) for x in os.listdir(d)]
        vs = [x for x in vs if os.path.isfile(x)]
        return max(vs, key=os.path.getmtime) if vs else None
    except Exception:
        return None


_W_PAT = re.compile(rb"context:\{window:([0-9.e+]+)")
_ID_PAT = re.compile(rb'"(claude-[a-z0-9.\-]+)"')


def scan_binary_windows(path):
    """{model id: window} straight from the Claude Code binary (~0.1 s for 214 MB)."""
    out, prev = {}, b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                buf = prev + chunk
                for m in _W_PAT.finditer(buf):
                    ids = _ID_PAT.findall(buf[max(0, m.start() - 2500):m.start()])
                    if ids:
                        try:
                            w = int(float(m.group(1)))
                        except Exception:
                            continue
                        if 50000 <= w <= 20000000:
                            for mid in ids[-3:]:
                                out.setdefault(mid.decode("ascii", "ignore"), w)
                prev = buf[-4096:]
    except Exception:
        return {}
    return out


def catalog_windows(cfg, allow_scan):
    """Window table cache tied to a specific Claude Code build.
    Rebuilt only on rare events (Stop/PreCompact/SessionStart) so that
    PostToolUse stays fast."""
    cache_path = os.path.join(cfg["checkpoint_dir"], ".state", "model-windows.json")
    binp = claude_binary()
    stamp = None
    if binp:
        try:
            st = os.stat(binp)
            stamp = "%s:%d:%d" % (binp, st.st_mtime, st.st_size)
        except Exception:
            stamp = binp
    try:
        with open(cache_path) as fh:
            cached = json.load(fh)
        if cached.get("stamp") == stamp and cached.get("windows"):
            return cached["windows"]
    except Exception:
        cached = None
    if not (allow_scan and binp):
        return (cached or {}).get("windows", {}) if cached else {}
    windows = scan_binary_windows(binp)
    if windows:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path + ".tmp", "w") as fh:
                json.dump({"stamp": stamp, "scanned_at": int(time.time()),
                           "windows": windows}, fh, indent=1, sort_keys=True)
            os.replace(cache_path + ".tmp", cache_path)
        except Exception:
            pass
        return windows
    return (cached or {}).get("windows", {}) if cached else {}


# Aliases for when the transcript holds a short name instead of a model id.
ALIAS_WINDOWS = {"opus": 1000000, "fable": 1000000, "mythos": 1000000,
                 "sonnet": 1000000, "haiku": 200000}

DEFAULTS = {
    "enabled": True,
    "window": None,
    "windows": {},
    "default_window": 200000,
    "warn_at": 0.60,
    "checkpoint_at": [0.70, 0.80],
    "final_at": 0.95,
    "checkpoint_dir": os.path.join(HOME, ".claude", "checkpoints"),
    "tail_bytes": 262144,
}


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH) as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update(user)
    except Exception:
        pass
    cfg["checkpoint_dir"] = os.path.expanduser(str(cfg["checkpoint_dir"]))
    return cfg


def state_path(cfg, session_id):
    d = os.path.join(cfg["checkpoint_dir"], ".state")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return os.path.join(d, safe + ".json")


def load_state(cfg, session_id):
    try:
        with open(state_path(cfg, session_id)) as fh:
            return json.load(fh)
    except Exception:
        return {"fired": [], "max_tokens": 0}


def save_state(cfg, session_id, st):
    try:
        p = state_path(cfg, session_id)
        with open(p + ".tmp", "w") as fh:
            json.dump(st, fh)
        os.replace(p + ".tmp", p)
    except Exception:
        pass


def learned_windows(cfg):
    try:
        with open(os.path.join(cfg["checkpoint_dir"], ".state", "windows.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_learned_window(cfg, model, window):
    data = learned_windows(cfg)
    data[model or "unknown"] = window
    try:
        with open(os.path.join(cfg["checkpoint_dir"], ".state", "windows.json"), "w") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def _tail(path, tail_bytes):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()          # drop the cut-off line
        return fh.read(), size


def read_tail_facts(transcript_path, tail_bytes):
    """(tokens, model, branch, file_size). Tokens are counted the way Claude Code
    counts them: input + cache_creation + cache_read + output of the last main-thread
    reply. The tail grows until usage is found: large tool results easily push the
    last assistant reply megabytes back."""
    if not transcript_path or not os.path.exists(transcript_path):
        return 0, None, None, 0
    for limit in (tail_bytes, tail_bytes * 8, 1 << 30):
        try:
            chunk, size = _tail(transcript_path, limit)
        except Exception:
            return 0, None, None, 0
        tokens, model, branch = _scan(chunk)
        if tokens or limit >= size:
            return tokens, model, branch, size
    return 0, None, None, size


def _scan(chunk):
    tokens, model, branch = 0, None, None
    for raw in reversed(chunk.splitlines()):
        if branch is None and b'"gitBranch"' in raw:
            try:
                branch = json.loads(raw.decode("utf-8", "replace")).get("gitBranch") or None
            except Exception:
                pass
        if tokens or b'"usage"' not in raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if rec.get("isSidechain"):          # subagent replies are not our context
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        total = (int(u.get("input_tokens") or 0)
                 + int(u.get("cache_creation_input_tokens") or 0)
                 + int(u.get("cache_read_input_tokens") or 0)
                 + int(u.get("output_tokens") or 0))
        if total > 0:
            tokens, model = total, msg.get("model")
    return tokens, model, branch


def _truthy(v):
    return bool(v) and v.strip().lower() not in ("0", "false", "no", "off")


def settings_window(cwd):
    """autoCompactWindow from settings: the nearer scope wins."""
    paths = []
    if cwd:
        paths += [os.path.join(cwd, ".claude", "settings.local.json"),
                  os.path.join(cwd, ".claude", "settings.json")]
    paths += [os.path.join(HOME, ".claude", "settings.local.json"),
              os.path.join(HOME, ".claude", "settings.json")]
    for p in paths:
        try:
            with open(p) as fh:
                v = json.load(fh).get("autoCompactWindow")
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        except Exception:
            continue
    return None


def resolve_window(cfg, model, allow_scan=False, observed=0, cwd=None):
    """The window of THIS session. Order of trust:
    1) explicit session knobs: env CLAUDE_CODE_MAX_CONTEXT_TOKENS /
       CLAUDE_CODE_AUTO_COMPACT_WINDOW, the autoCompactWindow setting, the hook config;
    2) the [1m] suffix in the model name;
    3) measured on a real auto-compaction (learned): a fact beats the table;
    4) the table from the installed binary, then the built-in snapshot and aliases;
    5) a conservative default.
    On top: CLAUDE_CODE_DISABLE_1M_CONTEXT cuts the window to 200k, and actual usage
    above the computed window raises it (otherwise compaction would already have
    happened). Returns (window, where it came from)."""
    explicit = None
    src = None
    for env in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
        v = os.environ.get(env)
        if v and v.strip().isdigit():
            explicit, src = int(v.strip()), "env:" + env
            break
    if explicit is None:
        sw = settings_window(cwd)
        if sw:
            explicit, src = sw, "settings:autoCompactWindow"
    if explicit is None and cfg.get("window"):
        explicit, src = int(cfg["window"]), "config:window"

    raw = (model or "").strip()
    base = raw.split("[")[0]
    if explicit is not None:
        window = explicit
    elif "[1m]" in raw:
        window, src = 1000000, "model-suffix:[1m]"
    else:
        over = cfg.get("windows") or {}
        window = src = None
        if base in over:
            window, src = int(over[base]), "config:windows"
        if window is None:
            lw = learned_windows(cfg)       # measured on a real auto-compaction
            if base in lw and 100000 <= int(lw[base]) <= 2000000:
                window, src = int(lw[base]), "learned:auto-compact"
        if window is None:
            for table, label in ((catalog_windows(cfg, allow_scan), "binary-catalog"),
                                 (MODEL_WINDOWS, "builtin-snapshot")):
                if base in table:
                    window, src = int(table[base]), label
                    break
                hit = [k for k in sorted(table, key=len, reverse=True) if base.startswith(k)]
                if hit:
                    window, src = int(table[hit[0]]), label + ":prefix"
                    break
        if window is None and base in ALIAS_WINDOWS:
            window, src = ALIAS_WINDOWS[base], "alias"
        if window is None:
            window, src = int(cfg["default_window"]), "default"

    if _truthy(os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT", "")) and window > 200000 \
            and explicit is None:
        window, src = 200000, (src or "?") + "+1m-disabled"

    if explicit is None and observed > window * 1.02:
        window, src = (1000000 if observed < 970000 else 2000000), (src or "?") + "+observed"
    return window, src


_CP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)\.md$")
_CP_SESSION = re.compile(r"^session:[ \t]*(\S+)", re.M)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def latest_checkpoint(cfg, session_id):
    """The newest checkpoint of THIS session, or None. Never another session's: the
    session would take someone else's state for its own. Its own file is named like
    checkpoint_file(): <date>-<first 8 chars of the id>.md. The name holds only 8
    characters, so if the header records the full id (the session: line), it must match."""
    if not session_id:
        return None
    try:
        names = os.listdir(cfg["checkpoint_dir"])
    except Exception:
        return None
    own = []
    for name in names:
        m = _CP_NAME.match(name)
        if not m or m.group(1) != session_id[:8]:
            continue
        p = os.path.join(cfg["checkpoint_dir"], name)
        try:
            with open(p, errors="replace") as fh:
                s = _CP_SESSION.search(fh.read(4096))
            mtime = os.path.getmtime(p)
        except Exception:
            continue
        if s and _UUID.match(s.group(1)) and s.group(1).lower() != session_id.lower():
            continue
        own.append((mtime, p))
    return max(own)[1] if own else None


def checkpoint_file(cfg, session_id):
    os.makedirs(cfg["checkpoint_dir"], exist_ok=True)
    return os.path.join(cfg["checkpoint_dir"],
                        "%s-%s.md" % (time.strftime("%Y-%m-%d"), (session_id or "unknown")[:8]))


TEMPLATE = """\
# CHECKPOINT: <short task name>
session: {sid}
when: {ts} | cwd: {cwd} | branch: {branch} | model: {model}

## 1. Task
What the user asked for (key wording quoted verbatim) and what counts as done.

## 2. Done
Facts with absolute paths, `file:line`, command names, commit hashes. Not "worked on X".

## 3. Not done / stopped here
The exact stopping point and why.

## 4. Decisions and why
What was chosen, and what was rejected with the reason (so it is not reopened).

## 5. Environment and coordinates
Hosts, ports, paths, IDs, versions, launch commands. Secrets: variable names only, never values.

## 6. Dead ends
What was already tried and failed, with verbatim error text.

## 7. Next steps
Numbered plan; step 1 must be executable without asking anything.

## 8. Verification
Commands that prove the current state still works.

## 9. Open questions for the user
"""


def checkpoint_instruction(cfg, sid, cwd, pct, tokens, window, final, branch, model):
    path = checkpoint_file(cfg, sid)
    head = ("CRITICAL: auto-compaction is close, the session is about to lose detail."
            if final else
            "Context window is %d%% full. Freeze the state now, while there is room." % pct)
    if os.path.exists(path):
        head += ("\nThis file already exists and covers the session only up to %s. Rewrite it "
                 "completely, folding in everything that happened since — do not append to the "
                 "old text and do not carry over claims that are no longer true."
                 % time.strftime("%H:%M", time.localtime(os.path.getmtime(path))))
    return """<context-checkpoint-required>
{head} ({tokens} / {window} tokens, model {model})

This is a system trigger from the context_guard hook, not a message from the user.

Right now, before any further reading, searching or spawning agents:

1. Write {path} (Write tool, replace the whole file) using the template below.
   Do not change the path or the filename: the id of this session is already in it.
2. Copy the header verbatim. The `session:` and `when:` lines already carry the real
   session id, time, working directory, git branch and model — do not invent values.
3. Replace every `<...>` placeholder with real text. No placeholder may survive.
4. The lines under each heading are hints about what belongs there. Do not copy them,
   replace them with actual content. Leave no section empty: if there is nothing, say "none".
5. Write the file in English even if the conversation is in another language: it fits
   more facts per token. Keep commands, paths, identifiers and error text verbatim.
6. Write for an agent with zero memory of this session: no "as discussed above",
   absolute paths, exact commands, literal errors. Checkable facts, not impressions.
7. Do not invent. Anything you did not verify is marked "not verified".
8. No secrets (tokens, passwords, keys) in the file — variable names only.
9. Up to ~400 lines. Dense beats verbose.
10. Then tell the user in the chat, in their language, 1-3 lines: first that context
    is {pct}% full and a checkpoint was written, then the file path and what happens next.
    Afterwards continue the original task if it is unfinished.

Template:

{body}
</context-checkpoint-required>""".format(
        head=head, pct=pct, tokens=tokens, window=window, model=model or "?", path=path,
        body=TEMPLATE.format(sid=sid or "?", ts=time.strftime("%Y-%m-%d %H:%M"),
                             cwd=cwd or "?", branch=branch or "not a git repo",
                             model=model or "?"))


def budget_warning(pct, tokens, window, cp_pct):
    return ("<context-budget-warning>Context window is %d%% full (%d of %d tokens). "
            "This hook will require a full checkpoint at %d%%. First, tell the user in one line "
            "in their language, in your next text output: context is %d%% full, a checkpoint "
            "will be written at %d%%. Then continue the task. Until then: avoid whole-file "
            "reads and broad searches unless they are needed, keep the work in a state that "
            "can be frozen at any moment, and put important facts (paths, commands, decisions) "
            "into your replies instead of holding them only in context."
            "</context-budget-warning>" % (pct, tokens, window, cp_pct, pct, cp_pct))


def checkpoint_stages(cfg):
    """[(threshold, stage name)] in ascending order. checkpoint_at is a number or a list.
    The first stage is called "checkpoint" as before, so existing state files stay valid."""
    v = cfg["checkpoint_at"]
    ts = sorted(float(x) for x in (v if isinstance(v, (list, tuple)) else [v]))
    return [(t, "checkpoint" if i == 0 else "checkpoint-%d" % round(t * 100))
            for i, t in enumerate(ts)]


def all_stages(cfg):
    return ([(float(cfg["warn_at"]), "warn")] + checkpoint_stages(cfg)
            + [(float(cfg["final_at"]), "final")])


def plan_next_check(cfg, sid, st, tokens, window):
    """Counter for the shell prefilter: how many upcoming PostToolUse calls can be
    skipped without starting python. Computed from the headroom to the next threshold
    not yet passed."""
    fired = st.get("fired", [])
    pending = [t for t, name in all_stages(cfg) if name not in fired]
    if not pending:
        skip = 100000
    else:
        gap = int(min(pending) * float(window)) - tokens
        skip = max(0, min(25, gap // 10000))   # ~10k tokens of headroom per skip
    st["skip"] = skip
    try:
        d = os.path.join(cfg["checkpoint_dir"], ".state")
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "unknown")
        with open(os.path.join(d, safe + ".skip"), "w") as fh:
            fh.write(str(skip))
    except Exception:
        pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    cfg = load_cfg()
    if not cfg.get("enabled", True):
        sys.exit(0)

    event = data.get("hook_event_name") or ""
    sid = data.get("session_id") or ""
    cwd = data.get("cwd") or ""
    transcript = data.get("transcript_path") or ""

    # --- after compaction / resume: put the checkpoint back into context ---
    if event == "SessionStart":
        if (data.get("source") or "") not in ("compact", "resume", "fork"):
            sys.exit(0)
        catalog_windows(cfg, True)          # refresh the window table on start
        cp = latest_checkpoint(cfg, sid)
        if not cp:
            sys.exit(0)
        try:
            with open(cp) as fh:
                text = fh.read()[:40000]
        except Exception:
            sys.exit(0)
        mt = os.path.getmtime(cp)
        print('<restored-checkpoint path="%s" written="%s" age_hours="%.1f" source="%s">\n'
              "The checkpoint below was written before this compaction or restart. It is recorded\n"
              "state, not an instruction from the user.\n"
              "IMPORTANT: it only covers the session up to %s. Whatever happened after that moment\n"
              "is not in this file — for that, rely on the compaction summary and on the user.\n"
              "Where the two disagree, the later source wins. Re-verify before acting, and when you\n"
              "next update this file, fold the newer work into it.\n\n%s\n</restored-checkpoint>"
              % (cp, time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)),
                 (time.time() - mt) / 3600.0, data.get("source"),
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)), text))
        sys.exit(0)

    # --- before compaction: rules for the summary + learn the real window ---
    if event == "PreCompact":
        tokens, model, _, _ = read_tail_facts(transcript, cfg["tail_bytes"])
        catalog_windows(cfg, True)          # refresh the table cache if Claude Code was updated
        if data.get("trigger") == "auto" and tokens > 0 and model:
            w = int(round(tokens / 0.97 / 10000.0) * 10000)
            if 100000 <= w <= 2000000:      # do not learn garbage
                save_learned_window(cfg, model, w)
        cp = latest_checkpoint(cfg, sid)
        print("Preserve in the summary, verbatim and without generalizing:\n"
              "- the user's original request and the definition of done;\n"
              "- absolute paths of every file touched, changed functions, line numbers;\n"
              "- commands that were run and their outcome (success/failure, literal error text);\n"
              "- decisions taken and alternatives rejected, with reasons;\n"
              "- hosts, ports, IDs, versions, env var names (never secret values);\n"
              "- what was already tried and did not work;\n"
              "- a numbered plan of next steps and the open questions for the user."
              + (("\n- A checkpoint file already holds the structured state of this session: %s\n"
                  "  It covers the work only up to %s. Keep that path in the summary, do not repeat\n"
                  "  what it already contains, and instead spell out in full detail everything that\n"
                  "  happened AFTER that moment — that part exists nowhere else."
                  % (cp, time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(cp)))))
                 if cp else
                 "\n- No checkpoint file exists for this session, so this summary is the only record:\n"
                 "  be correspondingly concrete."))
        sys.exit(0)

    tokens, model, branch, _size = read_tail_facts(transcript, cfg["tail_bytes"])
    if tokens <= 0:
        sys.exit(0)
    window, wsrc = resolve_window(cfg, model, allow_scan=(event != "PostToolUse"),
                                  observed=tokens, cwd=cwd)
    ratio = float(tokens) / float(window)
    pct = int(ratio * 100)
    st = load_state(cfg, sid)
    st["max_tokens"] = max(st.get("max_tokens", 0), tokens)
    st["window"] = window
    st["window_source"] = wsrc
    st["model"] = model
    fired = st.setdefault("fired", [])

    # Take the highest stage reached, if it has not fired yet.
    stage = None
    for t, name in reversed(all_stages(cfg)):
        if ratio >= t:
            if name not in fired:
                stage = name
            break

    # Stop is the end-of-turn safety net; a threshold can also be crossed inside a
    # long turn, which is why PostToolUse can demand a checkpoint on its own.
    if event in ("Stop", "SubagentStop"):
        if event == "SubagentStop" or data.get("stop_hook_active") or stage == "warn":
            stage = None
    elif event != "PostToolUse":
        stage = None

    if not stage:
        plan_next_check(cfg, sid, st, tokens, window)
        save_state(cfg, sid, st)
        sys.exit(0)

    # A stage that fires also covers every lower one: at 96% the 70/80 checkpoints are moot.
    for t, name in all_stages(cfg):
        if name not in fired:
            fired.append(name)
        if name == stage:
            break
    plan_next_check(cfg, sid, st, tokens, window)
    save_state(cfg, sid, st)

    if stage == "warn":
        emit({"systemMessage": "Context %d%% (%d/%d, %s): a checkpoint will be required soon"
                               % (pct, tokens, window, model or "?"),
              "hookSpecificOutput": {"hookEventName": event,
                                     "additionalContext": budget_warning(
                                         pct, tokens, window,
                                         round(checkpoint_stages(cfg)[0][0] * 100))}})
    emit({"systemMessage": "Context %d%% (%d/%d, %s): checkpoint requested"
                           % (pct, tokens, window, model or "?"),
          "hookSpecificOutput": {"hookEventName": event,
                                 "additionalContext": checkpoint_instruction(
                                     cfg, sid, cwd, pct, tokens, window,
                                     final=(stage == "final"), branch=branch, model=model)}})


if __name__ == "__main__":
    main()
