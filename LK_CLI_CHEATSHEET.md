# `lk` CLI cheatsheet

Common commands for running self-hosted LiveKit. Verb spellings drift between
CLI versions — `lk <group> --help` is always the source of truth.

## Connecting (self-hosted, not Cloud)

```bash
# Per-shell env (what we use). Skip the Cloud "authenticate a project" prompt.
export LIVEKIT_URL=ws://127.0.0.1:7880
export LIVEKIT_API_KEY=mykey
export LIVEKIT_API_SECRET=<secret>

# Or save a reusable local project (no prompt afterwards)
lk project add --url ws://127.0.0.1:7880 --api-key mykey --api-secret <secret> --default local
lk project list
```

## Rooms

```bash
lk room list                              # active rooms (a call = a room here)
lk room participants <room>               # who's in it (SIP caller + agent)
lk room create <room>                     # create empty room manually
lk room delete <room>                     # force-close a room (hangs up the call)
lk room send-data <room> --data '{...}'   # push data to participants
lk room mute-track <room> <participant> <track>
```

## Tokens (join credentials for agents/clients)

```bash
# Mint a JWT to join a room (your Pipecat agent needs one of these)
lk token create \
  --api-key mykey --api-secret <secret> \
  --join --room call-1234 --identity bot-agent \
  --valid-for 24h
```

## SIP — inbound trunk (calls FROM FreeSWITCH)

```bash
lk sip inbound create trunk.json          # -> ST_xxxx
lk sip inbound list
lk sip inbound delete ST_xxxx             # no in-place edit; delete + recreate
```

## SIP — dispatch rules (which room an inbound call lands in)

```bash
lk sip dispatch create dispatch.json      # -> SDR_xxxx (uses dispatch_rule wrapper)
lk sip dispatch list
lk sip dispatch delete SDR_xxxx
```

## SIP — outbound (if you ever place calls OUT via a trunk)

```bash
lk sip outbound create outbound-trunk.json    # -> ST_xxxx
lk sip outbound list
# Place a call: dial a number into a room as a SIP participant
lk sip participant create \
  --trunk ST_xxxx --to +15551234567 --room call-out-1 --identity callee
```

## Agent dispatch (auto-assign a worker to new rooms)

```bash
lk dispatch create --room call-1234 --agent-name my-agent
lk dispatch list
lk dispatch delete <id>
```

## Egress (recording) — you do this on FreeSWITCH instead, but for reference

```bash
lk egress start room-composite --room <room> --output file:///out/rec.mp4
lk egress list
lk egress stop <egress-id>
```

## Load testing (validate your concurrency target)

```bash
# Simulate N publishers/subscribers against a room to size the host
lk load-test --room loadtest --publishers 10 --subscribers 10 --duration 2m
```

## Diagnostics quicky's

```bash
docker compose logs -f livekit-sip        # SIP signaling / INVITEs
docker compose logs -f livekit            # SFU / room lifecycle
lk room list                              # is a room being created on call?
ss -lunp | grep 5060                      # SIP port bound
```
