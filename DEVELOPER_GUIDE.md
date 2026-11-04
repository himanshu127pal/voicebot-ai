# Naina Voice Agent — Developer Guide

Everything a developer needs to understand, run, and modify the LiveKit voice bot
that answers inbound SIP calls, holds a sales conversation in Hindi/English, records
an outcome, and hangs up.

> **Audience:** developers extending or maintaining the agent.

### Which file? (two variants, same skeleton)

| File | Use it when |
|---|---|
| **`naina_agent_bilingual.py`** ⭐ | **The production agent.** Auto-detects the caller's language per turn and replies in it (Hindi ↔ English), and always speaks numbers in English words. This is the tested, deployed file — **this guide documents it.** |
| `naina_agent_sarvam.py` | Single-language variant (pinned `NAINA_LANG`). Same code **minus** the language auto-detect (§6.1) and the number→English TTS filter (§6.2). Use only if a campaign is strictly one language. |

They share the same structure, tools, disposition, silence/nudge, and deploy story —
so every section below applies to both **except §6.1 and §6.2**, which are
bilingual-only. Run **only one** at a time: they share `agent_name="naina-bot"`, so
LiveKit would load-balance between them.

---

## 1. What this is (30-second version)

- A **LiveKit Agents worker**. It registers with the LiveKit server as agent
  `naina-bot` and waits. When a call arrives, LiveKit runs `entrypoint()` for it.
- Pipeline is a **cascade**: **Sarvam STT → Gemini LLM → Sarvam TTS**, with
  **Silero VAD** + a turn detector deciding when the caller is done speaking.
- Telephony comes in over SIP: `FreeSWITCH → LiveKit SIP trunk → room → this agent`.
- Per-call context (name, phone, campaign, call_id) arrives as **SIP headers**
  mapped to LiveKit **participant attributes** (`dialer.*`).
- On call end it POSTs a **disposition** (the outcome) to your backend.
- **Bilingual:** STT auto-detects Hindi/English per turn and the prompt makes the LLM
  reply in the caller's *current* language (§6.1). **Numbers** are always spoken in
  English words via a TTS-side filter (§6.2).

```
Caller ─PSTN─> Dialer ─> FreeSWITCH ─SIP trunk─> LiveKit SIP ─> room "call-xxxx"
                                                                    │ dispatches
                                                                    ▼
                                              naina-bot worker → entrypoint()
                                              Sarvam STT → Gemini → Sarvam TTS
```

---

## 2. Call lifecycle (how one call flows through the code)

1. **`entrypoint(ctx)`** runs in its own OS subprocess (one per call — see §12).
2. `await ctx.connect()` — join the room.
3. `ctx.wait_for_participant()` — wait for the SIP caller, then read `dialer.*`
   attributes into the `prospect` dict.
4. Build `Naina(prospect)` — the agent, whose system prompt is assembled here.
5. Build `AgentSession(stt, llm, tts, vad)` — the pipeline.
6. Register `_on_shutdown` — fires when the call ends, builds + POSTs the disposition.
7. `session.start(...)` — pipeline goes live.
8. Wire the **silence-nudge** timers (state-change handlers).
9. **Dialer-AMD hook** — if a header flagged a machine, dispose VOICEMAIL + hang up.
   (Dormant unless you map the header — see §9.)
10. `session.generate_reply(...)` — the bot speaks the greeting.
11. Conversation runs. The LLM calls **tools** (`set_disposition`, `voicemail_detected`,
    `end_call`) as the call progresses.
12. Call ends (bot hangs up, caller hangs up, or dead-line timeout) → `_on_shutdown`
    → disposition POST.

---

## 3. Running it

```bash
# install  (num2words powers the English number filter, §6.2)
pip install "livekit-agents[google,silero]" livekit-plugins-sarvam python-dotenv num2words

# one-time: fetch the Silero VAD model
./.venv/bin/python naina_agent_bilingual.py download-files

# dev (verbose, single machine) / production
./.venv/bin/python naina_agent_bilingual.py dev
./.venv/bin/python naina_agent_bilingual.py start
```

> `num2words` is optional-at-runtime: if it's missing the agent still runs and logs a
> warning, but numbers are spoken as raw digits (which the voice mispronounces). Install it.

In production, run it under systemd (see `DEPLOY_NEW_SERVER.md`) so it restarts.

---

## 4. Configuration reference

### Environment variables (`.env` in the worker dir)

| Var | Purpose | Default / notes |
|---|---|---|
| `LIVEKIT_URL` | LiveKit server ws URL | `ws://127.0.0.1:7880` (co-located) |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Auth to LiveKit; also mints tokens | must match `livekit.yaml` |
| `SARVAM_API_KEY` | Sarvam STT + TTS | required |
| `GOOGLE_API_KEY` | Gemini LLM | required |
| `NAINA_LANG` | Language for STT + TTS | `hi-IN` (see list in code) |
| `MAX_CALL_SECONDS` | Hard cap on call length; force-hangup after this (billing safety) | `300` (5 min) |
| `DISPOSITION_WEBHOOK_URL` | Where the outcome is POSTed | if unset, payload is only logged |
| `DISPOSITION_WEBHOOK_TOKEN` | Optional bearer token for the POST | omit if not needed |
| `OMP_NUM_THREADS` | Caps Silero VAD threads on small boxes | set `1`–`2`; harmless to leave unset on big boxes |

### In-code knobs (edit `naina_agent_bilingual.py`)

| What | Where | Effect |
|---|---|---|
| STT model | `sarvam.STT(model="saaras:v3", ...)` | `saaras:v3` = fast streaming. Don't go back to `saarika:v2.5` (slow, deprecated). |
| STT language | `sarvam.STT(..., language="unknown")` | `"unknown"` = **auto-detect** (bilingual, §6.1). Pin (`language=LANG`) only for a single-language campaign. |
| LLM | `google.LLM(model="gemini-2.5-flash")` | Swap model or provider (`groq.LLM(...)`, `openai.LLM(...)`). Text LLM only — never a realtime model. |
| TTS codec | `sarvam.TTS(..., output_audio_codec="linear16")` | `linear16` = raw PCM, no decode lag. Don't use `mp3`. |
| TTS streaming latency | `sarvam.TTS(..., min_buffer_size=30, max_chunk_length=60)` | Lower than defaults (50/150) so the voice starts sooner and streams long replies fast — lets replies stay natural-length without a latency hit. See §5. |
| TTS voice | add `speaker="..."` to `sarvam.TTS(...)` | Bulbul voices (e.g. `shubh`, `anushka`) |
| Numbers → English words | `Naina.tts_node` + `NAINA`-level `_number_to_words` | Converts digit runs in the reply to spoken English (§6.2). Needs `num2words`. |
| Noise robustness (VAD) | `silero.VAD.load(activation_threshold=0.6, min_speech_duration=0.10)` in `prewarm()` | Main noise lever: quiet chatter falls below "speech" so it neither starts a turn nor interrupts the bot (§11.1) |
| Interruption behaviour | LiveKit default (immediate on VAD speech) — interruption kwargs omitted | Don't add `min_interruption_words` with Sarvam: a word gate lags until the caller stops (§11.1 ⚠️). Deprecated → `TurnHandlingOptions`. |
| Warm processes | `WorkerOptions(num_idle_processes=3)` | Higher = more simultaneous calls with zero cold-start; costs RAM |
| Silence nudge delay | `NUDGE_AFTER = 10.0` | Seconds of caller silence before the bot self-continues. Raised from 6→10 so natural thinking pauses don't trigger it. |
| Max nudges | `MAX_NUDGES = 3` | After this many unanswered nudges, hang up the dead line |
| Agent name | `WorkerOptions(agent_name="naina-bot")` | MUST equal `agentName` in the SIP dispatch rule |

---

## 5. The models (STT / LLM / TTS)

- **STT — `sarvam.STT(model="saaras:v3", mode="transcribe", language="unknown")`**
  Saaras v3: streaming, ~sub-second transcript. This was the single biggest latency
  fix (the old `saarika:v2.5` took 3–5s). Keep it.
  `language="unknown"` = **auto-detect** — essential for bilingual (§6.1). A *pinned*
  language transliterates spoken English into Devanagari, which fools the LLM into
  replying in the wrong language. (The single-language `naina_agent_sarvam.py` pins
  `language=LANG` on purpose.)
- **LLM — `google.LLM(model="gemini-2.5-flash")`**
  A **text** LLM. Reads `GOOGLE_API_KEY`. Swappable in one line to Groq/OpenAI/etc.
  > ⚠️ Do NOT use a *realtime/Live* model here (`google.beta.realtime.RealtimeModel`).
  > We evaluated speech-to-speech (Gemini Live) and it could not endpoint telephony
  > audio reliably (20s stalls / overlaps / `commit_audio` unsupported). The cascade
  > with a text LLM is the deliberate, tested choice.
  > **Reliability note:** Gemini's shared endpoint can return `503 "model overloaded"`
  > (a *capacity* error, unrelated to billing — a paid key doesn't exempt you). If that
  > bites in production, wrap the LLM in a `FallbackAdapter([gemini, groq])` so a 503
  > fails over instantly instead of stalling, and/or move to Vertex AI (`asia-south1`).
- **TTS — `sarvam.TTS(target_language_code=LANG, output_audio_codec="linear16",
  min_buffer_size=30, max_chunk_length=60)`**
  Bulbul v3, raw PCM out (no MP3 decode step). Add `speaker=` to change voice.
  Sarvam **streams** audio over a WebSocket; `min_buffer_size` (default 50) and
  `max_chunk_length` (default 150) are lowered to **30 / 60** so it starts speaking
  sooner and streams longer replies quickly. This is what lets replies stay natural
  (1–2 sentences) instead of clipped one-liners without paying a latency penalty.
  Lower = faster first audio but more WS chatter; don't go below ~30/50.
- **VAD — Silero**, preloaded in `prewarm()` (§12), used for speech detection.

Numbers in any reply are spoken in **English words** by a TTS-side filter — see §6.2.

---

## 6. The system prompt (the prompt guide)

The system prompt is assembled in `Naina.__init__` as one `instructions=` string. This
section is the **guide to how it's built and how to change it safely** — the structure
is deliberate and the ordering matters.

### 6.0 Prompt structure (and why it's ordered this way)

The prompt is laid out in labelled blocks, top to bottom:

```
You are Naina, a sales agent for GrowEasy …   ← persona + company/offer (one line)
## HARD RULES (never break):        ← 1 LANGUAGE  2 tone  3 NUMBERS  4 lead  5 on-topic  6 no-invent
## STYLE:                           ← one benefit per turn, handle side-questions
## AGENDA:                          ← greet → qualify → pitch → ask
## TOOLS:                           ← when to call set_disposition/voicemail/end_call
{name_clause}                       ← use the real name, or "don't invent one"
## ⚠️ MOST IMPORTANT — EVERY TURN:  ← LANGUAGE rule + NUMBERS rule, REPEATED, with
                                       few-shot examples   ← this is the LAST thing read
```

**Why the two most fragile rules (language, numbers) appear at BOTH the top and the
very bottom — the "sandwich":** an LLM re-reads the *entire* conversation every turn and
weights the **start (primacy)** and **end (recency)** of its context most. After a few
same-language turns, the history is a wall of that language and pulls the model to keep
using it. Making the language rule the **last thing it reads every turn** lets the rule
win that tug-of-war. This single change is what fixed the language switching. **Keep the
final block last** — if you append new sections after it, you break the recency effect.

Sections are kept **short** on purpose: research (and our calls) show instruction-
following degrades once the prompt+history gets long, so every block is trimmed to
essentials. Don't pad it.

### 6.1 What each rule prevents (don't delete blindly)

| Prompt clause | Why it's there |
|---|---|
| **RULE 1 — LANGUAGE:** reply in the caller's MOST RECENT message's language | Bilingual core. See §6.1-detail below. |
| **RULE 2 — tone:** "natural, warm, 1–2 short sentences; never clipped/pushy/monologue" | One-liners felt robotic & pushy; monologues made TTS slow. This is the balance. |
| **RULE 3 — NUMBERS:** "write numbers as plain digits" | The digits are converted to English words by the TTS filter (§6.2). Asking the LLM for words-in-Hindi failed; asking for digits is natural and reliable. |
| **RULE 4 — YOU lead:** end every turn with a point or question | Early bot answered then stalled in dead air; this makes it drive. |
| **RULE 5 — STAY ON TOPIC** (railguard): only discuss the company/offer; politely deflect off-topic (president's name, jokes, other companies) | Stops the bot being derailed into general Q&A. Legit product questions stay on-topic. See §6.3. |
| **RULE 6 — NEVER INVENT FACTS:** only state what's in the prompt; else "someone will follow up" | Anti-hallucination. Critical because there's **no knowledge base** yet (RAG deferred) — "our knowledge" = whatever is in the prompt. See §6.3. |
| **STYLE:** one benefit per turn; answer side-questions briefly then return; continue on "ok/haan" | Keeps momentum and re-anchors after interjections without waiting for acknowledgment. |
| **AGENDA:** greet → confirm person → check owner → pitch → ask | Gives "forward" a direction so the call progresses to an outcome. |
| **TOOLS** block | Tells the LLM *when* to fire each tool (§7); voicemail handling (§9); ordered hangup (say goodbye → `set_disposition` → `end_call`). |
| **name guard** ("don't invent one") | With no real name the LLM used to hallucinate one ("राम"). |
| **final ⚠️ block** (language + numbers repeated + few-shot) | The recency anchor (see §6.0). The few-shot examples show a concrete language switch and a digits-in-a-Hindi-sentence example. |

**Greeting** is a separate instruction: `session.generate_reply("Greet the caller…")`.
On the first turn there's no caller message yet, so it defaults to English — that's fine.

### 6.2 Editing the pitch safely — checklist

- 🔴 **Replace `GrowEasy` — it's a PLACEHOLDER.** The persona currently reads *"a sales
  agent for **GrowEasy** — an easy-to-use marketing platform…"*. `GrowEasy` and its
  one-line offer are dummy text. Put your real company name + one-line offer in **all
  three spots**: the persona line **and HARD RULES 5 and 6** (they name the company too).
  Search the file for `GrowEasy` and change every hit. Don't leave bracketed placeholders
  like `[Company]` in the prompt — the model will read them aloud.
- ✅ Replace the **AGENDA + STYLE** with your real script/pitch.
- ✅ Keep replies **short** (1–2 sentences) — it's both nicer and faster (TTS).
- ⚠️ **Keep the HARD RULES and the final ⚠️ block**, and keep that block **last**.
- ⚠️ Numbers: write **digits** in any example text you add (the filter voices them).

---

## 6.3 Railguard — staying on-topic & not hallucinating (RULES 5–6)

Two prompt rules keep the bot a *sales agent for your company* and nothing else:

**RULE 5 — Stay on topic.** The bot only discusses the company/offer. Off-topic asks
(general knowledge, news, politics, other companies, jokes, "who's the president?") get a
polite deflection + steer-back: *"I'm only able to help with GrowEasy, but…"*. **Important:
legitimate product questions and objections are ON-topic** — the rule explicitly says so,
so it won't wrongly refuse "do you integrate with Shopify?".

**RULE 6 — Never invent facts.** The bot states only what's in the prompt. If a caller
asks a real detail that isn't there (exact price, a feature, refund policy), it says
*"someone will follow up"* instead of making something up.
> ⚠️ **Why RULE 6 matters right now:** there is **no knowledge base** — RAG was deferred.
> So "answer from our knowledge" literally means "answer from the text in this prompt."
> Anything about your product the caller might ask should therefore be **written into the
> prompt** (a short FACTS block), or the bot can only defer. When you add RAG later, RULE 6
> becomes "answer from retrieved context, else defer."

**To make the railguard real for your business:**
1. Replace `GrowEasy` + the offer line (see §6.2).
2. Add a short **FACTS** block to the prompt with the things callers actually ask —
   price/plans, key features, who it's for, how to sign up. Keep it tight (bullet-ish).
   The more that's in there, the less RULE 6 has to deflect.
3. Optionally tune the deflection wording in RULE 5 to your brand voice.

**Reliability:** prompt guardrails hold ~95% of the time, not 100% — an LLM can still
occasionally take the bait. Fine for a sales bot. If you ever need a hard guarantee, add
an input classifier/moderation step, but that's overkill here.

---

## 6.1-detail Language handling (bilingual only)

**Goal:** reply in the caller's *current* language, switching turn-by-turn, at low latency.

**How it works — two parts, no per-turn code:**
1. **STT auto-detect** (`language="unknown"`, §5) preserves the real language/script of
   each turn, so English stays Latin and Hindi stays Devanagari. (A pinned language
   transliterates English → Devanagari and the LLM then replies in Hindi.)
2. **The sandwiched language rule** (§6.0): stated as HARD RULE 1 *and* repeated as the
   final block with few-shot examples, so the model matches the latest turn's language
   instead of drifting to the conversation's earlier language.

This is a **static-prompt** approach on purpose: it costs **zero** latency because it
never mutates the per-turn context (which would throw away LiveKit's preemptive-generation
head-start — a ~1s hit). The trade-off is that a static rule can drift more than a
deterministic per-turn injection.

**Backchannel carve-out (APPLIED).** A lone acknowledgment in the *other* language
("ठीक है" / "achha" / "hmm" mid-English call) is technically the "most recent message,"
so the bot used to flip language (we saw it reply Hindi to an English question after the
STT mis-scripted "achha" as Punjabi `ਅੱਛਾ`). **RULE 1 and the final block now carve this
out:** *a lone short acknowledgment (ok, okay, yeah, haan, hmm, achha, अच्छा, ठीक है) is
NOT a language switch — keep the language of the caller's last full sentence, whatever
script it arrived in.* If a wrong-language flip still shows up on a real sentence, the
deterministic fallback below is the next lever.

**If static drift is ever unacceptable:** the more forceful fallback is a deterministic
per-turn injection in `on_user_turn_completed`, keyed off Sarvam's STT `language` field
(it returns `hi-IN`/`en-IN` per transcript). It's bulletproof but costs the ~1s
head-start. We deliberately chose the static approach; this is the escape hatch.

**Nudge language:** the silence-nudge `generate_reply` (§10) explicitly says *"continue
in the SAME language you were speaking — never switch,"* so a nudge during an English
call stays English.

---

## 6.2 Number handling (always spoken in English words)

**Goal:** every number is spoken in **English words** ("four hundred ninety-nine"),
regardless of the sentence's language, and the voice never reads raw digits.

**Why not just prompt it?** Asking the LLM to write "English number words inside a Hindi
sentence" is unnatural — it resists and writes Hindi words ("पाँच सौ") instead. So we
split the job:

1. **Prompt** (RULE 3): the LLM writes numbers as **plain digits** (`499`) — natural in
   any language.
2. **TTS-side filter** (`Naina.tts_node`): converts digit runs → English words **just
   before** they reach the voice, using `num2words`.

**Why it adds no latency:** the filter runs on the LLM's **output**, downstream of
generation, so it never touches the preemptive-generation head-start. The conversion is a
local lookup (microseconds), no network. It's a **streaming** filter (`_convert_numbers_in_stream`)
that holds back a trailing digit run so a number split across chunks ("4" + "99") isn't
converted in halves.

**Conversion rules (`_number_to_words`):**
- Normal numbers → cardinal words: `499` → "four hundred ninety-nine", `1,000` → "one thousand".
- **Long runs (>6 digits) or a leading zero** → read **digit-by-digit**: phone `9557693351`
  → "nine five five seven…", OTP `007` → "zero zero seven". (Reading a phone number as a
  quantity would be wrong.)
- Decimals → "three point five". Devanagari numerals (`४९९`) are normalized first.

**Dependency & failure mode:** needs `pip install num2words`. If it's missing the agent
**still runs** (fails soft) but speaks raw digits and logs a warning once.

**Caveats / how to change:**
- **Hindi-turn accent:** on a Hindi sentence the `hi-IN` voice pronounces the English
  words with a Hindi accent. Text is always correct; accent is the residual trade-off.
- **US vs British style:** `num2words` default says "four hundred **and** ninety-nine".
  For US style (no "and"), pass `lang="en_US"` in the two `_num2words(...)` calls.
- **To disable** (revert to numbers in the sentence's language): remove the `tts_node`
  override and change RULE 3 back to "spell numbers out in the reply language."

---

## 7. Tools (functions the LLM can call)

Defined as `@function_tool` methods on `Naina`. The LLM decides when to call them,
based on the prompt + each tool's docstring (the docstring is the LLM's instruction —
edit it to change *when* the tool fires).

| Tool | What it does | When the LLM calls it |
|---|---|---|
| `set_disposition(outcome)` | Records `agent.outcome` (BOOKED/CALLBACK/NOT_INTERESTED/DNC/OUT_OF_ZONE) | when the call reaches an outcome |
| `voicemail_detected()` | Sets outcome `VOICEMAIL`, hangs up | on hearing a voicemail/machine greeting |
| `end_call()` | Waits for the goodbye to finish, then deletes the room (hangs up) | after goodbye, when the call is done |

**Hangup mechanism:** all three end the call by `api.room.delete_room(...)` →
FreeSWITCH gets BYE → call ends → `_on_shutdown` fires with the recorded outcome.

> Note in `end_call` we use `context.wait_for_playout()` (NOT
> `current_speech.wait_for_playout()`), which would deadlock — the tool owns that
> speech handle. Keep this if you add other "speak then act" tools.

---

## 8. Disposition (the outcome POST)

At end of call, `_on_shutdown` maps the outcome to a backend code, builds the payload,
and POSTs it.

**Outcome → code mapping** (`FLOW_OUTCOME_TO_CODE`) — edit to match your backend's codes:

| Flow outcome (LLM) | Backend code (posted) |
|---|---|
| BOOKED | Sale |
| CALLBACK | Callback Requested |
| NOT_INTERESTED | Not Interested |
| DNC | DND |
| OUT_OF_ZONE | Not Interested |
| VOICEMAIL | Voicemail |
| DROPPED / IDLE_DROPPED | Follow-up |

`DROPPED` is the default if no tool set an outcome (e.g. caller hung up mid-call).

**Payload** — edit **`build_disposition_payload()`** (one function, clearly marked).
Add/rename/remove fields freely; nothing else changes. Current shape:
```json
{
  "call_id": "CALL-123", "agent": "naina-bot",
  "prospect": { "name": "...", "phone": "...", "campaign": "..." },
  "outcome": "NOT_INTERESTED", "disposition_code": "Not Interested",
  "started_at": "...", "ended_at": "...", "duration_seconds": 59.1
}
```

**Transport** — `post_disposition()` sends it to `DISPOSITION_WEBHOOK_URL` (optional
bearer token). It **fails soft**: a webhook outage logs a warning and never breaks call
teardown. If the URL is unset, it logs the payload so you can see the shape.

**When it fires (timing).** The POST **starts** the moment the caller or bot leaves the
room (`participant_disconnected`), so it's not delayed by the ~20s process linger. The
POST task is created **once** (`_start_disposition_once`), and the **shutdown callback
`await`s that same task** — which is the crucial part.

> ⚠️ **Why the await matters (subtle, learned the hard way).** The two hangup paths tear
> down very differently: a **caller** hangup lets the worker linger ~20s (plenty of time
> for the ~700ms POST), but a **bot** hangup calls `end_call` → `delete_room`, so the
> process exits in **~30ms**. A bare fire-and-forget POST task gets **killed mid-flight** on
> that fast path — we confirmed the webhook silently never arrived on bot hangup. Awaiting
> the task inside the shutdown callback keeps the process alive until the POST completes, so
> it's delivered on **both** paths. `post_disposition` has a 10s timeout and fails soft, so
> the await can't hang teardown indefinitely.

**Webhook direction:** the agent makes an **outbound POST**. There's nothing to "add"
on the agent side beyond the URL. Your **backend must expose a receiving endpoint** that
consumes this POST (update the lead/CRM/retry logic). LiveKit's own webhooks are a
separate, unrelated feature you do **not** need for this.

---

## 9. Voicemail / answering-machine detection (AMD)

Two paths exist; **only the LLM path is active** right now.

**A. LLM detection (ACTIVE).** The prompt tells the bot to call `voicemail_detected`
when it hears a voicemail greeting; the tool disposes `VOICEMAIL` and hangs up.
- Keys off the **transcript**, so it catches typical greetings but not edge cases
  (very short greetings, immediate beeps, odd phrasing). It cannot hear the beep, so
  it does **not** leave a message — it detects and hangs up.

**B. Dialer/telephony AMD (DORMANT hook).** `entrypoint` checks
`attrs.get("dialer.answered_by")`; if it's `machine`/`voicemail`/`amd`/`fax` it disposes
VOICEMAIL and hangs up before pitching. Inert until you:
1. map a header on the trunk: `"X-Answered-By": "dialer.answered_by"`, and
2. have FreeSWITCH/the dialer set that header from its AMD.

For higher accuracy (and to *leave* messages), enable FreeSWITCH `mod_avmd` upstream and
feed the header. Left as a future upgrade.

---

## 10. Silence handling & dead-line hangup

After the bot finishes a turn and starts *listening*, a `NUDGE_AFTER`-second timer arms
(**10s** — raised from 6 so a natural thinking pause doesn't trigger it).
- Caller speaks (or bot speaks) → timer cancels.
- Timer fires → bot nudges (`generate_reply`), nudge counter increments.
- After `MAX_NUDGES` unanswered nudges → **hang up** (dead line).
- Real caller speech resets the nudge counter.

**Nudges are count-aware** (so we don't pitch into a void):
- **1st silence → a presence check** — *"Hello, are you still there? Can you hear me?"*.
  This confirms the caller is actually on the line before continuing.
- **2nd/3rd silence → re-engage** — one fresh, short line (new soft question/point).
- **4th silence (`nudges >= MAX_NUDGES=3`) → hang up** the dead line.

Every nudge pins two things: **stay in the same language** you were already speaking (so
an English call never gets a Hindi nudge), and **say something NEW** (never repeat a line).
To change the wording or add more escalation steps, edit the `if _idle["nudges"] == 1`
branch in `_idle_timer`.

Wired via `session.on("user_state_changed")` / `("agent_state_changed")`. The timer only
arms in the true `listening` state — not while STT is still transcribing — so it won't
fire over a slow transcript. Tune `NUDGE_AFTER` / `MAX_NUDGES` at the top of the block.

### Max call duration (billing safety)

A separate guard, `_max_duration_guard`, force-hangs-up **any** call that runs past
`MAX_CALL_SECONDS` (default **300s / 5 min**, set via env). This is a hard cost cap: a
stuck or looping call can't rack up STT/LLM/TTS + telephony charges indefinitely. The
outcome is left as whatever was set (default `DROPPED` → `Follow-up`, i.e. retryable).
Change the default via the `MAX_CALL_SECONDS` env var. This is independent of the silence
nudge — silence handles a *quiet* caller; this handles a call that never *ends*.

---

## 11. Turn detection / endpointing

The **local turn detector** (Silero VAD + LiveKit's local mini model) decides when the
caller is done. You'll see a benign startup warning: `cloud turn detector failed (401)` —
the *cloud* turn detector needs LiveKit Cloud; self-hosted correctly falls back to the
local model. **Ignore it.**

Occasionally a trailing-off utterance gets a low end-of-turn probability and the detector
waits ~2.5s to be sure (`endpointing_delay: 2.5` in logs). If that's too slow, tune the
endpointing delay in the AgentSession turn options. Do NOT switch to
`inference.TurnDetector()` (the *cloud* one) on self-hosted — it 401s.

### 11.1 Noise robustness (Layer 1 — noisy caller environments)

Callers phone in from noisy rooms; **background voices** on their side otherwise get
treated as the caller speaking, which **cuts the bot off mid-sentence** and **chops one
caller turn into several**. (Test tell-tale: the problem disappears in a quiet/isolated
room.) The noise is baked into the inbound PSTN audio, so the fix is agent-side.

**Layer 1 = tolerance tuning** (applied; doesn't remove noise, but stops the bot reacting
to quiet/brief noise). The effective lever with **Sarvam STT is the VAD threshold**, not a
word gate:

| Knob | Where | Value (default) | Effect |
|---|---|---|---|
| `activation_threshold` | `silero.VAD.load(...)` in `prewarm()` | **0.6** (0.5) | **Main knob.** Higher speech threshold → quiet background chatter falls below "speech", so it neither starts a turn nor interrupts the bot. |
| `min_speech_duration` | `silero.VAD.load(...)` in `prewarm()` | **0.10** (0.05) | Ignore very short noise blips when starting a turn. |
| interruption params | `AgentSession(...)` | **omitted** (defaults) | Left at LiveKit defaults (immediate VAD interruption) — see the ⚠️ below. |

> ⚠️ **Do NOT add `min_interruption_words` with Sarvam STT.** Sarvam emits its transcript
> only at the **end** of an utterance (no interim words), so a word-count gate can't fire
> until the caller *finishes* their sentence — the bot then talks right over them until
> they stop. We tested `min_interruption_words=2` and it produced exactly this lag, so the
> interruption params are **omitted** (LiveKit default = interrupt as soon as the VAD hears
> above-threshold speech). A word gate would only work with a streaming STT that emits
> interim words.
>
> **Deprecation:** `min_interruption_words` / `min_interruption_duration` (and
> `allow_interruptions`, `turn_detection`, `preemptive_generation`) are being moved into
> `turn_handling=TurnHandlingOptions(...)` for v2.0. That's a **relocation, not new
> behaviour** — it does NOT make a word gate work with Sarvam. Its one interesting addition
> is `interruption={"mode": "adaptive"}`, an acoustic model that rejects backchannels/noise
> as interruptions in <30ms — but it's **LiveKit-Cloud-only** (like Krisp BVC), so it's
> unavailable on this self-hosted stack; `"vad"` is the self-host mode.

**Trade-off with `activation_threshold=0.6`:** quiet or soft-spoken callers may be missed
if you push it higher — 0.6 is a middle ground. Loud, *sustained* background voices can
still interrupt (only Layer 2 / BVC fixes that). If soft callers get dropped, lower it
back toward 0.5; if the office noise still cuts the bot off, that's the signal Layer 1 has
hit its ceiling and you need Layer 2.

**Layer 2 (not done — needs infra):** true background-**voice** cancellation (Krisp
`BVCTelephony` via `RoomInputOptions`) removes other people's voices outright, but it's
tied to **LiveKit Cloud** media transport — not available on this fully self-hosted stack
without moving media to Cloud or licensing Krisp standalone. Open-source pre-filters
(RNNoise/DeepFilterNet) remove hum/fan noise but **not** competing voices. Revisit only if
Layer 1 isn't enough.

---

## 12. Concurrency & process model (important)

- **One call = one OS subprocess.** LiveKit runs each `entrypoint()` in its own process.
  All per-call state (`prospect`, `session`, `agent.outcome`, timers) lives in that
  process. **Call data cannot mix between calls** — isolation is enforced by the OS
  process boundary, not by careful coding.
- **One worker handles many simultaneous calls**, each in its own subprocess, bounded by
  CPU/RAM. Calls are mostly network I/O (STT/LLM/TTS), so a modern 8-core box handles
  dozens.
- **`prewarm()`** loads the Silero VAD once per subprocess *before* it takes a call, and
  `num_idle_processes` keeps N warm processes ready — so call start is instant (no
  "no warmed process" cold start). Size `num_idle_processes` to your peak concurrent calls.
- **Scale out** by running more workers; LiveKit load-balances jobs across them.

---

## 13. SIP header → attribute contract

FreeSWITCH sets `sip_h_X-*` headers; the LiveKit inbound trunk maps them to attributes
(`headers_to_attributes` in `trunk.json`). The agent reads:

| Attribute | From header | Used for |
|---|---|---|
| `dialer.prospect_name` | `X-Prospect-Name` | greeting / addressing |
| `dialer.phone` | `X-Phone` | disposition payload |
| `dialer.campaign` | `X-Campaign` | disposition payload |
| `dialer.call_id` | `X-Call-Id` | disposition payload (ties back to dialer) |
| `dialer.answered_by` | `X-Answered-By` | *optional* AMD hook (§9) |

To add a new field: set the header in the FreeSWITCH dialplan, add the mapping to
`trunk.json`, recreate the trunk (`lk sip inbound delete/create`), and read
`attrs.get("dialer.<x>")` in `entrypoint`.

---

## 14. Known caveats

- `cloud turn detector failed (401)` — benign, self-hosted falls back to local (§11).
- `publisher data channel ... closed unexpectedly` at hangup — benign teardown noise
  from deleting the room out from under the agent's own connection. Harmless.
- **Gemini `503 "model overloaded"`** — a *capacity* error on Google's shared endpoint,
  **not** a billing/quota issue (a paid key doesn't exempt you). It stalls a turn while
  it retries. Mitigate with a `FallbackAdapter([gemini, groq])` and/or Vertex AI
  `asia-south1` (§5).
- **Backchannel language flip** — a short "ok/हम्म" in the other language can flip the
  reply language for a turn (§6.1-detail). Carve-out documented there.
- **Hindi-accent on English numbers** — the `hi-IN` voice speaks the English number
  words with a Hindi accent (§6.2). Text is always correct; accent is the trade-off.
- **`num2words` missing** — agent still runs but speaks raw digits + logs a warning.
  Install it (§3).
- **`[Your Company Name]` spoken aloud** — if the persona has no real company name the
  LLM may voice a bracketed placeholder. Fill the name in (§6.2 checklist).
- **Version drift:** plugin arg names/model ids (`saaras:v3`, `output_audio_codec`,
  `gemini-2.5-flash`, `min_buffer_size`) can change across plugin releases. If startup
  errors on one, check that plugin's current docs. Structure won't change, spelling might.
- The file's top docstring may lag the code; the code is the source of truth — this guide
  matches the code.

---

## 15. "How do I change X?" — quick recipes

| Goal | Do this |
|---|---|
| Change the sales pitch/persona | Edit `instructions=` in `Naina.__init__` — follow the §6.2 checklist (keep HARD RULES + final block, fill company name) |
| Set the real company / add product facts | Replace every `GrowEasy` + add a FACTS block (§6.2, §6.3) |
| Tune the on-topic railguard / deflection wording | Edit HARD RULES 5–6 (§6.3) |
| Change the silence presence-check / re-engage wording | Edit the `_idle_timer` nudge branches (§10) |
| Change the Hindi TTS voice language | Set `NAINA_LANG` (e.g. `en-IN`, `ta-IN`) — this is the TTS voice + Hindi fallback; STT still auto-detects |
| Fix language drift / add a backchannel carve-out | Edit RULE 1 + the final ⚠️ block (§6.1-detail) |
| Change number speech (US style / digit-by-digit / off) | Edit `_number_to_words` + `tts_node` (§6.2) |
| Reduce time-to-first-word (TTS) | Lower `min_buffer_size` / `max_chunk_length` in `sarvam.TTS(...)` (§5) |
| Harden against Gemini 503 | Wrap LLM in `FallbackAdapter([gemini, groq])` (§5) |
| Change the voice | `sarvam.TTS(..., speaker="anushka")` |
| Swap the LLM | Replace `google.LLM(...)` with `groq.LLM(...)` etc. (+ its API key) |
| Add a disposition field | Edit `build_disposition_payload()` |
| Add a new outcome code | Add to `FLOW_OUTCOME_TO_CODE` + mention it in the prompt/tool |
| Make it hang up sooner on silence | Lower `NUDGE_AFTER` / `MAX_NUDGES` |
| Cap call length / control cost | Set `MAX_CALL_SECONDS` (default 300 = 5 min) |
| Handle more concurrent calls | Raise `num_idle_processes`; add more workers |
| Point disposition at your backend | Set `DISPOSITION_WEBHOOK_URL` (+ `_TOKEN`) |
| Add a new dialer field (e.g. lead_id) | Map header in `trunk.json` → read `attrs.get("dialer.lead_id")` |
| Wire real AMD | Enable FreeSWITCH `mod_avmd`, map `X-Answered-By` header (§9) |
