#!/usr/bin/env bash
# Linux smoke test in Docker: pytest (with the tmux integration test), then the agent against a real tmux.
#   scripts/linux-smoke.sh            build the image and run both stages (needs Docker)
#   scripts/linux-smoke.sh --inside   second stage only, meant to run inside the container
set -u

IMAGE="${NEONDECK_SMOKE_IMAGE:-neondeck-linux-smoke}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" != "--inside" ]; then
  command -v docker >/dev/null 2>&1 || { echo "FAIL: docker non trovato"; exit 1; }
  echo "== build =="
  docker build -f "$ROOT/tests/docker/Dockerfile.linux" -t "$IMAGE" "$ROOT" || { echo "FAIL: build dell'immagine"; exit 1; }
  echo "== stage 1: pytest (incluso il test di integrazione tmux) =="
  docker run --rm "$IMAGE" uv run --no-sync pytest -q -rs || { echo "FAIL: pytest"; exit 1; }
  echo "== stage 2: agent contro un tmux reale =="
  docker run --rm "$IMAGE" bash scripts/linux-smoke.sh --inside
  exit $?
fi

# ---------------------------------------------------------------- inside the container
export NEONDECK_HOME=/tmp/nd
ND="${ND:-/app/.venv/bin/neondeck}"
PORT=8787
FAILS=0
AGENT_PID=""

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILS=$((FAILS + 1)); }
check() { if eval "$2"; then pass "$1"; else fail "$1"; fi; }

cleanup() {
  [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null
  tmux kill-server 2>/dev/null
}
trap cleanup EXIT

rm -rf "$NEONDECK_HOME" "$HOME/.claude"
mkdir -p "$NEONDECK_HOME" "$HOME/.claude/sessions"
echo "{\"bind\": \"127.0.0.1\", \"port\": $PORT}" > "$NEONDECK_HOME/config.json"
cat > "$NEONDECK_HOME/buttons.json" <<'JSON'
[{"id": "say", "title": "say", "icon": {"type": "emoji", "value": "x"},
  "action": {"type": "send_keys", "text": "hello-smoke\r", "target": "selected"}}]
JSON

# tmux: pane 1 runs the fake Claude process, panes 2 and 3 are plain terminals
unset TMUX
tmux new-session -d -s smoke -x 120 -y 30 "sleep 1000"
tmux split-window -t smoke "cat"
tmux new-window -t smoke "cat"
sleep 1
CLAUDE_PID=$(pgrep -f '^sleep 1000$' | head -n 1)
[ -n "$CLAUDE_PID" ] || { echo "FAIL: il processo sleep non e' partito"; tmux list-panes -a; exit 1; }
cat > "$HOME/.claude/sessions/$CLAUDE_PID.json" <<JSON
{"pid": $CLAUDE_PID, "sessionId": "smoke-1", "cwd": "/tmp/smoke-project", "kind": "interactive",
 "status": "idle", "name": "smoke", "startedAt": 1, "updatedAt": 1}
JSON

"$ND" token >/dev/null  # creates the token before the agent starts: no race on config.json
"$ND" run > "$NEONDECK_HOME/agent.log" 2>&1 &
AGENT_PID=$!
TOKEN=""
for _ in $(seq 1 40); do
  TOKEN=$("$ND" token 2>/dev/null)
  curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/buttons" >/dev/null 2>&1 && break
  sleep 0.5
done
check "l'agent risponde su 127.0.0.1:$PORT" 'curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/buttons" >/dev/null'

SESSIONS=$(curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/sessions")
echo "$SESSIONS" | python3 -m json.tool | head -60

py_check() {  # py_check "description" "python expression over `data`"
  if echo "$SESSIONS" | python3 -c "import json,sys; data=json.load(sys.stdin); sys.exit(0 if ($2) else 1)"; then
    pass "$1"; else fail "$1"; fi
}
py_check "la sessione Claude e' un tile con host tmux" \
  "any(s['id']=='claude:$CLAUDE_PID' and s['kind']=='claude' and s['host']=='tmux' for s in data['sessions'])"
py_check "la sessione Claude e' controllable" \
  "any(s['id']=='claude:$CLAUDE_PID' and s['controllable'] is True for s in data['sessions'])"
py_check "gli altri pannelli diventano tile terminale tmux (2)" \
  "len([s for s in data['sessions'] if s['kind']=='terminal' and s['host']=='tmux' and s['id'].startswith('tmux:%')])==2"
py_check "il pannello di Claude non ha un tile terminale doppio" \
  "len([s for s in data['sessions'] if s['kind']=='terminal'])==2"
py_check "il backend tmux e' elencato come attivo" "any(t['id']=='tmux' for t in data['terminals'])"

# send_text: the button types into the first terminal tile
TARGET=$(echo "$SESSIONS" | python3 -c "import json,sys; print(next(s['id'] for s in json.load(sys.stdin)['sessions'] if s['kind']=='terminal'))")
PANE=${TARGET#tmux:}
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\": \"say\", \"target_id\": \"$TARGET\"}" "http://127.0.0.1:$PORT/api/press" | tee /tmp/press.json; echo
check "il pulsante risponde ok" 'python3 -c "import json; assert json.load(open(\"/tmp/press.json\"))[\"ok\"]"'
sleep 0.5
check "il testo e' arrivato nel pannello tmux (capture-pane)" 'tmux capture-pane -p -t "$PANE" | grep -q hello-smoke'

# focus without an attached client: tmux selects, the agent reports it
FOCUS=$(curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\": \"$TARGET\"}" "http://127.0.0.1:$PORT/api/focus")
echo "$FOCUS"
check "focus senza client tmux: errore chiaro in italiano" 'echo "$FOCUS" | grep -q "Nessun client tmux collegato"'

# CLI
STATUS=$("$ND" status 2>&1)
echo "$STATUS"
check "neondeck status elenca tmux attivo" 'echo "$STATUS" | grep -q "tmux: attivo"'
check "neondeck install non fallisce senza systemd" '"$ND" install --no-vscode >/tmp/install.log 2>&1; [ $? -eq 0 ]'
check "l'unit systemd e' stata scritta" '[ -f "$HOME/.config/systemd/user/neondeck.service" ]'

echo
if [ "$FAILS" -eq 0 ]; then echo "PASS: smoke test Linux completato"; exit 0; fi
echo "--- agent.log ---"; tail -40 "$NEONDECK_HOME/agent.log"
echo "FAIL: $FAILS controlli falliti"
exit 1
