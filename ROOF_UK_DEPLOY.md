# Roof UK campaign — deploy runbook

Adds a third agent worker (`roof-bot`) alongside the two that already run, and routes
SIP calls to the right one. **The Smart Health campaign must keep working throughout** —
step 6 is the verification that proves it did.

---

## What runs after this

| Service | Agent name | Health port | Config |
|---|---|---|---|
| `smarthealth-bot` | `smarthealth-bot` | 8081 | `/opt/livekit/bot/.env` |
| `smarthealth-web` | `smarthealth-web` | 8082 | `/opt/livekit/bot/.env` |
| **`roof-bot`** | **`roof-bot`** | **8083** | **`/opt/livekit/roof-bot/.env`** |

Three things keep them apart and all three matter: distinct `agent_name` (dispatch
targets it), distinct health port (two workers on 8081 crash-loops the second), and a
separate `.env` (one file cannot hold two `DISPOSITION_WEBHOOK_URL`s).

---

## 1. Capacity first

You are putting a third VAD-holding worker on a 4 vCPU box. Before adding it, take a
process off Smart Health:

```bash
grep -n num_idle_processes /opt/livekit/bot/agent_smarthealth_claude_dg.py
# change 3 -> 2, then:
systemctl restart smarthealth-bot
```

`roof-bot` starts at `num_idle_processes=1`. After the first few concurrent calls,
check for the warning that means the box is starved:

```bash
journalctl -u roof-bot -u smarthealth-bot --since "1 hour ago" | grep -i "slower than realtime"
```

Anything there means add cores or move a worker to its own host — not tune around it.

## 2. Files

From your workstation:

```powershell
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp agent_roof_uk.py roof.env.example roof-bot.service trunk-roof.json dispatch-roof.json ROOF_UK_DEPLOY.md root@<DROPLET_IP>:/root/
```

On the server:

```bash
mkdir -p /opt/livekit/roof-bot
cd /root
cp agent_roof_uk.py ROOF_UK_DEPLOY.md /opt/livekit/roof-bot/
cp roof.env.example /opt/livekit/roof-bot/.env
cp roof-bot.service /etc/systemd/system/
systemctl daemon-reload
```

No new venv and no pip install — it reuses `/opt/livekit/bot/.venv`.

## 3. Configure

```bash
nano /opt/livekit/roof-bot/.env
chmod 600 /opt/livekit/roof-bot/.env
```

Copy the shared keys across rather than retyping them:

```bash
grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|DEEPGRAM_API_KEY|ANTHROPIC_API_KEY)=' \
  /opt/livekit/bot/.env
```

Then set `DISPOSITION_WEBHOOK_URL` to the **roof CRM endpoint** — this is the whole
reason the file is separate. Getting it wrong posts UK roof leads into the Australian
health insurance CRM.

## 4. Smoke test before wiring any SIP

```bash
cd /opt/livekit/roof-bot
/opt/livekit/bot/.venv/bin/python agent_roof_uk.py download-files   # VAD (already cached; harmless)
/opt/livekit/bot/.venv/bin/python agent_roof_uk.py start
```

Expect `registered worker id=AW_...`, `health-check server on 127.0.0.1:8083`,
`STT(deepgram): nova-3 language=en-GB` and `TTS(deepgram): aura-2-...`. Ctrl-C, then:

```bash
systemctl enable --now roof-bot
systemctl is-active smarthealth-bot smarthealth-web roof-bot     # three × active
ss -lntp | grep -E '808[123]'                                    # three distinct ports
```

## 5. SIP routing — the part that can break the live campaign

LiveKit picks a trunk by the **called number**, so the two campaigns need two trunks and
two rules. Look at what exists first:

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=APIKEY LIVEKIT_API_SECRET=<secret>
lk sip inbound list       # note IDs, and whether the existing trunk has `numbers`
lk sip dispatch list      # note trunk_ids, roomPrefix, room_config.agents
```

Your existing trunk has **no `numbers`**, which makes it a wildcard that matches any
called number from the FreeSWITCH IP. Create the roof trunk *with* an explicit number so
it is the more specific match:

```bash
nano trunk-roof.json      # numbers: the DID or request-URI user part for this campaign
                          # allowed_addresses: the same FreeSWITCH IP
lk sip inbound create trunk-roof.json          # -> ST_xxxx

nano dispatch-roof.json   # paste ST_xxxx into trunk_ids
lk sip dispatch create dispatch-roof.json      # -> SDR_xxxx
lk sip dispatch list
```

FreeSWITCH then selects the campaign by what it dials:

```xml
<!-- roof campaign extension -->
<action application="bridge" data="sofia/gateway/livekit/ROOF_NUMBER"/>
```

> **Verify this by test call, don't trust it.** Specific-over-wildcard trunk matching is
> the documented behaviour, but your existing trunk is a wildcard and I have not seen
> your deployed config. Place one roof call and one Smart Health call and confirm each
> lands on the right worker. **If a roof call reaches `smarthealth-bot`**, the fix is to
> pin `numbers` on the Smart Health trunk too — which means recreating it and updating
> its dispatch rule's `trunk_ids`. Do that in a maintenance window, not on live traffic.

## 6. Prove nothing broke

```bash
# watch both at once — the identifier tells you which took the call
journalctl -u smarthealth-bot -u roof-bot -f -o short-iso --no-hostname
```

1. Place a **Smart Health** call → room `call-*`, handled by `smarthealth-bot`, ends with
   a disposition POST.
2. Place a **roof** call → room `roof-*`, handled by `roof-bot`, greeting is
   *"Hello, is this the property owner?"*, ends with its own disposition POST to the
   roof CRM.
3. Confirm the two CSVs are separate:
   `/opt/livekit/bot/disposition_smarthealth.csv` and
   `/opt/livekit/roof-bot/disposition_roof.csv`.

## 7. Rollback

```bash
systemctl disable --now roof-bot
lk sip dispatch delete SDR_xxxx
lk sip inbound delete ST_xxxx
```

Nothing about Smart Health was modified except `num_idle_processes`, so that is the
whole rollback.

---

## Things worth knowing about this campaign

**The homeowner gate is in code, not the prompt.** `save_qualification` returns a refusal
string the model cannot ignore when `homeowner` is no — the same pattern as the Smart
Health premium gate, which the model *would* talk itself out of when the caller was
friendly.

**"Accept a no" is HARD RULE 7.** The script never pushes: one soft objection handler,
then a warm exit. The prompt forbids rebutting the same objection twice, because a model
left to its own devices will keep selling.

**Required for a handoff: name, address, callback number.** Roof age is deliberately
*not* required — the script itself says "I'm not sure" is fine and moves on. Requiring it
would make the bot press for something the script gives away.

**Verify the TTS voice id before the first live call.** `aura-2-pandora-en` is the default
British voice here, but Aura ids *are* the voice and a wrong one returns 400 on every
turn — which on a call is indistinguishable from a mute bot. This exact failure cost a
debugging session on the Smart Health build.
