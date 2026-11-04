# Pipecat ⇄ LiveKit — what the agent side needs

`LiveKitTransport(url, token, room_name, params=LiveKitParams())`.
Three inputs. All of them come from the LiveKit deployment. The agent runs on
the SAME host as LiveKit (Host B), so it connects over localhost.

## 1. Connection values to hand the Pipecat side

```bash
# .env for the agent (Host B)
LIVEKIT_URL=ws://127.0.0.1:7880          # co-located → localhost, ws:// (no TLS needed internally)
LIVEKIT_API_KEY=mykey                     # same pair as livekit.yaml / sip.yaml
LIVEKIT_API_SECRET=<the openssl-generated secret>
```

- `url` → `LIVEKIT_URL`
- `api_key`/`api_secret` are NOT passed to the transport directly — they are used
  to **mint the JWT `token`** the transport needs.
- `room_name` is **dynamic** (see §3) — one room per call.

## 2. Minting the token (LiveKit Python SDK)

```python
from livekit import api

def make_token(room_name: str, identity: str = "naina-bot") -> str:
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
```

## 3. The one real design choice: how the agent learns the room name

Each inbound call creates a fresh room `call-xxxx` (our dispatch rule's
`roomPrefix`). The agent must join THAT room. Two standard patterns:

**A) LiveKit Agent Dispatch (recommended for telephony)**
Run the agent as a LiveKit worker; LiveKit assigns a job per new room and hands
the worker the room name + token automatically. You register an agent name and
attach it to the dispatch rule (`roomConfig.agents` / `lk dispatch`).

**B) Explicit — watch for new rooms**
Poll/subscribe to room creation, filter `call-*`, mint a token, join. More code,
but no dependency on the agent framework.

Start with (A) — it's the intended path and removes the token/room plumbing.

## 4. Reading dialer call-context (the X-headers)

Once joined, the SIP caller is a participant carrying the attributes we mapped
in `trunk.json`:

```python
# on participant connected / at pipeline start
attrs = sip_participant.attributes
call_id   = attrs.get("dialer.call_id")
prospect  = attrs.get("dialer.prospect_name")
campaign  = attrs.get("dialer.campaign")
phone     = attrs.get("dialer.phone")
```

Feed `prospect` into the greeting/flow; keep `call_id` for the disposition POST.

## 5. Minimal transport wiring (replaces the old SIP transport)

```python
from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
from pipecat.audio.vad.silero import SileroVADAnalyzer

transport = LiveKitTransport(
    url=os.environ["LIVEKIT_URL"],
    token=make_token(room_name),
    room_name=room_name,
    params=LiveKitParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
)

@transport.event_handler("on_first_participant_joined")
async def on_join(transport, participant):
    # read attrs (§4), kick off the greeting
    ...

@transport.event_handler("on_participant_disconnected")
async def on_leave(transport, participant):
    # map outcome → disposition_map, backend_client.post_dispose(call_id=...)
    ...
```

Everything downstream of the transport — STT → LLM → TTS, your flow, VAD,
interruptions — is UNCHANGED from the old pipeline. Only the transport object
swaps.

## 6. What carries over unchanged from the current codebase

- `control/disposition_map.py` — outcome→code table
- `backend_client.py` — POST Available / POST dispose to the dialer
- your `pipeline/main.py` STT/LLM/TTS/flow — same, minus the SIP transport

## Handoff checklist (give these to whoever builds the agent)

- [ ] LIVEKIT_URL / API_KEY / API_SECRET (§1)
- [ ] Room naming: prefix `call-` (from dispatch rule)
- [ ] Attribute keys: `dialer.call_id|prospect_name|campaign|phone` (§4)
- [ ] Dialer contract: how to POST disposition (endpoint + valid codes — from disposition_map)
- [ ] The existing `pipeline/main.py` (the bot brain) to swap the transport into
