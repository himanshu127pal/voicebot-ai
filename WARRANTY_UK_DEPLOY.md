# Warranty UK campaign — deploy runbook

Adds a **fifth** telephony worker (`warranty-bot`) alongside the four that already run,
and gives the UK appliance repair-cover bot its own phone line. **Every live campaign
must keep working throughout** — step 9 is the verification that proves it did.

This bot already exists on the browser demo. The agent file moves from
`/opt/livekit/web-demo/bots/` to `/opt/livekit/warranty-bot/`, and the demo then loads it
from there, exactly as it does for the other phone bots — so the phone and the demo can
never drift apart.

> **Read this before anything else.** This script has the same shape as a well-known UK
> appliance-warranty scam: an unexpected call, a household appliance, a price, a payment.
> What makes this campaign legitimate is what the bot refuses to do, and those refusals
> are in code, not in a prompt someone can soften later. Before it dials a single real
> number, the client must be able to answer: who is the company, what is the policy
> number, what does the written pack contain, and who answers the number the bot gives
> out. If those answers aren't ready, the campaign isn't ready.

---

## What runs after this

| Service | Agent name | Health port | Config | Disposition endpoint |
|---|---|---|---|---|
| `smarthealth-bot` | `smarthealth-bot` | 8081 | `/opt/livekit/bot/.env` | `/disposition/smarthealth` |
| `roof-bot` | `roof-bot` | 8083 | `/opt/livekit/roof-bot/.env` | `/disposition/roof` |
| `finance-bot` | `finance-bot` | 8084 | `/opt/livekit/finance-bot/.env` | `/disposition/finance` |
| `voicebot-web` | `voicebot-web` | 8085 | per bot, per call | — (demo calls never post) |
| `movers-bot` | `movers-bot` | 8086 | `/opt/livekit/movers-bot/.env` | `/disposition/movers` |
| **`warranty-bot`** | **`warranty-bot`** | **8087** | **`/opt/livekit/warranty-bot/.env`** | **`/disposition/warranty`** |

Three things keep them apart and all three matter: distinct `agent_name` (the dispatch
rule targets it), distinct health port (two workers on one port crash-loops the second),
and a separate `.env` (one file cannot hold five `DISPOSITION_WEBHOOK_URL`s).

---

## 1. Capacity — this is the one that will bite

You are putting a **sixth** VAD-holding worker on a 4 vCPU box: five telephony plus the
demo. That is past comfortable. Before you enable it:

```bash
grep -n num_idle_processes /opt/livekit/bot/agent_smarthealth_claude_dg.py
# 3 -> 2 if it is still 3, then: systemctl restart smarthealth-bot
```

After the first few concurrent calls, check for the warning that means the box is
starved — and take it seriously rather than tuning around it:

```bash
journalctl -u smarthealth-bot -u roof-bot -u finance-bot -u movers-bot -u warranty-bot \
  --since "1 hour ago" | grep -i "slower than realtime"
```

Anything there means add cores or move a campaign to its own host. Two concurrent calls
on two different campaigns is the load case to test before you trust it.

## 2. Files

From your workstation:

```powershell
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp agent_warranty_uk.py warranty.env.example warranty-bot.service `
    trunk-warranty.json dispatch-warranty.json WARRANTY_UK_DEPLOY.md root@<DROPLET_IP>:/root/
scp dispositions/schema_warranty.sql dispositions/receiver.py root@<DROPLET_IP>:/root/
scp agent_voicebot_web.py webdemo/demo_bots.py webdemo/setup.sh `
    webdemo/voicebot-web.service webdemo/WEB_DEMO.md root@<DROPLET_IP>:/root/
```

On the server:

```bash
mkdir -p /opt/livekit/warranty-bot
cd /root
cp agent_warranty_uk.py WARRANTY_UK_DEPLOY.md /opt/livekit/warranty-bot/
cp warranty.env.example /opt/livekit/warranty-bot/.env
cp warranty-bot.service /etc/systemd/system/
systemctl daemon-reload
```

No new venv and no pip install — it reuses `/opt/livekit/bot/.venv`.

## 3. The new disposition table

The `voicebot` database already exists, so run the **migration**, not the full
`schema.sql`:

```bash
mysql -u root -p < /root/schema_warranty.sql
mysql -u root -p -e "USE voicebot; SHOW TABLES;"
# fin_call_dispositions
# mov_call_dispositions
# roof_call_dispositions
# sh_call_dispositions
# wty_call_dispositions     <- new
```

Then the receiver, which now has a `/disposition/warranty` endpoint:

```bash
cp /root/receiver.py /opt/disposition-receiver/
systemctl restart disposition-receiver
curl -s localhost:8095/health          # {"ok":true,"db":"up"}
```

**Test the endpoint before a bot ever posts to it** — once with a quote, once with the
case this campaign exists to handle properly, an appliance that is already broken:

```bash
curl -s -X POST localhost:8095/disposition/warranty \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-WTY-1","campaign":"warranty-uk","phone":"+447700900123",
  "prospect_name":"Alex Taylor",
  "started_at":"2026-10-05T10:02:11.550300+00:00",
  "ended_at":"2026-10-05T10:06:48.113900+00:00",
  "duration_sec":277,"outcome":"INFO_REQUESTED",
  "outcome_note":"Written details agreed - sending",
  "consent_given":true,"info_sent":true,"eligibility":"eligible",
  "callback_request":"","decline_reason":"","remarks":"test row",
  "qualification":{"appliance":"washing machine","appliance_confirmed":"yes",
    "appliance_age":"about three years","working_normally":"yes","existing_fault":"none",
    "already_covered":"no","interest":"wants the details","discuss_with":"","notes":"",
    "first_name":"Alex","last_name":"Taylor","email":"alex@example.co.uk",
    "email_spoken":"alex at example dot co dot uk","contact_number":"+447700900123"},
  "quote":{"months":12,"price":99.0,
    "includes":["engineer call-out","labour","replacement parts","repair costs"]},
  "missing_required":[],"card_numbers_blocked":0}'
# {"status":"ok","id":1}

curl -s -X POST localhost:8095/disposition/warranty \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-WTY-2","campaign":"warranty-uk","phone":"+447700900124",
  "started_at":"2026-10-05T10:20:00.000000+00:00",
  "ended_at":"2026-10-05T10:22:40.000000+00:00",
  "duration_sec":160,"outcome":"EXISTING_FAULT",
  "outcome_note":"Existing fault - not sold, referred",
  "consent_given":false,"info_sent":false,"eligibility":"eligible",
  "callback_request":"","decline_reason":"","remarks":"told them plainly",
  "qualification":{"appliance":"washing machine","appliance_confirmed":"yes",
    "appliance_age":"four years","working_normally":"no",
    "existing_fault":"it has been leaking for a week","already_covered":"no",
    "interest":"","discuss_with":"","notes":"","first_name":"","last_name":"",
    "email":"","email_spoken":"","contact_number":""},
  "quote":{},"missing_required":["first_name","contact_number"],
  "card_numbers_blocked":1}'
# {"status":"ok","id":2}
```

Check both rows. The second must have **NULL in every `quote_*` column** — the bot is
forbidden from quoting an appliance that already has a fault, and "never quoted" has to
be distinguishable from "quoted zero":

```bash
mysql -u root -p -e "USE voicebot; SELECT id, outcome, eligibility, quote_price,
  qual_existing_fault, card_numbers_blocked FROM wty_call_dispositions;"

mysql -u root -p -e "USE voicebot; DELETE FROM wty_call_dispositions WHERE call_id LIKE 'TEST-WTY-%';"
```

**The standing report for this campaign** is the one that proves the guard works. It
should always come back empty:

```sql
SELECT id, call_id, outcome, qual_existing_fault FROM wty_call_dispositions
WHERE qual_existing_fault NOT IN ('', 'none') AND consent_given = 1;
```

A row there means someone was talked into cover for an appliance that was already
broken. That must never happen, and if it ever does, stop the campaign and tell me.

## 4. Configure

```bash
nano /opt/livekit/warranty-bot/.env
chmod 600 /opt/livekit/warranty-bot/.env

grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|DEEPGRAM_API_KEY|ANTHROPIC_API_KEY)=' \
  /opt/livekit/bot/.env            # copy the shared keys across rather than retyping
```

In order of what will embarrass you soonest:

1. **`COMPANY_NAME`, `CONTACT_NUMBER`, `COMPANY_WEBSITE`** — on this campaign people
   **will** ask who is calling, and "I can't tell you" is what a scam sounds like. The
   number must be answered by a human who knows about this campaign.
2. **`PLAN_PRICE`, `PLAN_MONTHS`, `PLAN_INCLUDES`** — placeholder pricing ships in this
   file. `PLAN_INCLUDES` is pipe-separated and spoken as a list. Every figure the bot
   says is computed from these; the model cannot round, discount or invent one.
3. **`MAX_APPLIANCE_YEARS`** — above this the bot stops, says cover has to be checked
   and offers the written details. It never says "no" outright and never quotes.
4. **`DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/warranty`** — this is the
   whole reason the file is separate.
5. **`TTS_VOICE`** — `aura-2-pandora-en` is the British feminine voice (`roof-bot` uses
   it) and the default here. ⚠️ A wrong Aura id returns 400 on **every** turn, which on a
   call is indistinguishable from a mute bot. Verify it against Deepgram's current list.

## 5. Smoke test before wiring any SIP

```bash
cd /opt/livekit/warranty-bot
/opt/livekit/bot/.venv/bin/python agent_warranty_uk.py download-files   # VAD (cached; harmless)
/opt/livekit/bot/.venv/bin/python agent_warranty_uk.py start
```

Expect `registered worker id=AW_...`, `health-check server on 127.0.0.1:8087`,
`STT(deepgram): nova-3 language=en-GB` and `TTS(deepgram): aura-2-pandora-en`. Ctrl-C,
then:

```bash
systemctl enable --now warranty-bot
systemctl is-active smarthealth-bot roof-bot finance-bot movers-bot warranty-bot voicebot-web
ss -lntp | grep -E '808[1-7]'            # distinct ports
```

## 6. SIP routing — the part that can break a live campaign

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=APIKEY LIVEKIT_API_SECRET=<secret>
lk sip inbound list       # note IDs, and which trunks have `numbers`
lk sip dispatch list

nano trunk-warranty.json     # numbers: the DID or request-URI user part for this campaign
                             # allowed_addresses: the same FreeSWITCH IP as the others
lk sip inbound create trunk-warranty.json        # -> ST_xxxx

nano dispatch-warranty.json  # paste ST_xxxx into trunk_ids
lk sip dispatch create dispatch-warranty.json    # -> SDR_xxxx
```

FreeSWITCH then selects the campaign by what it dials:

```xml
<!-- warranty campaign extension -->
<action application="bridge" data="sofia/gateway/livekit/WARRANTY_NUMBER"/>
```

> **Verify by test call, don't trust it.** Specific-over-wildcard trunk matching is
> documented behaviour, but if a warranty call reaches another worker the fix is to pin
> `numbers` on that campaign's trunk too — recreating it and updating its dispatch rule.
> Maintenance window, not live traffic.

## 7. What the dialer should send

| Header | Attribute | Why |
|---|---|---|
| `X-Appliance` | `dialer.appliance` | **Matters here.** The opening line names the appliance: "cover for your washing machine". Without it the bot says "washing machine" by default, which is wrong for a fridge |
| `X-Prospect-Name` | `dialer.prospect_name` | Used in the greeting: "may I speak with Alex?" |
| `X-Phone` | `dialer.phone` | "Is this the best number?" resolves to it, so they never have to read digits out |
| `X-Call-Id` | `dialer.call_id` | Ties the disposition back to your dialer |
| `X-Campaign` | `dialer.campaign` | Lands in the disposition; defaults to `warranty-uk` |

All optional — the call works without any of them, it just asks more questions. If
`X-Phone` is missing, LiveKit's own `sip.phoneNumber` is the fallback.

## 8. The browser demo — same file, new location

```bash
cp /root/agent_voicebot_web.py /opt/livekit/web-demo/
cp /root/demo_bots.py /root/WEB_DEMO.md /opt/smarthealth-webdemo/
cp /root/voicebot-web.service /etc/systemd/system/ && systemctl daemon-reload

# The demo's copy is redundant now. Move it aside rather than deleting it.
mv /opt/livekit/web-demo/bots/agent_warranty_uk.py /root/agent_warranty_uk.py.bak 2>/dev/null || true
mv /opt/livekit/web-demo/bots/warranty.env /root/warranty.env.demo-backup 2>/dev/null || true

systemctl restart voicebot-web          # finishes any demo call in progress first
systemctl restart smarthealth-webdemo
curl -s localhost:8090/healthz          # "bots" still lists every one
```

Take one demo call as Appliance Cover: it should speak British English, quote in pounds,
and refuse flatly if you ask to pay by card. Demo calls do **not** post to the CRM —
`voicebot-web` blanks the webhook — so nothing you do on the page reaches
`wty_call_dispositions`.

## 9. Prove nothing broke

```bash
journalctl -u smarthealth-bot -u roof-bot -u finance-bot -u movers-bot -u warranty-bot \
  -u disposition-receiver -f -o short-iso --no-hostname
```

1. One call per existing campaign → each lands on its own worker, each posts its own
   disposition. Nothing about them changed, so anything that moved is a routing problem.
2. A **warranty** call → room `war-*`, handled by `warranty-bot`, greeting names the
   appliance, ends with a POST to `/disposition/warranty`.
3. On that call, deliberately **ask to pay by card**. It must refuse and say the details
   come in writing first. If it ever takes a number, stop the campaign.
4. Counts by campaign:

```bash
curl -s localhost:8095/stats
# {..., "warranty":{"INFO_REQUESTED":1}}
```

## 10. Rollback

```bash
systemctl disable --now warranty-bot
lk sip dispatch delete SDR_xxxx
lk sip inbound delete ST_xxxx
```

The table and the receiver endpoint can stay — an unused table costs nothing and keeping
it means the rollback is not a migration. To put the demo back to loading it from
`/opt/livekit/web-demo/bots/`, restore `/root/agent_warranty_uk.py.bak` there along with
the previous `agent_voicebot_web.py` and `demo_bots.py`, and restart `voicebot-web` and
`smarthealth-webdemo`.

---

## Things worth knowing about this campaign

**No payment is ever taken on the call, and there is no tool that could take one.** The
policy goes out in writing and payment happens from there. This is deliberately stricter
than the source script, which left phone payment open. On an outbound cold call to a
householder, card details by voice are the scam signature and the one thing the customer
cannot verify in the moment. If the client insists on phone payment, it belongs with a
human agent on a recorded line — not here. Changing that is a code change, not a setting,
which is the point.

**Card numbers never reach the model.** Any run of 12+ digits, written or spoken as
words, is stripped from the caller's turn before the LLM sees it, and `save_details`
refuses to store one. `card_numbers_blocked` counts it.

**An appliance with an existing fault is never sold cover.** `get_price` refuses outright
once a fault is recorded, and the bot says plainly that existing faults aren't covered by
a new policy. The outcome is `EXISTING_FAULT`, which is a success for this campaign, not
a loss.

**Eligibility is decided in code from the age**, not by the model and not by the
customer's opinion. Over `MAX_APPLIANCE_YEARS` the bot refers it and quotes nothing. It
never tells anyone they are covered — only what the plan is designed to help with, and
that the written terms decide.

**The close is written information, never a sale.** `INFO_REQUESTED` is the win.
Everything else — `THINKING`, `ALREADY_COVERED`, `NOT_ELIGIBLE` — is a call that ended
honestly, and the script is written to let them.

**What to brief the client on before the first batch:** who answers `CONTACT_NUMBER`,
what the written pack contains (the bot promises company details, price, cover period,
inclusions, exclusions, cancellation terms and contact details — all of it must be in
there), and that a customer may ring back having been told they would receive something.
