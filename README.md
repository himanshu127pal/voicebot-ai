# LiveKit migration — starter kit

> **⚠️ Status update — this project moved to PURE LiveKit Agents.**
> Pipecat was an early-stage idea and was **dropped**. The production agent is
> **`naina_agent_bilingual.py`** (a LiveKit Agents worker: Sarvam STT → Gemini LLM →
> Sarvam TTS). Wherever this file (and `DEPLOY.md`) says "Pipecat agent," read it as
> **the Naina LiveKit agent** — the SIP/media bring-up below is unchanged and still
> correct. For the agent itself, see **`DEVELOPER_GUIDE.md`** and **`DEPLOY_NEW_SERVER.md`**.

Replaces the hand-rolled aiortc SIP softphone (`sip/sip_ws_client.py`,
`sip/sip_transport.py`, `aiortc_debug_patch.py`) with a self-hosted LiveKit
media stack.

## Topology

```
FreeSWITCH (separate host, B2BUA, does call recording)
   │  SIP trunk: INVITE + X-headers, PCMA/RTP
   ▼
livekit-sip ──(Redis + WebRTC, localhost)──► livekit-server (SFU)
                                                   ▲
                              Pipecat agent(s) ────┘  (LiveKitTransport, same host)
```

## Bring-up order (prove media BEFORE touching the dialer)

1. **Generate a key/secret** and paste the SAME pair into `livekit.yaml`,
   `sip.yaml`, and your agent env:
   ```
   openssl rand -hex 32          # use as APISECRET
   ```
2. **Start the stack** (on the Pipecat host):
   ```
   docker compose up -d
   docker compose logs -f livekit-sip     # watch for "sip signaling started"
   ```
3. **Create trunk + dispatch rule** (fill in FreeSWITCH IP in `trunk.json` first):
   ```
   lk sip inbound create  trunk.json
   lk sip dispatch create dispatch.json
   ```
   ⚠ `lk` CLI verbs have changed across versions (e.g. `inbound` vs
   `inbound-trunk`). Run `lk sip --help` and confirm; the JSON *payload* fields
   are the stable part. Point the CLI at your server:
   `export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...`
4. **FreeSWITCH side** (separate host):
   - drop `freeswitch/livekit-gateway.xml` into `conf/sip_profiles/external/`,
     set the LiveKit host IP, then `sofia profile external rescan`
   - add `freeswitch/dialplan-to-bot.xml` to your dialplan
   - open **UDP 5060 + UDP 10000-20000** from FreeSWITCH → LiveKit host
5. **Smoke test with the stock agent** first:
   https://github.com/livekit-examples/livekit-sip-agent-example
   Bridge a test call to ext 1000, confirm two-way audio AND that
   `X-Call-Id` shows up as attribute `dialer.call_id` on the participant.
6. **Only then** swap the stock agent for your Pipecat `bot()` behind
   `LiveKitTransport`.

## How the old integration maps over

| Old (aiortc) | New (LiveKit) |
|---|---|
| Bot REGISTERs as ext 1000 over WSS | FreeSWITCH dialplan bridges ext 1000 → LiveKit trunk |
| `preview:incoming` guesses which call | `X-headers` → participant attributes (`dialer.*`) |
| 1 process = 1 call (486 on #2) | 1 room = 1 agent worker; scale by adding workers |
| `POST Available` / `POST dispose` | unchanged — plain HTTP, keep `backend_client.py` |
| DTLS/ICE/marker-bit patches | deleted — LiveKit owns the media |

## Gotchas (the ones that actually cost a day)

- **Silent audio** = RTP range (10000-20000/udp) not open FreeSWITCH→LiveKit. #1 cause.
- **Call connects then nothing happens** = api_key/secret mismatch between
  `sip.yaml` and `livekit.yaml`, or Redis not shared.
- **Wrong SDP IP** (FreeSWITCH can't reach the media) = fix `use_external_ip` /
  `nat_1to1_ip` in `sip.yaml` for private-network setups.
- **Host networking is Linux-only** — fine for your server, won't run on Docker Desktop/mac the same way.
- Pin image versions before production; `:latest` drifts.

## In the Pipecat agent

Read dialer context off the SIP participant's attributes:
```python
call_id   = participant.attributes.get("dialer.call_id")
prospect  = participant.attributes.get("dialer.prospect_name")
campaign  = participant.attributes.get("dialer.campaign")
```
Feed those into your existing flow, and keep `disposition_map` +
`backend_client.post_dispose(...)` on hangup exactly as today.
