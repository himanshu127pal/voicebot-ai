# Finance AU campaign — deploy runbook

Adds a fourth agent worker (`finance-bot`), a new disposition table, and a SIP route to
reach it. **The three campaigns already running must keep working throughout** — step 8
is the verification that proves they did.

---

## What runs after this

| Service | Agent name | Health port | Config | Disposition endpoint |
|---|---|---|---|---|
| `smarthealth-bot` | `smarthealth-bot` | 8081 | `/opt/livekit/bot/.env` | `/disposition/smarthealth` |
| `smarthealth-web` | `smarthealth-web` | 8082 | `/opt/livekit/bot/.env` | — |
| `roof-bot` | `roof-bot` | 8083 | `/opt/livekit/roof-bot/.env` | `/disposition/roof` |
| **`finance-bot`** | **`finance-bot`** | **8084** | **`/opt/livekit/finance-bot/.env`** | **`/disposition/finance`** |

Three things keep them apart and all three matter: distinct `agent_name` (dispatch
targets it), distinct health port (two workers on one port crash-loops the second), and
a separate `.env` (one file cannot hold four `DISPOSITION_WEBHOOK_URL`s).

---

## 1. Capacity — read this before anything else

This is the **fourth** VAD-holding worker on a 4 vCPU box, and it is the step most
likely to hurt. Silero VAD is CPU-bound and every *idle* process holds its own copy, so
the cost is paid whether or not calls are flowing.

Count what is already reserved:

```bash
nproc
grep -h num_idle_processes \
  /opt/livekit/bot/agent_smarthealth_claude_dg.py \
  /opt/livekit/bot/agent_smarthealth_web.py \
  /opt/livekit/roof-bot/agent_roof_uk.py
```

`finance-bot` adds 1. If the total is above roughly one per vCPU, take a process off
Smart Health first:

```bash
grep -n num_idle_processes /opt/livekit/bot/agent_smarthealth_claude_dg.py
# lower it, then:
systemctl restart smarthealth-bot
```

After the first few concurrent calls, check for the warning that means the box is
starved — it degrades **every** campaign, not just this one:

```bash
journalctl -u finance-bot -u roof-bot -u smarthealth-bot --since "1 hour ago" \
  | grep -i "slower than realtime"
```

Anything there means add cores or move a worker to its own host. Do not tune around it.

> If you would rather not risk the live campaigns, this bot runs perfectly well on a
> second small droplet pointed at the same LiveKit URL. Nothing in it assumes
> co-location except the loopback disposition URL, which becomes the private IP.

## 2. Files

From your workstation:

```powershell
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp agent_finance_au.py finance.env.example finance-bot.service `
    trunk-finance.json dispatch-finance.json FINANCE_AU_DEPLOY.md root@<DROPLET_IP>:/root/
scp dispositions/schema_finance.sql dispositions/receiver.py root@<DROPLET_IP>:/root/
```

On the server:

```bash
mkdir -p /opt/livekit/finance-bot
cd /root
cp agent_finance_au.py FINANCE_AU_DEPLOY.md /opt/livekit/finance-bot/
cp finance.env.example /opt/livekit/finance-bot/.env
cp finance-bot.service /etc/systemd/system/
systemctl daemon-reload
```

No new venv and no pip install — it reuses `/opt/livekit/bot/.venv`.

## 3. The new disposition table

The `voicebot` database already exists, so run the **migration**, not the full
`schema.sql` (which would re-declare the other two tables and the user):

```bash
mysql -u root -p < /root/schema_finance.sql

mysql -u root -p -e "USE voicebot; SHOW TABLES;"
# fin_call_dispositions      <- new
# roof_call_dispositions
# sh_call_dispositions
```

`GRANT ... ON voicebot.*` already covers the new table, so the `voicebot` user needs no
change. Confirm it anyway:

```bash
mysql -u voicebot -p voicebot -e "SELECT COUNT(*) FROM fin_call_dispositions;"   # 0
```

Then update the receiver with the third endpoint and restart it:

```bash
cp /root/receiver.py /opt/disposition-receiver/
systemctl restart disposition-receiver
curl -s localhost:8095/health          # {"ok":true,"db":"up"}
```

**Test the endpoint before a bot ever posts to it** — with an email, and without:

```bash
curl -s -X POST localhost:8095/disposition/finance \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-FIN-1","campaign":"finance-au","phone":"+61412345678",
  "prospect_name":null,
  "started_at":"2026-09-15T04:11:07.220400+00:00",
  "ended_at":"2026-09-15T04:15:02.884100+00:00",
  "duration_sec":236,"outcome":"INFO_REQUESTED",
  "outcome_note":"Information pack agreed - email captured",
  "consent_given":true,"info_pack_sent":true,"callback_request":"",
  "decline_reason":"","remarks":"test row",
  "qualification":{"looking_for_finance":"yes","loan_purpose":"business",
    "amount":"around thirty thousand","business_stage":"existing",
    "business_age":"about four years","monthly_turnover":"sixty to seventy thousand",
    "loan_use":"","employment_status":"","income_amount":"","income_frequency":"",
    "liabilities":"just a car loan","first_name":"Daniel","last_name":"",
    "email":"dan@brightcafe.com.au","email_spoken":"dan at bright cafe dot com dot au",
    "contact_number":"+61412345678"},
  "missing_required":[],
  "usage":{"stt_audio_duration":231.4,"llm_total_tokens":14208,"tts_characters_count":2871}}'
# {"status":"ok","id":1}

curl -s -X POST localhost:8095/disposition/finance \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-FIN-2","campaign":"finance-au","phone":"+61412345679",
  "started_at":"2026-09-15T04:20:00.000000+00:00",
  "ended_at":"2026-09-15T04:23:00.000000+00:00",
  "duration_sec":180,"outcome":"QUALIFIED_CALLBACK",
  "outcome_note":"Qualified - no usable email, adviser to call",
  "consent_given":true,"info_pack_sent":true,"callback_request":"",
  "decline_reason":"","remarks":"email unclear on the line",
  "qualification":{"looking_for_finance":"yes","loan_purpose":"personal",
    "amount":"twenty grand","business_stage":"","business_age":"","monthly_turnover":"",
    "loan_use":"consolidating a couple of cards","employment_status":"full-time",
    "income_amount":"about fourteen hundred","income_frequency":"fortnightly",
    "liabilities":"two credit cards","first_name":"Priya","last_name":"",
    "email":"","email_spoken":"p something at bigpond",
    "contact_number":"+61412345679"},
  "missing_required":[],"usage":{}}'
# {"status":"ok","id":2}
```

Check the two rows, especially that the missing email is **NULL and not an empty
string** — that distinction is what makes "who still needs emailing" a reliable query:

```bash
mysql -u root -p voicebot -e "
  SELECT id, call_id, outcome, qual_loan_purpose, qual_amount,
         qual_email, qual_email_spoken, started_at
  FROM fin_call_dispositions\G"

mysql -u root -p voicebot -e "
  SELECT COUNT(*) AS needs_emailing FROM fin_call_dispositions
  WHERE qual_email IS NULL AND consent_given = 1;"        # 1
```

`started_at` should read `2026-09-15 04:11:07.220400` — offset stripped, UTC preserved.
Then clear the test rows:

```bash
mysql -u root -p voicebot -e "
  DELETE FROM fin_call_dispositions WHERE call_id LIKE 'TEST-FIN-%';"
```

## 4. Configure

```bash
nano /opt/livekit/finance-bot/.env
chmod 600 /opt/livekit/finance-bot/.env
```

Copy the shared keys across rather than retyping them:

```bash
grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|DEEPGRAM_API_KEY|ANTHROPIC_API_KEY)=' \
  /opt/livekit/bot/.env
```

Then set, deliberately:

| Variable | Why it matters |
|---|---|
| `COMPANY_NAME` | Spoken in the opening line. Defaults to `Quick Funding` |
| `AGENT_NAME` | The voice introduces itself by this name |
| `COMPANY_WEBSITE` | Given **only** on request. Blank = "it's all in the email" — the bot will never invent an address |
| `CONTACT_NUMBER` | Same: given only on request |
| `DISPOSITION_WEBHOOK_URL` | Must be `/disposition/finance`. Pointing it at `/disposition/roof` posts loan leads into the roof table |

## 5. Smoke test before wiring any SIP

```bash
cd /opt/livekit/finance-bot
/opt/livekit/bot/.venv/bin/python agent_finance_au.py download-files   # VAD (cached; harmless)
/opt/livekit/bot/.venv/bin/python agent_finance_au.py start
```

Expect `registered worker id=AW_...`, `health-check server on 127.0.0.1:8084`,
`STT(deepgram): nova-3 language=en-AU` and `TTS(deepgram): aura-2-theia-en`. Ctrl-C,
then:

```bash
systemctl enable --now finance-bot
systemctl is-active smarthealth-bot smarthealth-web roof-bot finance-bot   # four × active
ss -lntp | grep -E '808[1234]'                                            # four distinct ports
```

## 6. SIP routing — the part that can break a live campaign

LiveKit picks a trunk by the **called number**, so each campaign needs its own trunk and
rule. Look at what exists first:

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=APIKEY LIVEKIT_API_SECRET=<secret>
lk sip inbound list       # note IDs and which trunks have `numbers`
lk sip dispatch list      # note trunk_ids, roomPrefix, room_config.agents
```

Create the finance trunk **with** an explicit number so it is the more specific match:

```bash
nano trunk-finance.json    # numbers: the DID or request-URI user part for this campaign
                           # allowed_addresses: the same FreeSWITCH IP
lk sip inbound create trunk-finance.json         # -> ST_xxxx

nano dispatch-finance.json # paste ST_xxxx into trunk_ids
lk sip dispatch create dispatch-finance.json     # -> SDR_xxxx
lk sip dispatch list
```

FreeSWITCH then selects the campaign by what it dials:

```xml
<!-- finance campaign extension -->
<action application="bridge" data="sofia/gateway/livekit/FINANCE_NUMBER"/>
```

> **Verify by test call, don't trust it.** If the original Smart Health trunk is still a
> wildcard (no `numbers`), specific-over-wildcard matching is what keeps these apart.
> That is documented behaviour, but place one call per campaign and confirm each lands
> on the right worker. **If a finance call reaches another bot**, pin `numbers` on that
> trunk too — which means recreating it and updating its dispatch rule's `trunk_ids`.
> Do that in a maintenance window, not on live traffic.

## 7. What the dialer should send

All optional, all read from SIP `X-` headers (see `trunk-finance.json`):

| Header | Attribute | Why |
|---|---|---|
| `X-Call-Id` | `dialer.call_id` | Ties the disposition back to your dialer |
| `X-Phone` | `dialer.phone` | **Matters here.** "Just call me on this number" is the most common answer to the contact-number question; without it the bot has to ask them to read the digits out |
| `X-Prospect-Name` | `dialer.prospect_name` | Not used in the greeting, only for reference |
| `X-Campaign` | `dialer.campaign` | Lands in the disposition |
| `X-State` | `dialer.state` | Reference only |

If `X-Phone` is missing, LiveKit's own `sip.phoneNumber` is used as a fallback. The
Smart Health dialer has been sending `phone='s'`, which is rejected as junk — fix it at
the dialer rather than loosening the check.

## 8. Prove nothing broke

```bash
# watch all four at once — the identifier tells you which took the call
journalctl -u smarthealth-bot -u roof-bot -u finance-bot -u disposition-receiver \
  -f -o short-iso --no-hostname
```

1. Place a **Smart Health** call → room `call-*`, `smarthealth-bot`, disposition POST.
2. Place a **roof** call → room `roof-*`, `roof-bot`, its own POST.
3. Place a **finance** call → room `fin-*`, `finance-bot`, opening is
   *"Hi there, my name's Sophie, I'm calling from Quick Funding…"*, ends with a POST to
   `/disposition/finance`.
4. Confirm the CSVs stay separate — `/opt/livekit/finance-bot/disposition_finance.csv`
   should be the only one that grew for call 3.

```bash
curl -s localhost:8095/stats
# {"smarthealth":{...}, "roof":{...}, "finance":{"INFO_REQUESTED":1}}
```

## 9. Rollback

```bash
systemctl disable --now finance-bot
lk sip dispatch delete SDR_xxxx
lk sip inbound delete ST_xxxx
```

The table can stay — an unused table costs nothing and keeps any leads already captured.
The only change made outside this campaign is `receiver.py`, which is additive: the
smarthealth and roof endpoints are untouched.

---

## Things worth knowing about this campaign

**The opening is the script's own AI-agent version, not section 1.** The written
section 1 opens with a bare question — *"am I speaking with the person who may be
looking for finance?"* — which is the exact thing that sounded wrong on the roof bot.
The script's own "Short AI Voice Agent Version" already introduces the agent and the
brokerage first, so that is what the bot says.

**Spoken email addresses are handled in code, not by the model.** `normalise_email()`
turns *"dan at bright cafe dot com dot au"* into `dan@brightcafe.com.au`, repairs the
provider names Deepgram splits (`g mail`, `hot mail`, `out look`), and **returns nothing
at all** when the result is not a valid address. An almost-right address is worse than
none: the information pack silently goes nowhere while the lead looks complete. A
stutter like *"john at at gmail dot com"* would normalise to the plausible-but-wrong
`john@atgmail.com`, so a leftover space inside the domain rejects the whole thing.

**Email capture is bounded at two attempts.** After the second failure the bot stops
asking, says an adviser will call instead, and the call closes as `QUALIFIED_CALLBACK`.
That outcome is a *real, fully qualified lead* — it just cannot be emailed. **It will
never show up in an "emails sent" report, so route it deliberately or it disappears.**
`qual_email_spoken` holds the raw words and is usually enough for a person to work out
the address.

**Every rate, fee, repayment and timeframe is forbidden.** Not a number, not a range,
not a "from". The script's section 11 answer is the only response to a rate question.
The bot also cannot say anyone is approved, eligible or likely to qualify.

**The bot never asks for anything sensitive.** No PIN, password, bank login, card, BSB,
account number, tax file number, licence or date of birth, and no request to send
documents on the call — documents come later, through the adviser, after they have the
company information. A cold call asking for bank statements is indistinguishable from a
scam, and the script itself (sections 8 and 13) leans on "review our information first".
If a caller volunteers any of it, the bot declines it.

**The "soft check" line is approved copy, spoken verbatim and never expanded.** The
claim that a preapproval *"does not appear as a standard loan enquiry on your credit
profile"* comes straight from section 6 of your script. The bot says it exactly once, in
those words, and is forbidden from elaborating, guaranteeing or discussing credit scores
further. **It is a factual claim about credit reporting** — worth your compliance team
confirming it matches what your lender panel actually does, since the bot will now say
it on every call that asks.

**The not-looking gate is in code, not the prompt.** `save_qualification` returns a
refusal string the model cannot ignore once `looking_for_finance` is no — the same
pattern as the roof homeowner gate and the Smart Health premium gate, both of which the
model *would* talk itself out of when the caller sounded friendly.

**Required for a handoff: loan purpose, amount, first name, contact number.** Turnover,
income and liabilities are deliberately *not* required — the script accepts vague
answers to all three and moves on, and requiring one would make the bot press for
something the script gives away.

**`ASK_INCOME_ON_PERSONAL=1` is an addition to the written script.** Section 3 asks
about employment and income only when someone wants to start or buy a business. A pure
personal-loan lead with no income information is not workable by a broker, so by default
the bot asks the same two questions on the personal path — optional, never required,
dropped the moment they hesitate. Set it to `0` to follow the script to the letter.

**The script never asks for a name; the bot does.** One extra question in the close —
*"Can I grab your first name?"* — because an adviser cannot send an information email to
an unnamed lead, and the CRM row would be unworkable without it.
