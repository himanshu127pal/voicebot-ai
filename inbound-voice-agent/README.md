# Inbound Voice Agent

AI voice agent that answers customers **calling back** on outbound campaign DIDs,
replacing a plain IVR.

In one short conversation the agent says who we are and why we called, then —
depending on the caller — books a callback, transfers to a person (if configured),
records WhatsApp / email consent, or opts the number out. Every call is logged with
the outcome, flags, cost and full transcript. The backend owns follow-up actions
(block list, sending messages, creating leads).

Built on LiveKit Agents (STT → LLM → TTS). LLM: Gemini or Claude (`LLM_PROVIDER`).
Voice per campaign: **Sarvam** (Hindi/English auto-detect) or **Deepgram**
(AU / UK / US English).

One worker serves **many campaigns**. The DID the customer dialled selects the
script — company name, greeting, facts, questions and voice.

---

## Architecture

### Call flow

```
Customer dials our DID
  → carrier → FreeSWITCH  (deploy/freeswitch/dialplan-inbound.xml)
        sets X-DID, X-Caller, X-Call-Id, X-Recording; records the call;
        falls back to the IVR if the AI leg fails
  → LiveKit SIP trunk "inbound-ai"  (deploy/livekit/trunk-inbound.json)
        X-headers → participant attributes inbound.*
  → room "inbound-…" → dispatched to agent "inbound-bot"
  → worker.entrypoint()
        campaign  = CampaignRegistry.resolve(DID)
        session   = STT → LLM → TTS  (stack from the campaign)
        greeting  = fixed line: company + agent name + why we called
        … conversation, tools …
  → hang-up → DispositionReporter → webhook + JSONL
```

### Campaign resolution (multiple scripts, one agent)

`CampaignRegistry.resolve(did, hint)`:

1. `X-Campaign` header, if FreeSWITCH sets one
2. exact DID match (digits only — `+44 20 7946 0000` = `442079460000`)
3. match on the last 9 digits (carriers send `+44…`, `44…` or `020…` for the same DID)
4. the campaign marked `default: true`
5. a built-in neutral fallback

Campaign YAML files are re-read on every call — script edits need **no restart**.
A file that fails validation is logged and skipped; the rest keep working.

### Process model

Each call runs `entrypoint()` in its own OS process, so call state cannot mix.
`prewarm()` loads Silero VAD into idle processes (`NUM_IDLE_PROCESSES`) so calls
start without a cold-start delay. Scale out by running more workers with the same
`INBOUND_AGENT_NAME`.

### Conversation policy

| Caller | Agent |
|---|---|
| "Who is this?" / doesn't remember | Who we are + why we called; ask if they want to hear more |
| Interested | Benefit → `mark_interested` → questions → callback or transfer |
| Busy | WhatsApp (then email if needed), or a callback |
| Not interested / no calls | Offer WhatsApp **once**, then email once → `share_details` or `opt_out` |
| Annoyed / "spam?" | Apologise, confirm legitimacy, `opt_out` immediately — no pitch |
| Wrong number | Apologise, `opt_out` |
| Wants a person | `transfer_to_human` (falls back to callback if URI not set) |

Hard rules (WhatsApp only on mobile, email only without WhatsApp, full name,
UK postcode check) are enforced **inside the tools**, not only in the prompt.
A tool refuses with `NOT SAVED — …` and tells the model what to ask next.

### Outcomes

One headline `outcome`, plus individual flags. Precedence:

1. `TRANSFERRED`
2. do-not-call → `WHATSAPP_OPT_IN` / `EMAIL_OPT_IN` if they chose a channel, else `OPT_OUT`
   (do-not-call outranks "interested")
3. `INTERESTED` → `CALLBACK` → `WHATSAPP_OPT_IN` → `EMAIL_OPT_IN`
4. `NOT_INTERESTED` / `WRONG_NUMBER` / `INFO_ONLY` (set by the LLM)
5. `NO_OUTCOME` (someone spoke) / `DROPPED` (nobody spoke)

### End-of-call report

Posted to `DISPOSITION_WEBHOOK_URL` (if set) and appended to `TRANSCRIPT_LOG_PATH`:

```json
{
  "call_id": "…", "agent": "inbound-bot", "direction": "inbound",
  "campaign": "appliance_cover_uk", "company": "…", "did": "…",
  "caller_number": "…", "caller_line_type": "mobile",
  "outcome": "WHATSAPP_OPT_IN", "disposition_code": "WhatsApp Opt-in",
  "do_not_call": true, "whatsapp_opt_in": true, "whatsapp_number": "…",
  "email_opt_in": false, "email": "",
  "interested": false, "callback_requested": false, "callback_when": "",
  "transferred": false, "answers": {}, "notes": "",
  "postcode": "", "postcode_verified": false,
  "lead_found": false, "address_on_file": false,
  "recording": "/recordings/inbound/….wav", "room": "inbound-…",
  "started_at": "…", "ended_at": "…", "duration_seconds": 74.2,
  "usage": {}, "cost": {},
  "transcript": [{"role": "assistant", "text": "…", "at": "…"}]
}
```

---

## Project setup

### Requirements

- Python **3.10+**
- API keys for the LLM (`GOOGLE_API_KEY` or `ANTHROPIC_API_KEY`) and the voice
  stack used by your campaigns (`DEEPGRAM_API_KEY` and/or `SARVAM_API_KEY`)
- For real calls: LiveKit (server + SIP) and FreeSWITCH — see [Deployment](#deployment)

### Install

```bash
python -m venv .venv

# Linux / macOS
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m inbound_agent download-files   # Silero VAD, once per machine

# Windows
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m inbound_agent download-files
```

### Environment

```bash
cp .env.example .env
```

Fill in at least:

| Variable | Purpose |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | LiveKit connection (real calls) |
| `LLM_PROVIDER` | `google` or `anthropic` |
| `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` | LLM key for the chosen provider |
| `DEEPGRAM_API_KEY` / `SARVAM_API_KEY` | Voice stacks used in campaign YAML |
| `CONSOLE_CAMPAIGN` | Campaign `key` for local console tests |
| `CONSOLE_CALLER` | Fake caller ID for console (mobile/landline tests) |
| `LEADS_FILE` | Optional YAML of name/address by phone (confirm on call-back) |
| `HUMAN_TRANSFER_URI` | Global SIP/tel transfer target; blank = book a callback |
| `DISPOSITION_WEBHOOK_URL` | Backend that acts on opt-outs / WhatsApp / callbacks |
| `TRANSCRIPT_LOG_PATH` | Local JSONL call log (default `./data/calls.jsonl`) |
| `INBOUND_AGENT_NAME` | Must match LiveKit dispatch `agent_name` (default `inbound-bot`) |
| `WORKER_HTTP_PORT` | Health port (default `8088`) |
| `MAX_CALL_SECONDS` | Cap per call (default `600`; systemd stop timeout must be higher) |

### Run locally (against LiveKit)

```bash
.venv/bin/python -m inbound_agent download-files
.venv/bin/python -m inbound_agent dev      # verbose development
.venv/bin/python -m inbound_agent start    # production-style
```

### Tests

```bash
.venv/bin/python -m pytest
```

Unit tests do not need LiveKit or a network (postcode tests mock the API).

### Call cost report

```bash
.venv/bin/python -m inbound_agent.costs
```

Reads `TRANSCRIPT_LOG_PATH` and prints per-call / per-minute / per-campaign totals.

---

## Local testing (no server, no phone)

Console mode uses your mic/speaker — no LiveKit, SIP or DID required. Only model keys
and `CONSOLE_CAMPAIGN` are needed.

```bash
.venv/bin/python -m inbound_agent console              # talk
.venv/bin/python -m inbound_agent console --text       # type
.venv/bin/python -m inbound_agent console --list-devices
```

- Set `CONSOLE_CALLER` to test line type, e.g. `+442079460123` (UK landline) or
  `+447400123456` (UK mobile). Blank = withheld.
- In `--text` mode set `NUDGE_AFTER_SECONDS=60` so silence check-ins do not fire while typing.
- Hang-up and transfer are simulated; outcomes still go to `TRANSCRIPT_LOG_PATH`.
- `cloud turn detector failed (401)` / Adaptive Interruption `401` warnings are expected
  on a self-hosted LiveKit setup.

---

## Campaigns (scripts)

One YAML file per campaign under `config/campaigns/`. Copy `_template.yaml` and fill
every `REPLACE`. Details: [docs/SCRIPT_GUIDE.md](docs/SCRIPT_GUIDE.md).

| Field | Meaning |
|---|---|
| `key` | Unique id (e.g. `appliance_cover_uk`) |
| `default` | Exactly one campaign should be `true` (unknown DIDs) |
| `dids` | Numbers customers ring back on |
| `company`, `agent_name`, `purpose`, `benefit` | Spoken identity |
| `greeting` | Optional fixed opening (`{time_greeting}` supported) |
| `facts`, `faq` | Only facts the agent may state |
| `questions` | Details to collect once interested (`field` + `ask`) |
| `extra_instructions` | Script order, eligibility, care rules |
| `channels` | `whatsapp` / `email` |
| `country` | e.g. `GB` — local phone rules; GB + `address` question enables UK postcode check |
| `human_transfer_uri` | Optional SIP/tel override |
| `voice` | `sarvam` or `deepgram` (+ language / voice / speed) |

Current examples:

- `appliance_cover_uk.yaml` — UK washing-machine cover (full script)
- `go_care_health_au.yaml` — AU Go Care private health insurance (`+61 420 199 870`)
- `example_roof_uk.yaml` — short UK roof example
- `naina_default.yaml` — default / unknown DID (placeholders)

---

## Deployment

Target: existing LiveKit host (livekit-server + livekit-sip + Redis). FreeSWITCH is
typically on its own host. Full steps: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### 1. Install on the media host

```bash
sudo mkdir -p /opt/livekit/inbound-voice-agent
sudo cp -r . /opt/livekit/inbound-voice-agent/
cd /opt/livekit/inbound-voice-agent

python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m inbound_agent download-files

cp .env.example .env   # fill LIVEKIT_*, LLM + voice keys, webhook, log path
```

### 2. LiveKit SIP trunk + dispatch

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=… LIVEKIT_API_SECRET=…

# edit deploy/livekit/trunk-inbound.json → allowed_addresses = FreeSWITCH IP
lk sip inbound create deploy/livekit/trunk-inbound.json      # prints ST_…

# put that id into deploy/livekit/dispatch-inbound.json → trunk_ids
lk sip dispatch create deploy/livekit/dispatch-inbound.json
```

- Trunk `numbers: ["inbound-ai"]` must match the user-part FreeSWITCH bridges to.
- Dispatch `agent_name` must equal `INBOUND_AGENT_NAME` (default `inbound-bot`).

### 3. FreeSWITCH dialplan

1. Reuse the existing LiveKit gateway.
2. Add `deploy/freeswitch/dialplan-inbound.xml` **above** the IVR extension.
3. Fill the DID regex and the IVR fallback extension.
4. Ensure `/recordings/inbound/` exists and is writable.
5. `fs_cli -x reloadxml`

### 4. systemd

```bash
sudo cp deploy/systemd/inbound-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inbound-agent
journalctl -u inbound-agent -f
```

Expect `loaded N campaign(s)` and `registered worker` for `inbound-bot`.
`TimeoutStopSec=660` is above `MAX_CALL_SECONDS=600` so in-flight calls drain cleanly.

### 5. Smoke test

1. Call a routed DID from a mobile.
2. Greeting with the right company within ~1 s.
3. "Please don't call me again" → opt-out → hang-up → `OPT_OUT (DNC)` in the log.
4. Repeat with WhatsApp opt-in and "I'm interested".

### Updating

| Change | Action |
|---|---|
| Script only | Edit YAML in `config/campaigns/` — live on the next call |
| Code | `git pull && sudo systemctl restart inbound-agent` |
| Unit file | `sudo systemctl daemon-reload` then restart |

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| Call connects, silence | RTP 10000–20000/udp not open FreeSWITCH → LiveKit |
| Call never reaches the agent | `agent_name` mismatch; trunk `numbers` ≠ bridged user-part |
| Wrong company in greeting | DID missing from campaign `dids` |
| Bot never speaks (Deepgram) | Bad `tts_voice` or missing `DEEPGRAM_API_KEY` |
| `address already in use :8088` | Change `WORKER_HTTP_PORT` |
| Webhook never arrives | `DISPOSITION_WEBHOOK_URL` unset, or backend rejects |
| `cloud turn detector failed (401)` | Benign on self-hosted; local detector is used |

---

## Technical details

### Stack

| Layer | Options |
|---|---|
| Orchestration | LiveKit Agents worker (`inbound-bot`) |
| STT / TTS | Deepgram (English) or Sarvam (Hindi/English) — per campaign |
| LLM | Google Gemini or Anthropic Claude — global via `LLM_PROVIDER` |
| VAD | Silero (prewarmed) |
| Telephony | FreeSWITCH → LiveKit SIP; hang-up deletes the room; transfer via SIP REFER |

### Modules (`src/inbound_agent/`)

| Module | Responsibility | LiveKit? |
|---|---|---|
| `settings.py` | Environment → typed `Settings` | no |
| `campaigns.py` | YAML load, validation, DID → campaign | no |
| `call_state.py` | Per-call state, outcome precedence | no |
| `prompts.py` | System prompt + greeting from campaign | no |
| `disposition.py` | Report payload, webhook, JSONL | no |
| `leads.py` | Caller ID → name/address on file | no |
| `phones.py` | Valid? country? mobile / landline? | no |
| `postcodes.py` | Spoken UK postcode → postcodes.io | no |
| `validation.py` | Full name, spoken email | no |
| `numbers.py` | Digits → spoken English words | no |
| `redaction.py` | Strip card-length numbers before the LLM | no |
| `costs.py` | Per-call cost from usage | no |
| `pipeline.py` | STT / LLM / TTS / VAD | yes |
| `telephony.py` | Hang-up, SIP transfer | yes |
| `tools.py` | Function tools (state in session userdata) | yes |
| `agent.py` | `InboundAgent` | yes |
| `call_guards.py` | Silence nudge, max duration | yes |
| `worker.py` | Entrypoint, prewarm, worker options | yes |

Business logic stays out of LiveKit-facing modules so it is unit-tested without a server.

### Tools the LLM can call

| Tool | When |
|---|---|
| `share_details` | WhatsApp or email consent (mobile-only for WhatsApp) |
| `opt_out` | Do-not-call |
| `book_callback` | Caller names a day/time |
| `mark_interested` | Caller wants to proceed |
| `save_answer` | One answer to a campaign question |
| `check_postcode` / `save_address` | UK address flow (`country: GB` + `address` question) |
| `confirm_address` | Address on file is still correct |
| `transfer_to_human` | Cold transfer if URI configured |
| `set_disposition` | `NOT_INTERESTED` / `WRONG_NUMBER` / `INFO_ONLY` |
| `end_call` | Hang up after goodbye |

### Data checks

The model talks; **code** checks. See [docs/DATA_CHECKS.md](docs/DATA_CHECKS.md).

| Check | How |
|---|---|
| Mobile vs landline | `phonenumbers` (offline) |
| UK postcode | postcodes.io (free) |
| Full name | First + surname rule |
| Email | Spoken → normalised address |
| Card / bank numbers | Redacted before they reach the LLM |

AU / US postcode lookup is not built yet (UK only today).

### What the agent records vs what the backend must do

| On the call (agent) | After the call (backend) |
|---|---|
| Outcome, answers, transcript, cost | Act on `do_not_call` (block list) |
| WhatsApp / email opt-in + contact | Send the message |
| Callback time in the caller's words | Dial / schedule the advisor |
| Transfer attempt | Live supervisor destination must be configured |

Without `DISPOSITION_WEBHOOK_URL`, everything is still written to the local JSONL log only.

---

## Project layout

```
config/campaigns/      Campaign scripts — one YAML per campaign
config/test_leads.yaml Optional fake leads for console tests
src/inbound_agent/     Agent code (see modules table above)
deploy/
  livekit/             SIP inbound trunk + dispatch rule
  freeswitch/          Dialplan: DID → agent (IVR as fallback)
  systemd/             Service unit
docs/                  Architecture, deployment, script guide, data checks, changelog
tests/                 Unit tests
qa/                    Sample transcripts / TTS checks
data/                  Local call log (gitignored in practice)
```

---

## Further documentation

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Call flow, modules, outcomes (detail) |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Server, LiveKit, FreeSWITCH, systemd |
| [docs/SCRIPT_GUIDE.md](docs/SCRIPT_GUIDE.md) | Writing a campaign YAML |
| [docs/DATA_CHECKS.md](docs/DATA_CHECKS.md) | Phone, postcode, name, email checks |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | What changed, when, and why |

---

## Status

- [ ] Final script per campaign → `config/campaigns/` (placeholders marked `REPLACE`)
- [ ] Real DIDs → campaign files + `deploy/freeswitch/dialplan-inbound.xml`
- [ ] Backend webhook that acts on `do_not_call` and `whatsapp_opt_in`
- [ ] Human transfer target, once the hand-off policy is decided
- [ ] AU / US postcode verification (UK only today)
