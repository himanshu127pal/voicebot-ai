# Integration guide: move the Pipecat bot onto LiveKit transport

> **Audience:** the Claude session working inside the Pipecat pipeline repo
> (the one with `pipeline/main.py`, the STT/LLM/TTS flow, RAG, etc.).
> **Goal:** replace the custom aiortc SIP transport with LiveKit, driven by
> LiveKit **agent dispatch**. The media/telephony infra is ALREADY deployed —
> you only change how the bot connects and how it's launched.
> **Do NOT** touch the STT/LLM/TTS/flow/RAG logic. Only the transport + launch
> path change.

---

## 1. Architecture: before → after

**Before (what you have now):**
```
FreeSWITCH → custom SIP-over-WSS client (aiortc, DTLS/ICE, monkey-patches)
           → SIPTransport (Pipecat BaseInput/BaseOutput)
           → bot() pipeline
supervisor.py runs a wait_for_call()/handle_call() loop and calls bot(sip_transport=...)
```

**After (target):**
```
FreeSWITCH → LiveKit SIP trunk → room "call-xxxx" (auto-created per call)
           → LiveKit dispatches agent "naina-bot" to that room
           → worker entrypoint(ctx) builds LiveKitTransport(room)
           → bot() pipeline  (UNCHANGED except the transport object it's handed)
```

The custom SIP stack (`sip/sip_ws_client.py`, `sip/sip_transport.py`,
`aiortc_debug_patch.py`, all aiortc/soxr/av deps) is **deleted**. LiveKit owns
all media — no ICE, no DTLS, no resampling, no marker-bit patches.

## 2. What is ALREADY deployed (do not rebuild)

- Self-hosted LiveKit server + Redis + `livekit-sip` on the SAME host the agent
  will run on (reachable at `ws://127.0.0.1:7880`).
- An inbound SIP trunk from FreeSWITCH.
- A SIP dispatch rule: each call → room `call-*`, and it dispatches the agent
  named **`naina-bot`**.
- FreeSWITCH attaches per-call context as SIP `X-headers`, which LiveKit maps to
  **participant attributes**:

  | attribute key           | meaning              |
  |-------------------------|----------------------|
  | `dialer.call_id`        | dialer's call id     |
  | `dialer.prospect_name`  | lead name            |
  | `dialer.campaign`       | campaign             |
  | `dialer.phone`          | caller number        |

## 3. Connection config (env)

The agent host already has these; confirm they're in the agent's env/.env:
```bash
LIVEKIT_URL=ws://127.0.0.1:7880      # localhost, ws:// (co-located; no TLS needed)
LIVEKIT_API_KEY=mykey
LIVEKIT_API_SECRET=<secret>          # same pair used by the LiveKit server
```

## 4. Dependencies

```bash
pip install "pipecat-ai[livekit]"    # brings the LiveKit transport
pip install livekit-agents           # the agent-dispatch worker harness
pip install livekit-api              # AccessToken (token minting)
```
Remove (no longer used): `aiortc`, `soxr`, `av` (if only used by the old SIP path),
and the `sip/` package + `aiortc_debug_patch.py`.

## 5. Your tasks (in order)

### Task A — Add an agent-dispatch worker as the new entry point

Create `agent_worker.py` (reference implementation is in this folder). It:
1. registers a worker with `agent_name="naina-bot"` (must match the dispatch rule),
2. runs `entrypoint(ctx)` once per call,
3. gets the room name from `ctx.room.name`,
4. reads the SIP participant's `dialer.*` attributes,
5. builds `LiveKitTransport` for that room and runs your existing `bot()`.

This **replaces** `run_naina.py` + the `supervisor.py` wait_for_call/handle_call
loop as the process you launch. (Keep `disposition_map.py` and `backend_client.py`.)

### Task B — Swap the transport object

Find where the pipeline currently builds its transport. Search the repo:
```
grep -rn "SIPTransport\|SmallWebRTCTransport\|transport.type\|def bot(" pipeline/
```
`bot()` currently accepts an injected transport (signature seen in supervisor.py:
`bot(runner_args, backend_client, sip_transport)`). Change it to accept a generic
`transport` and build the pipeline with a `LiveKitTransport` instead:

```python
from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
from pipecat.audio.vad.silero import SileroVADAnalyzer

transport = LiveKitTransport(
    url=os.environ["LIVEKIT_URL"],
    token=token,             # minted in the worker, scoped to this room
    room_name=room_name,
    params=LiveKitParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
        # keep audio_in_filter / mixer (room tone) here if you used them before
    ),
)
```

Everything after this (pipeline processors, STT, LLM, TTS, flow manager) stays
the same — it consumes `transport.input()` / `transport.output()` exactly like
the old transport.

### Task C — Map the old event handlers

The old SIP transport fired `on_client_connected` / `on_client_disconnected`.
LiveKit transport fires participant events. Rename the handlers:

| Old (SIP)                     | New (LiveKit)                          |
|-------------------------------|----------------------------------------|
| `on_client_connected`         | `on_first_participant_joined`          |
| `on_client_disconnected`      | `on_participant_disconnected`          |

Inside the "joined" handler, read the dialer context off the participant and
inject it where the old code injected `prospect` (the flow_manager.state / greeting):

```python
@transport.event_handler("on_first_participant_joined")
async def on_join(transport, participant):
    attrs = participant.attributes
    prospect = {
        "name":     attrs.get("dialer.prospect_name"),
        "phone":    attrs.get("dialer.phone"),
        "campaign": attrs.get("dialer.campaign"),
        "call_id":  attrs.get("dialer.call_id"),
    }
    # inject `prospect` into flow state / kick off greeting (same as before)
```

### Task D — Preserve the disposition flow (unchanged logic)

On call end, keep exactly what you had — the outcome→code mapping and the POST:
```python
from control.disposition_map import map_outcome_to_code
# outcome = flow result, e.g. "BOOKED"
code = map_outcome_to_code(outcome)
await backend_client.post_dispose(code=code, call_id=prospect["call_id"])
```
The dialer contract (endpoints, valid codes) has NOT changed — only how the call
reaches the bot changed. `backend_client.py` and `disposition_map.py` are reused
as-is.

## 6. The one cross-framework detail to get right

`livekit-agents` (the worker) and Pipecat's `LiveKitTransport` can each connect
to the room. Pick ONE owner of the room connection:

- **Recommended:** let **Pipecat's LiveKitTransport** connect (mint a token in the
  worker, pass it to the transport, and do NOT call `ctx.connect()`).
- **Alternative:** call `ctx.connect()` and reuse `ctx.room` if the transport
  supports an existing room object.

Confirm which pattern the reference `livekit-sip-agent-example`
(https://github.com/livekit-examples/livekit-sip-agent-example) uses for the
installed versions and match it. This is the only place the two frameworks touch.

## 7. Keep unchanged (explicit non-goals)

- STT / LLM / TTS services and their config
- The conversation flow / flow_manager / prompts
- RAG / vector store
- `disposition_map.py`, `backend_client.py`
- Audio processing choices (VAD, noise suppression, room tone) — carry them onto
  `LiveKitParams` the same way they were on the old transport params.

## 8. Verify (definition of done)

1. `python agent_worker.py dev` starts and logs "registered worker naina-bot".
2. Bridge a test call on FreeSWITCH to the agent extension.
3. Worker logs a job for room `call-xxxx` and prints the `dialer.*` context.
4. Two-way audio: the bot greets and responds (STT/LLM/TTS working over LiveKit).
5. On hangup, the disposition POST fires with the correct `call_id`.
6. A second concurrent call gets its own room + its own entrypoint (no "busy").

## 9. Gotchas

- `agent_name` in `WorkerOptions` MUST equal the `agentName` in the dispatch rule
  (`naina-bot`), or the worker never gets jobs.
- Token must carry `room_join=True` and the exact room name, or join is rejected.
- `LIVEKIT_API_KEY/SECRET` must match the LiveKit server's — mismatch = auth fail.
- Don't reintroduce manual resampling; LiveKit delivers audio at the rate you set
  in `LiveKitParams`. Let Pipecat handle it.
- Read participant attributes AFTER the participant has joined
  (`on_first_participant_joined` / `ctx.wait_for_participant()`), not before.
```
