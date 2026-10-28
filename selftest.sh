#!/bin/sh
# Self-test. Writes only temporary files and ST-* files in the checkpoint dir, and removes them.
HERE=$(cd "$(dirname "$0")" && pwd)
CG="$HERE/context_guard.py"
PY="/usr/bin/python3 $CG"
SH="/bin/sh $HERE/context_guard.sh"
D=$(/usr/bin/python3 -c "
import importlib.util
spec=importlib.util.spec_from_file_location('cg','$CG')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.load_cfg()['checkpoint_dir'])")
mkdir -p "$D"
W=$(mktemp -d)
ok=0; bad=0
chk() { # chk "name" "expected substring or EMPTY" "output"
  if [ "$2" = "EMPTY" ]; then
    if [ -z "$3" ]; then echo "  ok   $1"; ok=$((ok+1)); else echo "  FAIL $1 (expected silence, got: $(printf '%s' "$3" | head -c 80))"; bad=$((bad+1)); fi
  else
    case "$3" in *"$2"*) echo "  ok   $1"; ok=$((ok+1));; *) echo "  FAIL $1 (no '$2' in: $(printf '%s' "$3" | head -c 120))"; bad=$((bad+1));; esac
  fi
}
mk() { /usr/bin/python3 -c "
import json,sys
print(json.dumps({'type':'assistant','isSidechain':False,'gitBranch':'main','message':{'model':sys.argv[1],'usage':{'input_tokens':4,'cache_creation_input_tokens':0,'cache_read_input_tokens':int(sys.argv[2]),'output_tokens':0}}}))" "$1" "$2" > "$W/$3"; }
mk claude-opus-5 120000  s_low.jsonl
mk claude-opus-5 620000  s_warn.jsonl
mk claude-opus-5 710000  s_cp.jsonl
mk claude-opus-5 810000  s_cp2.jsonl
mk claude-opus-5 960000  s_final.jsonl
mk claude-haiku-4-5-20251001 175000 s_haiku.jsonl
mk claude-opus-5 910000  s_side.jsonl
/usr/bin/python3 -c "
import json
r={'type':'assistant','isSidechain':True,'message':{'model':'claude-opus-5','usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':990000,'output_tokens':0}}}
open('$W/s_side.jsonl','a').write(json.dumps(r)+'\n')"
j() { printf '{"hook_event_name":"%s","session_id":"ST-%s","cwd":"/tmp","transcript_path":"%s/%s"%s}' "$1" "$2" "$W" "$3" "$4"; }
chk "12%: silence"                EMPTY      "$(j Stop a s_low.jsonl | $PY)"
chk "62%: warning"                "62%"      "$(j PostToolUse b s_warn.jsonl | $PY)"
chk "62% on Stop: silence"        EMPTY      "$(j Stop c s_warn.jsonl | $PY)"
chk "71%: checkpoint"             "checkpoint requested" "$(j Stop d s_cp.jsonl | $PY)"
chk "71% again: silence"          EMPTY      "$(j Stop d s_cp.jsonl | $PY)"
chk "81%: second checkpoint"     "81%"      "$(j PostToolUse d s_cp2.jsonl | $PY)"
chk "81% again: silence"          EMPTY      "$(j PostToolUse d s_cp2.jsonl | $PY)"
chk "96%: CRITICAL"               "CRITICAL" "$(j PostToolUse e s_final.jsonl | $PY)"
chk "haiku 200k: 87%"             "200000"   "$(j PostToolUse f s_haiku.jsonl | $PY)"
chk "subagent not counted"        "910004"   "$(j Stop g s_side.jsonl | $PY)"
chk "stop_hook_active loop guard" EMPTY      "$(j Stop h s_cp.jsonl ',"stop_hook_active":true' | $PY)"
chk "warning goes to chat"       "tell the user in one line" "$(j PostToolUse w s_warn.jsonl | $PY)"
chk "English in the instruction"  "Write the file in English" "$(j Stop i s_cp.jsonl | $PY)"
chk "wrapper: no \\n on input"     "checkpoint requested" "$(j PostToolUse j s_cp.jsonl | $SH)"
printf '# CHECKPOINT: old\n' > "$D/$(date +%Y-%m-%d)-ST-k.md"
chk "existing file is rewritten"  "already exists" "$(j Stop k s_cp.jsonl | $PY)"
chk "PreCompact: tail after cutoff" "happened AFTER" "$(j PreCompact k s_cp.jsonl | $PY)"
chk "SessionStart: cutoff"        "only covers the session up to" "$(printf '{\"hook_event_name\":\"SessionStart\",\"source\":\"compact\",\"session_id\":\"ST-k\"}' | $PY)"
chk "SessionStart startup: silence" EMPTY    "$(printf '{\"hook_event_name\":\"SessionStart\",\"source\":\"startup\",\"session_id\":\"ST-k\"}' | $PY)"

# --- only the session's own checkpoint comes back: ST-k and other files exist, ST-none has none ---
ss() { printf '{"hook_event_name":"SessionStart","source":"%s","session_id":"%s"}' "$1" "$2"; }
chk "resume without own: silence" EMPTY      "$(ss resume ST-none | $PY)"
chk "compact without own: silence" EMPTY     "$(ss compact ST-none | $PY)"
chk "no session_id: silence"      EMPTY      "$(ss resume '' | $PY)"
chk "PreCompact without own"      "No checkpoint file exists" "$(j PreCompact none s_cp.jsonl | $PY)"
chk "resume: own file"            "-ST-k.md\"" "$(ss resume ST-k | $PY)"
rm -f "$D"/*ST-*.md "$D"/.state/ST-*

# --- the file name holds 8 chars of the id: the full id in the header rejects a same-prefix file ---
CT=$(mktemp -d)
res=$(/usr/bin/python3 -c "
import importlib.util, os
spec=importlib.util.spec_from_file_location('cg','$CG')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg=dict(m.load_cfg(), checkpoint_dir='$CT')
A='5e1f7e57-0000-4000-8000-00000000000a'; B='5e1f7e57-0000-4000-8000-00000000000b'
def put(name, head, t):
    p=os.path.join('$CT', name); open(p, 'w').write('# CHECKPOINT: t\n' + head + '\n'); os.utime(p, (t, t))
put('2026-01-01-5e1f7e57.md', 'session: ' + A, 1e9)
put('2026-01-02-5e1f7e57.md', 'session: 5e1f7e57 | edited by hand', 2e9)
put('2026-01-03-5e1f7e57.md', 'session: ' + B + ' | other', 3e9)
b=lambda s: os.path.basename(m.latest_checkpoint(cfg, s) or 'None')
print('A=%s B=%s' % (b(A), b(B)))")
rm -rf "$CT"
chk "same prefix, other full id: skipped" "A=2026-01-02-5e1f7e57.md" "$res"
chk "own full id: taken"          "B=2026-01-03-5e1f7e57.md" "$res"

# --- session window resolution ---
WT=$(mktemp -d)
mkdir -p "$WT/.claude"; printf '{"autoCompactWindow": 123456}\n' > "$WT/.claude/settings.json"
res=$(/usr/bin/python3 -c "
import importlib.util, os, sys
spec=importlib.util.spec_from_file_location('cg','$CG')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cfg=dict(m.load_cfg(), checkpoint_dir='$WT/cp')  # clean .state: no windows learned by live sessions
m.catalog_windows(cfg, True)                      # window table from a fresh binary scan, ~0.1 s
out=[]
out.append('catalog=%d,%s' % m.resolve_window(cfg,'claude-opus-5'))
out.append('haiku=%d,%s' % m.resolve_window(cfg,'claude-haiku-4-5-20251001'))
out.append('suffix=%d,%s' % m.resolve_window(cfg,'claude-sonnet-4-5[1m]'))
out.append('settings=%d,%s' % m.resolve_window(cfg,'claude-opus-5',cwd='$WT'))
os.environ['CLAUDE_CODE_DISABLE_1M_CONTEXT']='1'
out.append('no1m=%d,%s' % m.resolve_window(cfg,'claude-opus-5'))
del os.environ['CLAUDE_CODE_DISABLE_1M_CONTEXT']
os.environ['CLAUDE_CODE_MAX_CONTEXT_TOKENS']='300000'
out.append('env=%d,%s' % m.resolve_window(cfg,'claude-opus-5'))
del os.environ['CLAUDE_CODE_MAX_CONTEXT_TOKENS']
out.append('unknown=%d,%s' % m.resolve_window(cfg,'claude-zzz-9'))
out.append('escalated=%d,%s' % m.resolve_window(cfg,'claude-zzz-9',observed=400000))
print(' '.join(out))")
rm -rf "$WT"
chk "window: binary catalog"      "catalog=1000000,binary-catalog"     "$res"
chk "window: 200k model"          "haiku=200000,binary-catalog"        "$res"
chk "window: [1m] suffix"         "suffix=1000000,model-suffix"        "$res"
chk "window: autoCompactWindow"   "settings=123456,settings"           "$res"
chk "window: 1M disabled"         "no1m=200000,binary-catalog+1m-disabled" "$res"
chk "window: env override"        "env=300000,env:CLAUDE_CODE_MAX"     "$res"
chk "window: unknown model"       "unknown=200000,default"             "$res"
chk "window: raised by usage"     "escalated=1000000,default+observed" "$res"
rm -rf "$W" "$HERE/__pycache__"
echo "  --- passed $ok, failed $bad ---"
