# LiveKit self-hosted — full deploy runbook

> **⚠️ Pipecat was dropped — the agent is the Naina LiveKit worker
> (`naina_agent_bilingual.py`).** This runbook covers the **media stack** (LiveKit
> server + SIP + trunk/dispatch); it's still correct. For deploying the **agent** itself
> (venv, `num2words`, systemd), use **`DEPLOY_NEW_SERVER.md`**, and see `DEVELOPER_GUIDE.md`.

Two hosts:
- **Host A — FreeSWITCH** (already exists; the dialer bridges calls here)
- **Host B — LiveKit media stack + the Naina agent** (what we deploy below)

All "Host B" commands run on the LiveKit box. FreeSWITCH steps are marked.

---

## 0. Prerequisites (Host B, once)

```bash
# Docker Engine + compose plugin
curl -fsSL https://get.docker.com | sh
docker compose version

# lk CLI (host tool, used to create the trunk + dispatch rule)
curl -sSL https://get.livekit.io/cli | bash
lk --version
```

## 1. Config files (Host B)

Put these in one dir, e.g. `/etc/livekit/livekit-migration/`:
`docker-compose.yml`, `livekit.yaml`, `sip.yaml`, `trunk.json`, `dispatch.json`.

Generate ONE api secret and use the SAME key/secret in all three places
(livekit.yaml, sip.yaml, and the lk env vars in step 4):

```bash
openssl rand -hex 32     # -> paste as the api_secret everywhere
```

Fill in the placeholders:
- `livekit.yaml`  -> `keys:` = `mykey: <secret>`
- `sip.yaml`      -> `api_key: mykey`, `api_secret: <secret>`
- `sip.yaml`      -> `use_external_ip: true` (private VPC: pin `nat_1to1_ip: <Host B private IP>` instead)
- `trunk.json`    -> `allowed_addresses: ["<FreeSWITCH source IP>"]`  (NOT 127.0.0.1)

## 2. Start the stack (Host B)

```bash
cd /etc/livekit/livekit-migration
docker compose up -d
docker compose ps                          # all 3 up: redis, livekit, livekit-sip
docker compose logs livekit-sip | grep -i sip   # expect SIP signaling listening on :5060
```

## 3. Verify the server responds (Host B)

```bash
curl http://127.0.0.1:7880                  # should respond (not connection refused)
ss -lunp | grep 5060                        # UDP 5060 bound (SIP)
```

## 4. Point lk at YOUR server (self-hosted, not Cloud) (Host B)

If lk prompts "No local projects found. Authenticate one?" choose **No** and use env vars:

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880
export LIVEKIT_API_KEY=APIKEY
export LIVEKIT_API_SECRET=42b75de07fc84a550df1b885ffa3d7d8491bc8734357fc7335d08f7184fc070c
```

## 5. Create trunk + dispatch rule (Host B)

JSON must be strict (no `//` comments — protobuf JSON rejects unknown fields).

```bash
lk sip inbound create trunk.json           # -> SIPTrunkID: ST_xxxx
lk sip inbound list                        # confirm AllowedAddresses = FreeSWITCH IP
```

Put that trunk ID into dispatch.json's trunk_ids, then:

```bash
lk sip dispatch create dispatch.json       # -> SDR_xxxx
lk sip dispatch list
```

Note: dispatch.json uses the `dispatch_rule` wrapper (newer lk schema). If your
lk build errors, run `lk sip dispatch create --help` for its expected shape.

## 6. Firewall (Host B) — open ONLY from FreeSWITCH

```
UDP 5060           (SIP signaling)   from Host A
UDP 10000-20000    (RTP media)       from Host A     <- #1 cause of silent audio
```
Everything else (7880/7881/50000-60000/6379) stays internal to Host B.

## 7. FreeSWITCH side (Host A)

Gateway — `conf/sip_profiles/external/livekit.xml` (set proxy = Host B IP:5060):
```xml
<include>
  <gateway name="livekit">
    <param name="proxy"    value="HOSTB_IP:5060"/>
    <param name="register" value="false"/>
    <param name="context"  value="public"/>
    <param name="ping"     value="30"/>
  </gateway>
</include>
```
```bash
fs_cli -x "sofia profile external rescan"
fs_cli -x "sofia status gateway livekit"
```

Dialplan — bridge the agent extension to the trunk with call-context X-headers
(see dialplan-to-bot.xml). Adjust `^1000$` and the `${...}` vars to your dialer.

## 8. Test

```bash
# Host B: watch the SIP service
docker compose logs -f livekit-sip
```
Bridge a call to ext 1000 on FreeSWITCH. Expect in the logs:
- inbound INVITE from FreeSWITCH IP
- a new room `call-xxxx` created
- a SIP participant joins with attributes dialer.call_id / dialer.prospect_name ...

Confirm the room + participant:
```bash
lk room list
lk room participants <room-name>
```
(No Pipecat agent yet = call connects but nobody talks. Use the stock
livekit-sip-agent-example as a placeholder answerer to verify two-way audio.)

---

## Troubleshooting (the ones that actually happen)

| Symptom | Cause | Fix |
|---|---|---|
| lk prompts for Cloud login | self-hosted needs env creds | export LIVEKIT_URL/API_KEY/API_SECRET |
| `unknown field "//"` | comments in JSON | remove all `//` keys |
| dispatch `missing rule` | old flat schema | wrap in `dispatch_rule` (or check `--help`) |
| INVITE arrives, instantly rejected | allowed_addresses != FreeSWITCH IP | recreate trunk with correct IP/CIDR |
| Call connects, no room created | api_key/secret mismatch sip.yaml vs livekit.yaml | make them identical |
| Call connects, total silence | RTP range 10000-20000 not open | open UDP range Host A -> Host B |
| One-way / wrong media IP | SDP advertises unreachable IP | fix use_external_ip / nat_1to1_ip in sip.yaml |
| Host networking error on `up` | not Linux / Docker Desktop | this stack is Linux-only |
