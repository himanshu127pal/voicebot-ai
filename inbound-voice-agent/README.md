# Inbound Voice Agent

AI voice agent that answers customers **calling back** on our outbound campaign DIDs,
replacing the IVR ("press 1…, press 2…").

In one short conversation the agent says who we are and why we called, then — depending
on the caller — books a callback, transfers to a person, sends the details on WhatsApp /
email instead of calling, or opts the number out ("nobody will call you again about this
unless you ask"). Every call is reported to the backend with the outcome, opt-in/opt-out
flags and the full transcript.

Built on the Naina agent (LiveKit Agents, STT → LLM → TTS cascade). LLM: Gemini or Claude
(`LLM_PROVIDER`). Voice per campaign: **Sarvam** (Hindi/English auto-detect) or **Deepgram**
(AU/UK/US English).

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip
cp .env.example .env                        # fill in keys
.venv/bin/python -m inbound_agent download-files
.venv/bin/python -m inbound_agent dev
```

Run the tests: `.venv/bin/python -m pytest`

## Local testing (no server, no phone)

Console mode runs the agent against your own mic/speaker — no LiveKit server, SIP or DID
needed. Only the model keys are required (LLM + the campaign's voice stack).

```bash
# .env: LLM_PROVIDER + its key, the voice key, and CONSOLE_CAMPAIGN=<campaign key>
.venv/bin/python -m inbound_agent console           # talk through the mic
.venv/bin/python -m inbound_agent console --text    # type instead of speaking
.venv/bin/python -m inbound_agent console --list-devices
```

In `--text` mode set `NUDGE_AFTER_SECONDS=60` so the silence check-in doesn't fire while
you're typing. Hang-up and transfer are simulated in console mode; outcomes are still written to
`TRANSCRIPT_LOG_PATH`. `cloud turn detector failed (401)` / `Adaptive Interruption … 401`
warnings are expected on a self-hosted setup.

## Project layout

```
config/campaigns/      Campaign scripts — one YAML per campaign (DIDs, company, purpose, facts)
src/inbound_agent/
  __main__.py          CLI entry: python -m inbound_agent {dev|start|download-files}
  worker.py            LiveKit entrypoint: one run per call
  agent.py             InboundAgent (prompt + tools + number-to-words TTS filter)
  tools.py             share_details, opt_out, book_callback, mark_interested,
                       transfer_to_human, set_disposition, end_call
  prompts.py           System prompt + greeting, built from the campaign
  campaigns.py         YAML loading, validation, DID → campaign lookup
  call_state.py        Per-call state and outcome rules
  disposition.py       End-of-call report → webhook + JSONL log
  call_guards.py       Silence nudge + max call duration
  pipeline.py          STT / LLM / TTS / VAD construction
  telephony.py         Hang-up and SIP transfer
  numbers.py           Digits → spoken English words
  settings.py          Environment configuration
deploy/
  livekit/             SIP inbound trunk + dispatch rule
  freeswitch/          Dialplan: DID → agent (IVR as fallback)
  systemd/             Service unit
docs/                  ARCHITECTURE · DEPLOYMENT · SCRIPT_GUIDE
tests/                 Unit tests (no LiveKit/network needed)
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — call flow, modules, outcome rules
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — server setup, LiveKit, FreeSWITCH, systemd
- [docs/SCRIPT_GUIDE.md](docs/SCRIPT_GUIDE.md) — writing a campaign script (for the script team)

## Status

- [ ] Final script per campaign → `config/campaigns/` (placeholders are marked `REPLACE`)
- [ ] Real DIDs → campaign files + `deploy/freeswitch/dialplan-inbound.xml`
- [ ] Backend webhook that acts on `do_not_call` and `whatsapp_opt_in`
- [ ] Human transfer target, once the hand-off policy is decided
