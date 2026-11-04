# Movers AU campaign — deploy runbook

Gives the Australian removalist booking bot its own phone line: a telephony worker
(`movers-bot`) alongside the others already running. **Every live campaign must keep
working throughout** — step 8 is the verification that proves it did.

This bot already exists — the browser demo has been running it — and nothing about what
it says changes here. Same file, same prompt, same tools, now also run by
`movers-bot.service` for SIP calls, so a script change lands on the phone and the demo
together after a restart. **The rate card is placeholder pricing** — see step 4.

---

## What runs after this

| Service | Agent name | Health port | Config | Disposition endpoint |
|---|---|---|---|---|
| `smarthealth-bot` | `smarthealth-bot` | 8081 | `/opt/livekit/bot/.env` | `/disposition/smarthealth` |
| `roof-bot` | `roof-bot` | 8083 | `/opt/livekit/roof-bot/.env` | `/disposition/roof` |
| `finance-bot` | `finance-bot` | 8084 | `/opt/livekit/finance-bot/.env` | `/disposition/finance` |
| `voicebot-web` | `voicebot-web` | 8085 | per bot, per call | — (demo calls never post) |
| **`movers-bot`** | **`movers-bot`** | **8086** | **`/opt/livekit/movers-bot/.env`** | **`/disposition/movers`** |

Three things keep them apart and all three matter: distinct `agent_name` (the dispatch
rule targets it), distinct health port (two workers on one port crash-loops the second),
and a separate `.env` (one file cannot hold four `DISPOSITION_WEBHOOK_URL`s).

---

## 1. Capacity — read this before anything else

You are putting a **fifth** VAD-holding worker on a 4 vCPU box (four telephony + the
demo). Before adding it, check what the existing ones are set to:

```bash
grep -n num_idle_processes /opt/livekit/bot/agent_smarthealth_claude_dg.py \
  /opt/livekit/roof-bot/agent_roof_uk.py /opt/livekit/finance-bot/agent_finance_au.py
```

`movers-bot` starts at `num_idle_processes=1`. If Smart Health is still above 2, take it
down to 2 and restart it. After the first few concurrent calls, check for the warning
that means the box is starved:

```bash
journalctl -u movers-bot -u smarthealth-bot -u roof-bot -u finance-bot \
  --since "1 hour ago" | grep -i "slower than realtime"
```

Anything there means add cores or move a worker to its own host — not tune around it.
A removalist call is the longest script here (up to 480s), so two of them overlapping is
the load case to watch.

## 2. Files

From your workstation:

```powershell
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp agent_movers_au.py movers.env.example movers-bot.service `
    trunk-movers.json dispatch-movers.json MOVERS_AU_DEPLOY.md root@<DROPLET_IP>:/root/
scp dispositions/schema_movers.sql dispositions/receiver.py root@<DROPLET_IP>:/root/
scp agent_voicebot_web.py webdemo/demo_bots.py webdemo/setup.sh `
    webdemo/voicebot-web.service webdemo/WEB_DEMO.md root@<DROPLET_IP>:/root/
```

On the server:

```bash
mkdir -p /opt/livekit/movers-bot
cd /root
cp agent_movers_au.py MOVERS_AU_DEPLOY.md /opt/livekit/movers-bot/
cp movers.env.example /opt/livekit/movers-bot/.env
cp movers-bot.service /etc/systemd/system/
systemctl daemon-reload
```

No new venv and no pip install — it reuses `/opt/livekit/bot/.venv`.

## 3. The new disposition table

The `voicebot` database already exists, so run the **migration**, not the full
`schema.sql` (which would re-declare the other tables and the user):

```bash
mysql -u root -p < /root/schema_movers.sql
mysql -u root -p -e "USE voicebot; SHOW TABLES;"
# fin_call_dispositions
# mov_call_dispositions      <- new
# roof_call_dispositions
# sh_call_dispositions
```

Then the receiver, which now has a `/disposition/movers` endpoint:

```bash
cp /root/receiver.py /opt/disposition-receiver/
systemctl restart disposition-receiver
curl -s localhost:8095/health          # {"ok":true,"db":"up"}
```

**Test the endpoint before a bot ever posts to it** — once with a quote, once without:

```bash
curl -s -X POST localhost:8095/disposition/movers \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-MOV-1","campaign":"movers-au","phone":"+61412345678",
  "prospect_name":"Alex Taylor",
  "started_at":"2026-09-28T09:11:07.220400+00:00",
  "ended_at":"2026-09-28T09:17:02.884100+00:00",
  "duration_sec":355,"outcome":"BOOKING_PENDING_DEPOSIT",
  "outcome_note":"Booked - awaiting deposit",
  "consent_given":true,"booking_confirmed":true,"callback_request":"",
  "decline_reason":"","remarks":"test row",
  "qualification":{"details_confirmed":"yes","date_confirmed":"yes",
    "pickup_area":"Parramatta","dropoff_area":"Bondi Junction","move_date":"Saturday 11 October",
    "property_size":"two-bedroom unit","items":"furniture, boxes and a fridge",
    "furniture":"a queen bed, a sofa, a dining table and two wardrobes",
    "heavy_items":"none","fragile_items":"the TV and some glassware",
    "access":"second floor with a lift at pickup, ground floor at the new place",
    "parking":"driveway at both ends","notes":"",
    "arrival_window":"7 to 8 am","full_name":"Alex Taylor",
    "contact_number":"+61412345678","email":"alex@example.com.au",
    "email_spoken":"alex at example dot com dot au",
    "pickup_address":"12 Church Street, Parramatta",
    "delivery_address":"5 Oxford Street, Bondi Junction"},
  "quote":{"bedrooms":2,"movers":2,"truck_tonnes":6.0,"rate":150.0,"min_hours":3.0,
    "callout":80.0,"deposit":100.0,"estimate_min":530.0},
  "missing_required":[],"card_numbers_blocked":0}'
# {"status":"ok","id":1}

curl -s -X POST localhost:8095/disposition/movers \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-MOV-2","campaign":"movers-au","phone":"+61412345679",
  "started_at":"2026-09-28T09:30:00.000000+00:00",
  "ended_at":"2026-09-28T09:31:12.000000+00:00",
  "duration_sec":72,"outcome":"NOT_MOVING","outcome_note":"No longer moving",
  "consent_given":false,"booking_confirmed":false,"callback_request":"",
  "decline_reason":"","remarks":"pulled out of the purchase",
  "qualification":{"details_confirmed":"yes","date_confirmed":"no",
    "pickup_area":"Parramatta","dropoff_area":"Bondi Junction","move_date":"","property_size":"",
    "items":"","furniture":"","heavy_items":"","fragile_items":"","access":"",
    "parking":"","notes":"","arrival_window":"","full_name":"","contact_number":"",
    "email":"","email_spoken":"","pickup_address":"","delivery_address":""},
  "quote":{},"missing_required":["move_date","property_size","arrival_window",
    "full_name","contact_number","pickup_address","delivery_address"],
  "card_numbers_blocked":1}'
# {"status":"ok","id":2}
```

Check the two rows. The second must have **NULL in every `quote_*` column**, not zeros —
"they were never quoted" and "they were quoted nothing" are different facts, and the
office will report on the first:

```bash
mysql -u root -p -e "USE voicebot; SELECT id, outcome, quote_rate, quote_estimate_min,
  qual_email, card_numbers_blocked FROM mov_call_dispositions;"
```

`card_numbers_blocked = 1` on the second row is the guard working, not an error: it
counts the times the caller started reading out a card or account number and the agent
stripped it before the model saw it.

Finally, delete the test rows:

```bash
mysql -u root -p -e "USE voicebot; DELETE FROM mov_call_dispositions WHERE call_id LIKE 'TEST-MOV-%';"
```

## 4. Configure — the rate card is the part to get right

```bash
nano /opt/livekit/movers-bot/.env
chmod 600 /opt/livekit/movers-bot/.env
```

Copy the shared keys across rather than retyping them:

```bash
grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|DEEPGRAM_API_KEY|ANTHROPIC_API_KEY)=' \
  /opt/livekit/bot/.env
```

Then, in order of what will embarrass you soonest:

1. **`MOVE_RATES`, `MIN_HOURS`, `CALLOUT_FEE`, `DEPOSIT`** — placeholder pricing ships
   in this file. `MOVE_RATES` is `bedrooms:movers:truck tonnes:hourly rate`, one tier per
   comma, each applying from that many bedrooms up. Every figure the bot speaks is
   computed from these in code; the model is forbidden from inventing, rounding or
   discounting any of them.
2. **`RATES_INCLUDE_GST`** — 1 makes the bot say "150 dollars an hour, including GST",
   0 makes it say "plus GST". Match how the client quotes customers.
3. **`DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/movers`** — this is the
   whole reason the file is separate. Getting it wrong posts removalist bookings into
   another campaign's CRM.
4. **`ARRIVAL_WINDOWS`** — the only windows the bot will ever offer. It cannot invent one.
5. **`TTS_VOICE`** — `aura-2-hyperion-en` is the Australian masculine voice and the
   default here (`theia` is the feminine one). ⚠️ Aura ids *are* the voice, and a wrong
   one returns 400 on **every** turn, which on a call is indistinguishable from a mute
   bot. Verify the id against Deepgram's current voice list before the first live call.
6. **`COMPANY_NAME` / `AGENT_NAME`** — both spoken in the opening line.

## 5. Smoke test before wiring any SIP

```bash
cd /opt/livekit/movers-bot
/opt/livekit/bot/.venv/bin/python agent_movers_au.py download-files   # VAD (cached; harmless)
/opt/livekit/bot/.venv/bin/python agent_movers_au.py start
```

Expect `registered worker id=AW_...`, `health-check server on 127.0.0.1:8086`,
`STT(deepgram): nova-3 language=en-AU` and `TTS(deepgram): aura-2-hyperion-en`. A line
saying `MOVE_RATES=... is unusable` means the rate card failed to parse and the built-in
demo rates are in use — fix it now, not after a call. Ctrl-C, then:

```bash
systemctl enable --now movers-bot
systemctl is-active smarthealth-bot roof-bot finance-bot movers-bot voicebot-web  # five × active
ss -lntp | grep -E '808[1-6]'                                                    # distinct ports
```

## 6. SIP routing — the part that can break a live campaign

LiveKit picks a trunk by the **called number**, so each campaign needs its own trunk and
rule. Look at what exists first:

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=APIKEY LIVEKIT_API_SECRET=<secret>
lk sip inbound list       # note IDs, and which trunks have `numbers`
lk sip dispatch list      # note trunk_ids, roomPrefix, room_config.agents
```

Create the movers trunk **with** an explicit number so it is the more specific match
against the old wildcard trunk:

```bash
nano trunk-movers.json    # numbers: the DID or request-URI user part for this campaign
                          # allowed_addresses: the same FreeSWITCH IP as the others
lk sip inbound create trunk-movers.json          # -> ST_xxxx

nano dispatch-movers.json # paste ST_xxxx into trunk_ids
lk sip dispatch create dispatch-movers.json      # -> SDR_xxxx
lk sip dispatch list
```

FreeSWITCH then selects the campaign by what it dials:

```xml
<!-- movers campaign extension -->
<action application="bridge" data="sofia/gateway/livekit/MOVERS_NUMBER"/>
```

> **Verify this by test call, don't trust it.** Specific-over-wildcard trunk matching is
> documented behaviour, but if a movers call reaches another worker the fix is to pin
> `numbers` on that campaign's trunk too — which means recreating it and updating its
> dispatch rule's `trunk_ids`. Do that in a maintenance window, not on live traffic.

## 7. What the dialer should send

This campaign follows up an **online inquiry**, so three headers carry that inquiry. All
are optional and the call still works without them — the bot asks for the route and the
date instead of confirming them, which is a longer, less impressive call.

| Header | Attribute | Why |
|---|---|---|
| `X-Pickup` | `dialer.pickup` | Opening turn: "you're moving from Parramatta to Bondi Junction — is that correct?" |
| `X-Dropoff` | `dialer.dropoff` | Same turn |
| `X-Move-Date` | `dialer.move_date` | "Are you still planning to move on Saturday 11 October?" |
| `X-Phone` | `dialer.phone` | **Matters.** "Just use this number" is the usual answer to the contact-number question; without it the bot must ask them to read the digits out |
| `X-Prospect-Name` | `dialer.prospect_name` | Used in the greeting: "may I speak with Alex?" |
| `X-Call-Id` | `dialer.call_id` | Ties the disposition back to your dialer |
| `X-Campaign` | `dialer.campaign` | Lands in the disposition; defaults to `movers-au` |

If `X-Phone` is missing, LiveKit's own `sip.phoneNumber` is the fallback. The Smart
Health dialer has been sending `phone='s'`, which is rejected as junk — fix that at the
dialer rather than loosening the check.

## 8. The browser demo — same file, one copy

The demo loads this bot from `/opt/livekit/movers-bot/agent_movers_au.py`, exactly as it
does for the other phone bots, so the demo and the phone can never drift apart.

```bash
cp /root/agent_voicebot_web.py /opt/livekit/web-demo/
cp /root/demo_bots.py /opt/smarthealth-webdemo/
cp /root/WEB_DEMO.md /opt/smarthealth-webdemo/
cp /root/voicebot-web.service /etc/systemd/system/ && systemctl daemon-reload

# The old Australian file is no longer loaded by anything. Move it aside rather than
# deleting it — it is the only copy left, and step 10 rolls back with it.
mv /opt/livekit/web-demo/bots/agent_movers_au.py /root/agent_movers_au.py.bak 2>/dev/null || true
# Customised the demo's movers.env? Those settings belong in /opt/livekit/movers-bot/.env
# now — keep a copy before it goes.
mv /opt/livekit/web-demo/bots/movers.env /root/movers.env.demo-backup 2>/dev/null || true

systemctl restart voicebot-web          # finishes any demo call in progress first
systemctl restart smarthealth-webdemo
journalctl -u voicebot-web -n 40 --no-pager | grep -i movers
curl -s localhost:8090/healthz          # "bots" still lists every one
```

Take one demo call on the page as the moving-services bot: it should speak Australian
English and quote in dollars. Demo calls do **not** post to the CRM —
`voicebot-web` blanks the webhook — so nothing you do on the page reaches
`mov_call_dispositions`. Details: `WEB_DEMO.md`.

## 9. Prove nothing broke

```bash
# watch all five at once — the identifier tells you which took the call
journalctl -u smarthealth-bot -u roof-bot -u finance-bot -u movers-bot \
  -u disposition-receiver -f -o short-iso --no-hostname
```

1. Place a **Smart Health** call → room `call-*`, handled by `smarthealth-bot`.
2. Place a **roof** call → room `roof-*`, handled by `roof-bot`.
3. Place a **finance** call → room `fin-*`, handled by `finance-bot`.
4. Place a **movers** call → room `mov-*`, handled by `movers-bot`. With a name in
   `X-Prospect-Name` the greeting is *"Hello, may I speak with Alex?"*; without one it
   goes straight to *"…following up an online inquiry about a moving service"*. It ends
   with a POST to `/disposition/movers`.
5. Confirm the CSVs stay separate — `/opt/livekit/movers-bot/disposition_movers.csv` is
   this campaign's, and it is written even when the webhook fails.
6. Counts by campaign:

```bash
curl -s localhost:8095/stats
# {"smarthealth":{...}, "roof":{...}, "finance":{...}, "movers":{"BOOKING_PENDING_DEPOSIT":1}}
```

## 10. Rollback

```bash
systemctl disable --now movers-bot
lk sip dispatch delete SDR_xxxx
lk sip inbound delete ST_xxxx
```

The table and the receiver endpoint can stay — an unused table costs nothing and keeping
it means the rollback is not a migration.

To put the **browser demo** back on the old Australian bot as well, restore
`/root/agent_movers_au.py.bak` to `/opt/livekit/web-demo/bots/agent_movers_au.py`, put back
the previous `agent_voicebot_web.py` and `demo_bots.py`, and restart `voicebot-web` and
`smarthealth-webdemo`. That file is no longer in the repo — the server copy from step 8 is
the only one, which is why step 8 moves it instead of deleting it.

---

## Things worth knowing about this campaign

**Every price is computed in code.** `get_quote()` returns the crew, the truck, the
hourly rate, the minimum hours, the call-out fee, the deposit and the estimated minimum
total, and the prompt forbids stating any figure a tool did not return — no rounding, no
"roughly", no discounts. An LLM doing arithmetic aloud is how a call quotes $530 and the
confirmation says $590. Anything the rate card does not cover (packing, materials,
storage, insurance, a piano surcharge) gets "I'll have the office confirm that in
writing", never a number.

**Arrival windows are a list, not a judgement.** `check_availability()` returns
`ARRIVAL_WINDOWS` verbatim and the bot may offer nothing else. When the list is empty it
offers a callback instead of inventing a slot.

**Card and account numbers never reach the LLM.** Any run of 12+ digits — written or
spoken as words — is stripped from the caller's turn *before* the model sees it, and
`save_details()` refuses to store one. The deposit is always paid after the call, from
the confirmation. `card_numbers_blocked` in the disposition counts how often that fired.

**The booking is confirmed in two phases.** The first `confirm_booking()` returns the
summary to read back; only a second call, after the customer says it is all correct,
finalises it. Correct a detail in between and the read-back is invalidated, so the
customer always hears the final version.

**"Agreed to book but never finished" is its own outcome.** `end_call` is blocked twice
while consent is given and the booking is incomplete; after that the call ends as
`BOOKING_INCOMPLETE` with the missing fields in `remarks`, rather than trapping the
customer in a loop.

**Required for a booking:** date, size of move, arrival window, name, contact number,
pickup address, delivery address. Email is deliberately **not** required — the
confirmation goes by text when there is no usable address, and the bot stops asking after
two unclear attempts.

**Settings to check with the client:** GST handling (`RATES_INCLUDE_GST`), whether their
crews quote a call-out fee at all, the arrival windows, the billing increment
(`BILLING_INCREMENT`, default 15 minutes), how far ahead the crew rings (`CALL_AHEAD`,
default 30 minutes), and how the deposit is paid (`PAYMENT_METHODS`, default bank
transfer, BPAY or PayID).
