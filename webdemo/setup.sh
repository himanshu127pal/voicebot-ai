#!/usr/bin/env bash
# webdemo/setup.sh — installer for the browser demo, run ON THE SERVER. Safe to re-run.
#
# Touches NOTHING that is currently running. It only:
#   - creates/updates /opt/smarthealth-webdemo (page + token server) with its own venv
#   - installs the demo worker (agent_voicebot_web.py) to /opt/livekit/web-demo
#   - installs two systemd units — it does not start or restart them
# It does not edit docker-compose.yml, livekit.yaml, sip.yaml, any trunk or dispatch
# rule, or any telephony bot. An existing .env and demo_signups.db are kept.
#
# Upgrading from the single-bot demo (smarthealth-web)? Follow WEB_DEMO.md §4 instead;
# it switches over with the old worker still running, so there's no gap.
#
# Usage:  bash setup.sh
set -euo pipefail

APP=/opt/smarthealth-webdemo
WORKER_DIR=/opt/livekit/web-demo
VENV=/opt/livekit/bot/.venv
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 1/6  sanity checks"
[ -x "$VENV/bin/python" ] || { echo "FATAL: $VENV not found. Is the telephony agent installed?"; exit 1; }
command -v python3 >/dev/null || { echo "FATAL: python3 missing"; exit 1; }
for f in server.py email_policy.py demo_bots.py countries.py requirements.txt voicebot-web.service \
         smarthealth-webdemo.service static/index.html static/styles.css static/app.js; do
  [ -f "$SRC/$f" ] || { echo "FATAL: $SRC/$f missing — copy the whole webdemo/ folder."; exit 1; }
done

echo "==> 2/6  app dir + venv at $APP"
mkdir -p "$APP/static/vendor"
cp "$SRC/server.py" "$SRC/email_policy.py" "$SRC/demo_bots.py" "$SRC/countries.py" \n   "$SRC/requirements.txt" "$APP/"
cp "$SRC/static/index.html" "$SRC/static/styles.css" "$SRC/static/app.js" "$APP/static/"
[ -f "$APP/.env" ] || cp "$SRC/.env.example" "$APP/.env"
[ -x "$APP/.venv/bin/pip" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> 3/6  livekit-client (vendored, so a demo never depends on a CDN)"
# Vendored rather than CDN-linked because conference wifi and corporate guest networks
# block plenty of CDNs, and a blank page mid-demo is fatal. Kept if already present.
if [ -s "$APP/static/vendor/livekit-client.umd.min.js" ]; then
  echo "    already present"
else
  curl -fsSL -o "$APP/static/vendor/livekit-client.umd.min.js" \
    "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"
  test -s "$APP/static/vendor/livekit-client.umd.min.js" || {
    echo "FATAL: livekit-client download failed or was empty"; exit 1; }
fi

echo "==> 4/6  demo worker -> $WORKER_DIR"
# Accept it beside webdemo/ or inside it, depending on how the folder was copied over.
WEBAGENT=""
for p in "$SRC/../agent_voicebot_web.py" "$SRC/agent_voicebot_web.py"; do
  [ -f "$p" ] && WEBAGENT="$p" && break
done
[ -n "$WEBAGENT" ] || {
  echo "FATAL: agent_voicebot_web.py not found next to or inside webdemo/."; exit 1; }
mkdir -p "$WORKER_DIR/bots"
if [ "$(readlink -f "$WEBAGENT")" != "$(readlink -f "$WORKER_DIR/agent_voicebot_web.py")" ]; then
  cp "$WEBAGENT" "$WORKER_DIR/agent_voicebot_web.py"
fi
# Demo-only bots (mortgage, energy): no telephony service, so the worker loads them
# from $WORKER_DIR/bots. Their optional <bot>.env files there are never overwritten.
# The other bots are read from their own telephony directories and not copied here.
WEBBOTS="$(cd "$(dirname "$WEBAGENT")" && pwd)/webbots"
if [ -d "$WEBBOTS" ]; then
  cp "$WEBBOTS"/agent_*.py "$WORKER_DIR/bots/"
else
  echo "    note: no webbots/ folder beside the worker — demo-only bots not installed"
fi

echo "==> 5/6  which voicebots the worker can find"
# A missing file doesn't break the others — that bot refuses its demo calls with a
# message. Take it out of DEMO_BOTS in $APP/.env so it isn't offered at all.
for f in /opt/livekit/bot/agent_smarthealth_claude_dg.py \
         /opt/livekit/roof-bot/agent_roof_uk.py \
         /opt/livekit/finance-bot/agent_finance_au.py \
         /opt/livekit/movers-bot/agent_movers_au.py \
         /opt/livekit/warranty-bot/agent_warranty_uk.py \
         "$WORKER_DIR/bots/agent_mortgage_leads_au.py" \
         "$WORKER_DIR/bots/agent_energy_assist_au.py"; do
  if [ -f "$f" ]; then echo "    ok       $f"; else echo "    MISSING  $f"; fi
done

echo "==> 6/6  systemd units"
cp "$SRC/smarthealth-webdemo.service" /etc/systemd/system/
cp "$SRC/voicebot-web.service"        /etc/systemd/system/
systemctl daemon-reload

echo "==> done — NOT started yet, on purpose."
cat <<'NEXT'

Fresh install, next in order (details in WEB_DEMO.md §3):

  1. Edit /opt/smarthealth-webdemo/.env
       LIVEKIT_API_KEY / LIVEKIT_API_SECRET   copy from /opt/livekit/livekit.yaml
       PUBLIC_LIVEKIT_URL=wss://<your-demo-hostname>
       OTP_API_KEY, DEMO_SESSION_SECRET       see the comments in the file

  2. DNS A record → this server, then Caddy (TLS is not optional: no HTTPS, no mic).

  3. Open the media ports (WebRTC audio does NOT go through Caddy):
       80,443/tcp   7881/tcp   50000-60000/udp

  4. Start:
       systemctl enable --now voicebot-web smarthealth-webdemo
       journalctl -u voicebot-web -n 30 --no-pager    # expect: registered worker

  5. Verify telephony is STILL fine before you demo anything:
       systemctl is-active smarthealth-bot roof-bot finance-bot movers-bot warranty-bot

Upgrading from smarthealth-web? Stop here and follow WEB_DEMO.md §4.
NEXT
