# Deployment

Target: the existing LiveKit host (livekit-server + livekit-sip + Redis already running,
see `../README.md` and `../docker-compose.yml`). FreeSWITCH is on its own host.

## 1. Install the agent

```bash
sudo mkdir -p /opt/livekit/inbound-voice-agent
sudo cp -r . /opt/livekit/inbound-voice-agent/
cd /opt/livekit/inbound-voice-agent

python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m inbound_agent download-files     # Silero VAD model

cp .env.example .env    # fill in: LIVEKIT_*, GOOGLE_API_KEY, SARVAM/DEEPGRAM keys,
                        # DISPOSITION_WEBHOOK_URL, TRANSCRIPT_LOG_PATH
```

The project has its own virtualenv, so it cannot conflict with the other bots' packages.

## 2. LiveKit trunk and dispatch rule

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=… LIVEKIT_API_SECRET=…

# edit deploy/livekit/trunk-inbound.json: allowed_addresses = FreeSWITCH IP
lk sip inbound create deploy/livekit/trunk-inbound.json      # prints ST_… id

# put that id into deploy/livekit/dispatch-inbound.json → trunk_ids
lk sip dispatch create deploy/livekit/dispatch-inbound.json
```

- `numbers: ["inbound-ai"]` must equal the user-part FreeSWITCH bridges to. It keeps this
  trunk distinct from the outbound-campaign trunks on the same host.
- `agent_name` in the dispatch rule must equal `INBOUND_AGENT_NAME` (default `inbound-bot`).
- `lk` verbs differ between CLI versions (`inbound` vs `inbound-trunk`) — check `lk sip --help`.

## 3. FreeSWITCH

1. The existing `livekit` gateway (`../freeswitch/livekit-gateway.xml`) is reused.
2. Add `deploy/freeswitch/dialplan-inbound.xml` to the context where the carrier delivers
   DIDs, **above** the IVR extension. Fill in:
   - the DID regex (start with 1–2 DIDs; the rest stay on the IVR),
   - the IVR extension used as the fallback.
3. Make sure `/recordings/inbound/` exists and is writable.
4. `fs_cli -x reloadxml`

## 4. Run as a service

```bash
sudo cp deploy/systemd/inbound-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inbound-agent
journalctl -u inbound-agent -f
```

On start you should see `loaded N campaign(s)` and no `skipping campaign file` errors.
`registered worker` with agent `inbound-bot` means LiveKit can dispatch calls to it.

## 5. Smoke test

1. Call one routed DID from a mobile.
2. Expect the greeting with the right company within ~1 s of answer.
3. Say "please don't call me again" → agent apologises, confirms opt-out, hangs up.
4. Check the log line `call ended → OPT_OUT (DNC)` and the new line in `TRANSCRIPT_LOG_PATH`.
5. Repeat with "send it on WhatsApp" and "I'm interested".

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Call connects, silence | RTP 10000-20000/udp not open FreeSWITCH → LiveKit |
| Call never reaches the agent | `agent_name` mismatch between dispatch rule and `.env`; trunk `numbers` ≠ bridged user-part |
| Wrong company in greeting | DID missing from campaign `dids`; check the `DID … matches no campaign` warning |
| Bot never speaks (Deepgram) | `tts_voice` not an Aura id, or `DEEPGRAM_API_KEY` missing |
| `address already in use :8088` | Another worker on the port — change `WORKER_HTTP_PORT` |
| Webhook never arrives | `DISPOSITION_WEBHOOK_URL` unset, or backend rejects — see `disposition POST` log |
| `cloud turn detector failed (401)` | Benign on self-hosted; the local detector is used |

## Updating

- **Script change:** edit the YAML in `config/campaigns/` — applies on the next call.
- **Code change:** `git pull && sudo systemctl restart inbound-agent`. The service drains
  in-flight calls first (`TimeoutStopSec=360`).
