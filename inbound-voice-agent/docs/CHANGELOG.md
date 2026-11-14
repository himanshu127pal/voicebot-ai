# Changelog

What changed in the inbound agent, when, and why. Newest first.

## 2026-11-12 — Per-call cost

- **Every call log line now says what the call cost.** `usage` = LiveKit's per-model
  counts (LLM tokens incl. cached, TTS characters, STT audio seconds); `cost` = those
  priced at pay-as-you-go rates (`costs.py`) in USD, split STT / TTS / LLM / telephony,
  plus the ₹ total. Also sent to the webhook, and the `call ended` log line shows it.
- `python -m inbound_agent.costs` totals the log: per call, per minute, per campaign.
  Calls logged before this change are skipped.
- New settings: `USD_INR_RATE` (Sarvam bills in rupees), `TELEPHONY_USD_PER_MINUTE`
  (carrier rate from its invoice; 0 = AI cost only). Server and DID rental are fixed
  monthly costs and are not in the per-call figure.
- `gemini-2.5-flash` is deprecated; from 2026-12-01 Google routes it to
  `gemini-3.5-flash-lite` at the same price. Test the bot on it before then.

## 2026-11-12 — Client feedback round 2 (inbound call-backs)

- **Name and address confirmed, not asked.** We rang the caller first, so their record
  exists. The caller ID is looked up in `LEADS_FILE` (`leads.py`). With a record, Stacy
  asks *"Am I speaking with Daniel Whitmore?"*, then reads the address back and calls
  `confirm_address` on a yes. No postcode is asked unless they've moved. Nothing is read
  out before the name is confirmed. No record → asks as before.
  `config/test_leads.yaml` holds fake customers for console tests. Report: `lead_found`,
  `address_on_file`.
- **No "is this a convenient time?" on a call-back.** The caller rang us. The
  `appliance_cover_uk` greeting and the default greeting now end with *"Would you like
  to hear a little more about it?"*.
- **Stacy says what kind of line it is.** *"I can see you're calling from a landline, so
  I can't send WhatsApp messages to it — do you have a mobile…?"* (and *"…from a mobile
  ending in 3456"*). Before, she knew but only asked for a mobile.
- **WhatsApp first, then email, nothing else.** The "we'll post the details" fallback is
  gone.

## 2026-11-10 (after the first console test)

Test call: 9 minutes, `data/calls.jsonl` (13:39–13:48 IST).

- **Number padding bug fixed.** The caller said 9 digits ("0 7 9 4 6 0 1 2 3"), the
  model saved `07946 012300`. Now a number is saved only if it's valid and its digits
  were actually said by the caller (`matches_what_was_said` in `phones.py`); otherwise
  the agent is told what was heard and asks for the rest.
- **"Yes, it's the best number" is enough.** The agent asked for the full number after a
  yes. Script step 5 now says any yes = save "calling number" and move on, and
  `save_answer` understands more ways of saying yes.
- **WhatsApp "same number" / "I already told you"** now uses the contact number
  confirmed earlier (or the caller ID for "the one I'm calling from"), without asking
  again.
- **Max call length 5 → 10 minutes** (`MAX_CALL_SECONDS=600`; systemd
  `TimeoutStopSec=660`). The test call would have been cut at 5 minutes. A smooth call
  through the full script takes about 5 minutes, a typical one 6–7, a messy one 8–9.
- Glitch seen in the test (agent stopping mid-sentence, e.g. 13:46:39): the mic picked
  up laptop-speaker echo or noise as an interruption. Use headphones for console tests;
  real phone calls have echo cancellation. No code change.

## 2026-11-10 (later) — Simplifications after review

- **Street is not confirmed any more.** Only the postcode must be real (postcodes.io).
  The first line is saved as said, in one step: `save_address(first_line)`. The
  two-step read-back (`caller_confirmed`) and the `address_confirmed` report field are
  gone.
- **Channel Islands and Isle of Man postcodes are accepted** like the UK (`UK_COUNTRIES`
  in `postcodes.py`).
- US numbers and AU/US postcodes are out of scope for now; there's no campaign there.
- SIP test numbers for the mobile/landline check: `DATA_CHECKS.md` → "Testing over SIP".
- New `CONSOLE_CALLER` setting: a pretend caller ID for local console tests (console mode
  has none). Ignored on real calls.

## 2026-11-10 — UK appliance cover: client feedback round 1

Campaign: `config/campaigns/appliance_cover_uk.yaml` (Stacy).
Baseline: commit `c513107` (2026-11-06), the version last deployed.

### Client feedback

> It's talking faster. Full name: even only "Robert" is being okayed — we must insist on
> full name. Then we have to get WhatsApp opt-in, and keep email as the last option in
> case the client doesn't agree to WhatsApp — separate both, don't ask at the same time,
> and if we get WhatsApp opt-in don't ask for email. The caller ID / DID I used is a
> fixed-line DID — put intelligence in to check if it's fixed line or mobile; we can't
> send WhatsApp to a fixed line or landline. Then address and postcode need
> confirmation; it must have a knowledge base of areas by postcode for UK, AU, USA etc.

### What was done

| Feedback | Root cause found | Fix | Enforced in code? |
|---|---|---|---|
| Talks too fast | No speed set on the Deepgram voice | `voice.tts_speed: 0.9` in the YAML, plus a PACE rule (short sentences, one idea per turn) | Speed: yes. Pace: prompt |
| "Robert" accepted as full name | Script step 1 asked "May I take your name?" (not *full* name); `save_answer` saved anything | Script asks for first name and surname; `save_answer("full_name")` refuses a single name and asks for the surname (gives up after 2 tries) | Yes |
| WhatsApp first, email only as fallback, never both | Step 6 order was right, but the "think about it" rule and the prompt's busy / not-interested lines offered "WhatsApp or email" in one question | All offers are WhatsApp first; email in a new turn only after a no. `share_details("email")` is refused once WhatsApp is agreed | Yes |
| WhatsApp sent to a fixed-line DID | `share_details` fell back to the caller ID without checking its type; mobile vs landline was left to the model | Caller ID is checked when the call starts (`phonenumbers`); the prompt says if it's a landline; `share_details("whatsapp")` refuses any non-mobile. The "mobile or landline?" question was removed: the type is worked out and saved as `answers.number_type` | Yes |
| Address and postcode need confirmation, with area knowledge | Postcode and first line were asked in one question, with no lookup | Postcode is asked first and checked on postcodes.io, then read back with its area ("…in Westminster"). Then the first line; the full address is read back and only saved after a yes | Yes |
| Area knowledge for AU / USA | — | Not built yet: this campaign is UK only. Free data sources are listed in `DATA_CHECKS.md` §3 | — |

Also:

- Email is now cleaned from speech (`"john dot smith at g mail dot com"`) and read back.
  Previously anything with an `@` was accepted.
- The report sent to the dialer has new fields: `caller_line_type`, `postcode`,
  `postcode_verified` and `address_confirmed`.

### Files

| File | Change |
|---|---|
| `src/inbound_agent/phones.py` | **New.** Number → valid / country / mobile or landline |
| `src/inbound_agent/postcodes.py` | **New.** Spoken postcode → clean postcode → postcodes.io |
| `src/inbound_agent/validation.py` | **New.** Full-name and email checks |
| `src/inbound_agent/tools.py` | WhatsApp/email rules, full-name and contact-number checks; new `check_postcode` and `save_address` tools |
| `src/inbound_agent/prompts.py` | Caller line type in the prompt; WhatsApp first, email as fallback |
| `src/inbound_agent/campaigns.py` | New YAML settings `country` and `voice.tts_speed` |
| `src/inbound_agent/pipeline.py` | Passes the speed to Deepgram |
| `src/inbound_agent/call_state.py`, `worker.py`, `agent.py`, `disposition.py`, `settings.py` | Wiring and report fields; `POSTCODE_API_URL` setting |
| `config/campaigns/appliance_cover_uk.yaml` | Script steps 1, 4, 5, 6 rewritten; `number_type` question removed; `country: GB`; `tts_speed: 0.9` |
| `pyproject.toml`, `.env.example` | `phonenumbers` dependency; `POSTCODE_API_URL` |
| `tests/test_phones.py`, `test_postcodes.py`, `test_validation.py`, `test_tools.py` | **New.** 136 tests in total (was 45) |
| `docs/DATA_CHECKS.md` | **New.** How each check works, the free services, paid upgrades |

### Deploying

1. `pip install -e .` on the server: this pulls in `phonenumbers`.
2. The server needs outbound HTTPS to `api.postcodes.io`. If it can't reach it, calls
   still work; the postcode just isn't verified.
3. Restart the agent service. (YAML-only edits go live without a restart; code changes
   don't.)

### Still to do / to decide

- [ ] **Test calls**: from a landline and from a mobile (numbers in `DATA_CHECKS.md` →
      "Testing over SIP"), with first name only, a wrong postcode, and refusing
      WhatsApp. Check the speed by ear (0.85–0.95).
- [x] ~~Channel Islands / Isle of Man~~: accepted (see the later entry above).
- [x] ~~Street-level address check~~: not needed; the postcode check is enough.
- [ ] AU / US postcode lookups, only if a campaign for those countries comes.
- [ ] Your own extra points for the bot (to be added here).
