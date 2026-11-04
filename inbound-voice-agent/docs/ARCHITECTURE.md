# Architecture

## Background

We place outbound campaign calls from DIDs in AU / UK / US / NZ. Customers who ring those
numbers back used to reach an IVR, got confused, flagged the number as spam, and the DID
was blocked. This agent replaces the IVR. Goals:

- the caller immediately hears **who we are and why we called** (we are legitimate);
- a caller who doesn't want calls gets an alternative (**WhatsApp / email**) or a clean
  **opt-out** — never pressure;
- interested callers become leads at no agent cost;
- every conversation is recorded for analysis.

## Call flow

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
        session   = STT → Gemini → TTS  (stack from the campaign)
        greeting  = fixed line: company + agent name + why we called
        … conversation, tools …
  → hang-up → DispositionReporter → webhook + JSONL
```

## Conversation policy

| Caller | Agent |
|---|---|
| "Who is this?" / doesn't remember | Who we are + why we called, ask if they want to hear more |
| Interested | One benefit → `mark_interested` → `book_callback` or `transfer_to_human` |
| Busy | WhatsApp/email details, or a callback |
| Not interested / no calls | Offer WhatsApp/email **once** → `share_details` or `opt_out` |
| Annoyed / "spam?" | Apologise, confirm legitimacy, `opt_out` immediately, no pitch |
| Wrong number | Apologise, `opt_out` |
| Wants a person | `transfer_to_human` (falls back to a callback if not configured) |

The prompt (`prompts.py`) repeats the fragile rules — language and "a no is a no" — at the
top and as the final block, because the model weights the start and end of its context
most. Keep the final block last when editing.

## Modules

| Module | Responsibility | LiveKit dependency |
|---|---|---|
| `settings.py` | Environment → typed `Settings` | no |
| `campaigns.py` | YAML scripts, validation, DID lookup | no |
| `call_state.py` | Per-call state, outcome precedence | no |
| `prompts.py` | System prompt + greeting | no |
| `disposition.py` | Report payload, webhook, JSONL | no |
| `numbers.py` | Digits → English words (streaming) | no |
| `pipeline.py` | STT / LLM / TTS / VAD | yes |
| `telephony.py` | Hang-up, SIP REFER transfer | yes |
| `tools.py` | LLM function tools (state via session userdata) | yes |
| `agent.py` | `InboundAgent` | yes |
| `call_guards.py` | Silence nudge, max duration | yes |
| `worker.py` | Entrypoint, prewarm, worker options | yes |

Business logic is kept out of the LiveKit-facing modules so it is unit-tested without a
server.

## Campaign resolution

`CampaignRegistry.resolve(did, hint)`:

1. `X-Campaign` header, if FreeSWITCH sets one;
2. exact DID match (digits only — `+44 20 7946 0000` = `442079460000`);
3. match on the last 9 digits (carriers send `+44…`, `44…` or `020…` for the same DID);
4. the campaign marked `default: true`;
5. a built-in neutral fallback.

Campaign files are re-read on every call, so script changes need no restart. A file that
fails validation is logged and skipped; the rest keep working.

## Outcomes

One headline `outcome` is reported, plus every individual flag. Precedence:

1. `TRANSFERRED`
2. do-not-call → `WHATSAPP_OPT_IN` / `EMAIL_OPT_IN` if they chose a channel, else `OPT_OUT`
   (a do-not-call request outranks "interested")
3. `INTERESTED` → `CALLBACK` → `WHATSAPP_OPT_IN` → `EMAIL_OPT_IN`
4. `NOT_INTERESTED` / `WRONG_NUMBER` / `INFO_ONLY` (set by the LLM)
5. `NO_OUTCOME` (a person spoke) / `DROPPED` (nobody spoke)

## Report payload

```json
{
  "call_id": "…", "agent": "inbound-bot", "direction": "inbound",
  "campaign": "roof_uk", "company": "…", "did": "…", "caller_number": "…",
  "outcome": "WHATSAPP_OPT_IN", "disposition_code": "WhatsApp Opt-in",
  "do_not_call": true, "whatsapp_opt_in": true, "whatsapp_number": "…",
  "email_opt_in": false, "email": "", "interested": false,
  "callback_requested": false, "callback_when": "", "transferred": false,
  "notes": "", "recording": "/recordings/inbound/…wav", "room": "inbound-…",
  "started_at": "…", "ended_at": "…", "duration_seconds": 74.2,
  "transcript": [{"role": "assistant", "text": "…", "at": "…"}]
}
```

The **backend owns the follow-up**: block list for `do_not_call`, WhatsApp/email sending,
callbacks and lead creation. Without a webhook these are only logged.

## Process model

Each call runs `entrypoint()` in its own OS process, so call state can never mix between
calls. `prewarm()` loads the Silero VAD into idle processes ahead of time
(`NUM_IDLE_PROCESSES`), so calls start without a cold-start delay. Scale out by running
more workers with the same `INBOUND_AGENT_NAME`.
