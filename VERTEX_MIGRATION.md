# Fixing the 503s — move Gemini from AI Studio to Vertex AI

**Problem.** The agent calls `generativelanguage.googleapis.com` (Google AI Studio).
That endpoint has **no dedicated capacity** — every caller shares one pool, so when
global demand spikes you get `503 "This model is currently experiencing high demand"`
regardless of model or billing tier. A paid key raises *quota* (429s), not *capacity*
(503s). This is why upgrading the model didn't help: we saw it on 2.5-flash **and** 3.7-flash.

**Fix.** Same models, same plugin, same prompts — Google Cloud's endpoint instead:
**99.5% uptime SLO**, optional reserved throughput, and a regional endpoint that also
cuts latency for Australian calls.

This changes ONE call site. Nothing else in the agent moves.

---

> **Naming note (2026): "Vertex AI" is now "Gemini Enterprise Agent Platform".**
> Google rebranded and expanded Vertex AI; the Console menus and docs may show the new
> name. This is a product/branding change, **not** an API change — the service is still
> `aiplatform.googleapis.com`, the IAM role is still **Vertex AI User**
> (`roles/aiplatform.user`), and the SDK is still `vertexai=True` + project + location.
> Everything below applies as written; only some labels differ. If the API Library
> search for "Vertex AI API" comes up short, search "Gemini Enterprise Agent Platform"
> or "aiplatform" instead. Step 5's verification script settles any doubt in seconds.

## 1. Google Cloud setup (once) — Cloud Console

All of this is click-through at **https://console.cloud.google.com**.

**a. Create (or pick) the project**
Project dropdown in the top bar → **New Project** → name it e.g. `smarthealth-voice`
→ **Create**. Then make sure that project is selected in the dropdown for every step
below — picking the wrong project is the single most common cause of a later 403.
Note the **Project ID** shown on the dashboard (it may differ from the name, e.g.
`smarthealth-voice-481207`) — that exact ID is what goes in `VERTEX_PROJECT`.

**b. Enable billing** (Vertex refuses to serve without it)
☰ menu → **Billing** → **Link a billing account** → pick/create one.

**c. Enable the Vertex AI API**
☰ menu → **APIs & Services** → **Library** → search **"Vertex AI API"** → open it →
**Enable**. Wait for it to finish before the next step.

## 2. Service account + key — Cloud Console

**a. Create the service account**
☰ menu → **IAM & Admin** → **Service Accounts** → **+ Create service account**
- *Service account name*: `livekit-agent`
- **Create and continue**
- *Grant this service account access to project* → Role: **Vertex AI User**
  (search "Vertex AI User" — do **not** grant Editor or Owner; this role is enough)
- **Continue** → **Done**

**b. Download the JSON key**
Click the new `livekit-agent@...` account → **Keys** tab → **Add key** →
**Create new key** → **JSON** → **Create**. The file downloads to your computer.

**c. Put the key on the server**
From your laptop:

```bash
scp ~/Downloads/smarthealth-voice-xxxxx.json root@<server>:/opt/livekit/bot/vertex-sa.json
```

Then on the server, lock it down — it is a credential:

```bash
chmod 600 /opt/livekit/bot/vertex-sa.json
```

> Treat that JSON like a password: never commit it, never put it in the repo or in a
> shared drive. If it leaks, delete the key in the **Keys** tab and create a new one.

## 3. `.env`

```bash
GOOGLE_APPLICATION_CREDENTIALS=/opt/livekit/bot/vertex-sa.json
VERTEX_PROJECT=smarthealth-voice-481207  # the PROJECT ID from the dashboard, not the name
VERTEX_LOCATION=australia-southeast1     # Sydney: lowest latency for AU callers
# GOOGLE_API_KEY is no longer used for the LLM; harmless to leave in place.
```

Region choice: `australia-southeast1` for the AU campaign (latency + data residency).
`asia-south1` (Mumbai) if the worker ever serves India. `global` also works and is the
most availability-resilient, at the cost of unpredictable routing.

## 4. The code change

In `agent_smarthealth.py`, inside `_make_session()`:

```python
# BEFORE — AI Studio, shared capacity, intermittent 503s
llm=google.LLM(model="gemini-2.5-flash"),

# AFTER — Vertex AI: 99.5% SLO, regional endpoint, same model + prompts
llm=google.LLM(
    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    vertexai=True,
    project=os.getenv("VERTEX_PROJECT"),
    location=os.getenv("VERTEX_LOCATION", "australia-southeast1"),
),
```

Auth comes from `GOOGLE_APPLICATION_CREDENTIALS` — no key argument.

## 5. Verify before a live call

```bash
./.venv/bin/python - <<'PY'
import os, time
from dotenv import load_dotenv; load_dotenv()
from google import genai
c = genai.Client(vertexai=True, project=os.environ["VERTEX_PROJECT"],
                 location=os.getenv("VERTEX_LOCATION", "australia-southeast1"))
t = time.perf_counter()
r = c.models.generate_content(model="gemini-2.5-flash", contents="Say OK")
print(f"{time.perf_counter()-t:.2f}s ->", (r.text or "").strip())
PY
```

A time and `OK` means auth, project, region and billing are all good. An error here is
a *setup* problem — fix it before touching the agent.

---

## While you're here: retire gemini-2.5-flash

Vertex fixes the 503s. It does **not** fix the other defect we hit on 2.5-flash:
intermittently returning an empty stream (`"no response generated"`, FinishReason.STOP,
livekit/agents#4706) — that was the ~27s of dead air on one call. Reports of that bug
are specific to 2.5-flash.

Once Vertex is verified, move up:

```bash
GEMINI_MODEL=gemini-3.5-flash
```

LiveKit's own tool-heavy voice benchmark (100-scenario "Hotel Receptionist") scores it
**82% pass / 775ms median TTFT** — the top pass-rate tier, and validated on the exact
workload shape this agent has. https://livekit.com/benchmarks/capabilities

Change the model **after** Vertex is confirmed working, not at the same time — one
variable at a time.

## If 503s ever persist

On Vertex a 503 means a genuine regional outage, not shared-pool contention. Options,
in order: switch `VERTEX_LOCATION` (e.g. `global`), buy **Provisioned Throughput**
(reserved capacity, fixed price), or add a cross-vendor `FallbackAdapter`. Do the
fallback only once a second provider is independently proven — an unproven fallback
adds failure modes rather than removing them.
