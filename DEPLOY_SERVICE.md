# Running the agent as a systemd service

Takes the worker from a terminal-bound `dev` run to a supervised service that survives
reboots, crashes and SSH disconnects.

**Two things change vs what you've been doing:**
1. `dev` → **`start`**. `dev` is verbose, deprecated, and tied to your terminal.
2. The process is supervised — restarted on crash, started on boot.

---

## 0. Before you install — check for a conflicting worker

Workers are addressed by `agent_name`, not by filename. Two workers registering the same
name means LiveKit **load-balances calls between them** — so half your calls would hit an
old build. Check nothing else is already running as `smarthealth-bot`:

```bash
systemctl list-units --type=service | grep -Ei 'naina|smarthealth|livekit'
ps aux | grep -E 'agent_.*\.py' | grep -v grep
```

Stop/disable anything stale:

```bash
systemctl stop naina-bot && systemctl disable naina-bot     # example
```

Also make sure your own `dev` run is stopped (Ctrl-C in that terminal).

## 1. One-time: fetch the Silero VAD model

`prewarm()` loads it per worker process; it must already be on disk.

```bash
cd /opt/livekit/bot
./.venv/bin/python agent_smarthealth_claude_v2.py download-files
```

## 2. Install the unit

```bash
cp /opt/livekit/bot/smarthealth-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable smarthealth-bot      # start on boot
systemctl start  smarthealth-bot
```

## 3. Verify

```bash
systemctl status smarthealth-bot
journalctl -u smarthealth-bot -f
```

Healthy startup shows, in order:

```
plugin registered {"plugin": "livekit.plugins.anthropic", ...}
[smarthealth] LLM: claude-haiku-4-5 (strict_tool_schema=False, caching=ephemeral, ...)
[smarthealth] turn handling: VAD interruption (min_words=2), adaptive detector disabled
registered worker {"agent_name": "smarthealth-bot", ...}
```

`registered worker` is the one that matters — until it appears, no call will route here.

Then place a test call and confirm the greeting.

## 4. Prove it survives a reboot

Don't assume `enable` worked — verify:

```bash
systemctl is-enabled smarthealth-bot     # -> enabled
reboot
# after it comes back:
systemctl status smarthealth-bot
journalctl -u smarthealth-bot -n 30
```

---

## Everyday operations

```bash
# deploy a new build of the worker
scp agent_smarthealth_claude_v2.py root@<server>:/opt/livekit/bot/
systemctl restart smarthealth-bot

# logs
journalctl -u smarthealth-bot -f                 # live
journalctl -u smarthealth-bot --since "1 hour ago"
journalctl -u smarthealth-bot -p warning          # warnings+errors only
journalctl -u smarthealth-bot -f | grep -vE "httpcore|httpx"   # drop HTTP noise

# control
systemctl stop|start|restart smarthealth-bot
systemctl disable smarthealth-bot        # stop starting at boot
```

### Restart drains calls — it is not instant

`restart` sends SIGINT; the worker stops taking new jobs and lets in-flight calls finish.
`TimeoutStopSec=480` (> `MAX_CALL_SECONDS=420`) gives a live call room to end naturally.
So a restart during a call can take minutes — that is correct, not a hang. Killing it
early drops the caller **and** loses the disposition webhook for that lead.

Deploy during quiet hours where you can, and remember AU calling windows are
9am–8pm weekday / 9am–5pm weekend in the prospect's local time.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `status` = failed, exits immediately | `journalctl -u smarthealth-bot -n 50`. Usually a missing key in `.env` or a venv path typo in `ExecStart`. |
| Starts but no `registered worker` | Cannot reach `LIVEKIT_URL`, or key/secret mismatch with `livekit.yaml`. |
| Registers but calls never arrive | Dispatch rule `agentName` must equal **`smarthealth-bot`**. Check `lk sip dispatch list`. |
| `.env` values ignored | `WorkingDirectory` must be `/opt/livekit/bot` — `load_dotenv()` resolves `.env` relative to CWD. |
| Two builds answering alternately | A second worker with the same `agent_name` is running. See §0. |
| "inference slower than realtime" | CPU-starved. `OMP_NUM_THREADS=1` is already set; next lever is lowering `num_idle_processes` (currently 3) — on a 4 vCPU box, 1–2 is more realistic. |

## Notes

- **Runs as root.** Fine to start with; a dedicated unprivileged user owning
  `/opt/livekit/bot` is the better end state (add `User=`/`Group=` to the unit).
- **Secrets:** `.env` holds API keys — `chmod 600 /opt/livekit/bot/.env`.
- **Log volume:** the agent logs at DEBUG. journald will rotate, but if the disk fills
  quickly, cap it with `SystemMaxUse=` in `/etc/systemd/journald.conf`, or raise the log
  level in the worker.
