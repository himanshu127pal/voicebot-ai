# Browser demo — every voicebot, one page

A web page where a verified visitor picks one of our voicebots, plays the customer, and
talks to it from their browser. The page shows the script to follow and the details the
bot captures, live, as they speak.

**The telephony path is not touched.** If everything here fails, SIP calls still work.

---

## 1. How it fits together

```
  Browser ──HTTPS──▶ Caddy :443 ──▶ demo server :8090      signup, token mint, page
     │                  │
     │                  └────────▶ livekit :7880           WS signalling  (EXISTING)
     │
     └── WebRTC audio ─────────────▶ this host, UDP 50000-60000 / TCP 7881
                                          │
                                     worker "voicebot-web"  ── runs the chosen bot's
                                                               production file per call
```

| Bot | telephony | browser demo |
|---|---|---|
| Smart Health | `smarthealth-bot` (8081) | all seven through **`voicebot-web`** (8085) |
| Roof UK | `roof-bot` (8083) | |
| Finance AU | `finance-bot` (8084) | |
| Movers AU | `movers-bot` (8086) | |
| Appliance cover UK | `warranty-bot` (8087) | |
| Mortgage broker leads | — demo only | |
| Electricity assistance | — demo only | |
| dispatched by | SIP dispatch rules | `/api/token` with `{"bot": "<key>"}` |
| room names | `call-*` / `roof-*` / `fin-*` / `mov-*` / `war-*` | `demo-<bot>-*` |

**One worker, not one per bot.** Every worker keeps an idle process holding its own copy
of the voice-activity model. `voicebot-web` replaces the old `smarthealth-web` one for
one, so adding Roof and Finance to the demo costs no extra capacity.

**It runs the real bots.** For each call the worker loads that bot's production file —
`/opt/livekit/roof-bot/agent_roof_uk.py`, and so on — with that bot's own `.env`. Same
prompt, tools, gates and voice as a phone call. Change a script once and the phone and
the demo both get it on their next restart.

**Two bots are demo-only.** Mortgage broker leads and electricity assistance have no
SIP trunk and no service of their own — their agent files sit in `/opt/livekit/web-demo/bots/`
and the same worker loads them. Each is still a complete agent, so giving one its own
phone line later is the roof-bot runbook, not a rewrite. Moving services and appliance
cover both started this way and now have one: `MOVERS_AU_DEPLOY.md`,
`WARRANTY_UK_DEPLOY.md`.

Every bot reads settings like `COMPANY_NAME`, `TTS_VOICE` and `STT_LANGUAGE` from
its own `.env` file, and those settings conflict. The worker rebuilds the environment
for the chosen bot on every call, so the roof bot never picks up Smart Health's
Australian voice. `agent_voicebot_web.py` explains how.

### What differs from a phone call

| | Phone | Demo | Why |
|---|---|---|---|
| Machine guard | on | **off** | nobody is dialled, so it can only misfire and end a demo mid-sentence |
| Disposition webhook | posts to the lead tables | **not posted** | demo calls would pollute the lead tables — and on the finance bot, a visitor's real email would get a Quick Funding information pack |
| Disposition CSV | bot's own | `/opt/livekit/web-demo/demo-dispositions/<bot>.csv` | still there for debugging |
| Callback number | "shall I use the number you're on?" | **reads your verified number back** and asks you to confirm it | nobody on the page is on a call, so the phone wording confuses visitors. `WEB_DEMO=1` switches it |

To post demo calls to the webhooks anyway, set `DEMO_POST_DISPOSITIONS=1` in
`voicebot-web.service`. Every demo row is tagged `campaign = web-demo-<bot>` and
`call_id = web-*`.

---

## 2. Prerequisite you cannot skip: a hostname

**Browsers refuse microphone access on anything but HTTPS or localhost.** You need a DNS
A record pointing at this server:

```
demo.btbiz.in.   A   <this server's public IP>
```

Verify before continuing: `dig +short demo.btbiz.in`

---

## 3. Fresh install

Copy the `webdemo/` folder and `agent_voicebot_web.py` to the server, then:

```bash
cd /root/webdemo
bash setup.sh
```

That creates `/opt/smarthealth-webdemo` with its own venv, installs the worker to
`/opt/livekit/web-demo`, installs both units, and **stops without starting anything**.

### 3a. Configure

```bash
nano /opt/smarthealth-webdemo/.env
```

Every variable is documented in `.env.example`. The ones you must set:

```bash
LIVEKIT_API_KEY=APIKEY                  # exactly as in /opt/livekit/livekit.yaml
LIVEKIT_API_SECRET=<same secret>
PUBLIC_LIVEKIT_URL=wss://demo.btbiz.in  # no path; the SDK appends /rtc
OTP_API_KEY=<wapanel key>
DEMO_SESSION_SECRET=<openssl rand -hex 32>
DEMO_AGENT_NAME=voicebot-web
DEMO_BOTS=smarthealth,roof,finance
```

### 3b. Caddy (TLS)

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# Install Caddy FIRST. Copying the Caddyfile before `apt install caddy` lets the
# package drop its own default over the top of it.
cp -f Caddyfile /etc/caddy/Caddyfile
sed -i 's/demo\.yourdomain\.com/demo.btbiz.in/' /etc/caddy/Caddyfile
grep -c yourdomain /etc/caddy/Caddyfile      # must be 0

# Caddy runs as the 'caddy' user. A root-owned log dir makes it exit 1 at startup
# with "permission denied" — which reads like a config error but isn't.
mkdir -p /var/log/caddy && chown -R caddy:caddy /var/log/caddy

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl restart caddy
journalctl -u caddy -n 20 --no-pager         # want: certificate obtained successfully
```

### 3c. Firewall

WebRTC audio does **not** pass through Caddy — it goes straight to this host. Miss this
and you get a call that connects with total silence.

```bash
ufw status
```

**If `active`:**

```bash
ufw allow 80,443/tcp          # Caddy + the ACME challenge
ufw allow 7881/tcp            # ICE-TCP fallback — matters on locked-down guest wifi
ufw allow 50000:60000/udp     # the actual audio
```

**If `inactive`** — do NOT enable it. Turning ufw on starts filtering with an empty
ruleset and drops SIP from FreeSWITCH mid-campaign. Add the same three rules to the
DigitalOcean cloud firewall instead, if one is attached.

### 3d. Start

```bash
systemctl enable --now voicebot-web smarthealth-webdemo
journalctl -u voicebot-web -n 30 --no-pager
# expect: "bot smarthealth ready", "bot roof ready", "bot finance ready",
#         "health-check server on 127.0.0.1:8085", "registered worker"
```

---

## 4. Upgrading from the single-bot demo (`smarthealth-web`)

The new worker listens on 8085, so it can run **next to** the old one while you switch.
Visitors never hit a gap, and rolling back is one setting.

### 4a. Back up what you're about to replace (server)

```bash
mkdir -p /root/webdemo-backup
cp -a /opt/smarthealth-webdemo/server.py /opt/smarthealth-webdemo/.env \
      /opt/smarthealth-webdemo/static /root/webdemo-backup/
/opt/smarthealth-webdemo/.venv/bin/python -c "
import sqlite3
sqlite3.connect('/opt/smarthealth-webdemo/demo_signups.db').backup(
    sqlite3.connect('/root/webdemo-backup/demo_signups.db'))"
```

### 4b. Copy the files (PowerShell, on your PC)

```powershell
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp agent_voicebot_web.py webdemo\voicebot-web.service webdemo\server.py webdemo\demo_bots.py webdemo\countries.py webdemo\WEB_DEMO.md root@<DROPLET_IP>:/root/
scp webdemo\static\index.html webdemo\static\styles.css webdemo\static\app.js root@<DROPLET_IP>:/root/
```

### 4c. Install and start the new worker (server)

```bash
mkdir -p /opt/livekit/web-demo
cp /root/agent_voicebot_web.py /opt/livekit/web-demo/
cp /root/voicebot-web.service /etc/systemd/system/
systemctl daemon-reload

# The worker runs each bot from where its telephony service runs it. Confirm all three:
ls -l /opt/livekit/bot/agent_smarthealth_claude_dg.py \
      /opt/livekit/roof-bot/agent_roof_uk.py \
      /opt/livekit/finance-bot/agent_finance_au.py

systemctl enable --now voicebot-web
journalctl -u voicebot-web -n 30 --no-pager
```

Look for `bot … ready` for all three and `registered worker`. **Don't continue until
you see `registered worker`.** Without it the page would start calls that no agent ever
joins.

### 4d. Switch the page over (server)

```bash
cp /root/server.py /root/demo_bots.py /root/countries.py /root/WEB_DEMO.md /opt/smarthealth-webdemo/
cp /root/index.html /root/styles.css /root/app.js /opt/smarthealth-webdemo/static/

sed -i 's/^DEMO_AGENT_NAME=.*/DEMO_AGENT_NAME=voicebot-web/' /opt/smarthealth-webdemo/.env
grep -q '^DEMO_BOTS=' /opt/smarthealth-webdemo/.env || \
  echo 'DEMO_BOTS=smarthealth,roof,finance' >> /opt/smarthealth-webdemo/.env
grep -E '^(DEMO_AGENT_NAME|DEMO_BOTS)=' /opt/smarthealth-webdemo/.env

systemctl restart smarthealth-webdemo
curl -s localhost:8090/healthz
# {"ok":true,"agent":"voicebot-web","bots":["smarthealth","roof","finance"],...}
```

The restart also adds two columns to `demo_signups.db` (`usecase`, `last_bot`).
Existing signups are kept, and verified visitors stay signed in.

### 4e. Test, then retire the old worker

Hard-refresh `https://demo.btbiz.in`. The browser console should say `app.js build 12`
(`14` once the demo-only bots in §4f are in).
Take one call per bot:

- Smart Health: Sarah, Australian voice.
- Roof: Emma, **British** voice.
- Finance: Sophie, Australian voice.

Watch the right-hand panel fill in as you answer.

```bash
journalctl -u voicebot-web -f | grep -E "room=|could not|error"
# each call logs: room=demo-roof-… bot=roof agent='Emma' company='Building Regulatory Services'
```

When all three work:

```bash
systemctl disable --now smarthealth-web
systemctl is-active smarthealth-bot roof-bot finance-bot movers-bot warranty-bot   # five × active
```

### 4f. The demo-only bots

Already on `voicebot-web`? This is the whole update. No new service, no new port.

```powershell
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp agent_voicebot_web.py webbots\agent_mortgage_leads_au.py webbots\agent_energy_assist_au.py root@<DROPLET_IP>:/root/
scp webdemo\server.py webdemo\demo_bots.py webdemo\WEB_DEMO.md webdemo\static\app.js webdemo\static\styles.css root@<DROPLET_IP>:/root/
```

```bash
mkdir -p /opt/livekit/web-demo/bots
cp /root/agent_mortgage_leads_au.py /root/agent_energy_assist_au.py \
   /opt/livekit/web-demo/bots/
cp /root/agent_voicebot_web.py /opt/livekit/web-demo/
systemctl restart voicebot-web          # waits for any demo call in progress to finish
journalctl -u voicebot-web -n 40 --no-pager | grep "bot "    # one line per bot, all "ready"

cp /root/server.py /root/demo_bots.py /root/countries.py /root/WEB_DEMO.md /opt/smarthealth-webdemo/
cp /root/app.js /root/styles.css /opt/smarthealth-webdemo/static/
sed -i 's/^DEMO_BOTS=.*/DEMO_BOTS=smarthealth,roof,finance,movers,mortgage,energy,warranty/' \
  /opt/smarthealth-webdemo/.env
systemctl restart smarthealth-webdemo
curl -s localhost:8090/healthz          # "bots" lists every one
```

Hard-refresh the page; the console should say `app.js build 15`.

### 4g. The moving-services bot moved

It used to be demo-only. It now has its own phone line, so the worker loads it from
`/opt/livekit/movers-bot/agent_movers_au.py` like any other phone bot. Install it with
`MOVERS_AU_DEPLOY.md` — its step 8 is this side of the change — then the demo's copy is
redundant:

```bash
# Move the old Australian file aside rather than deleting it: it is the only copy left,
# and a rollback needs it.
mv /opt/livekit/web-demo/bots/agent_movers_au.py /root/agent_movers_au.py.bak 2>/dev/null || true
# Customised the demo's movers.env? Those settings belong in /opt/livekit/movers-bot/.env
# now. Keep a copy before it goes:
mv /opt/livekit/web-demo/bots/movers.env /root/movers.env.demo-backup 2>/dev/null || true
```

Until `/opt/livekit/movers-bot/` exists, picking that bot on the page fails the one call
with "that voicebot couldn't start" and the journal names the path it looked for. Every
other bot is unaffected. To hide it in the meantime, take `movers` out of `DEMO_BOTS`.

### 4h. The appliance-cover bot moved too

Same story, same fix: it now lives in `/opt/livekit/warranty-bot/agent_warranty_uk.py`.
Install it with `WARRANTY_UK_DEPLOY.md` — its step 8 is this side of the change — then:

```bash
mv /opt/livekit/web-demo/bots/agent_warranty_uk.py /root/agent_warranty_uk.py.bak 2>/dev/null || true
mv /opt/livekit/web-demo/bots/warranty.env /root/warranty.env.demo-backup 2>/dev/null || true
```

Until then, take `warranty` out of `DEMO_BOTS` or that one bot refuses its calls.

---

## 5. Verify — telephony first

```bash
systemctl is-active smarthealth-bot roof-bot finance-bot movers-bot warranty-bot   # must all be active
lk sip dispatch list                                       # unchanged
curl -s localhost:8090/healthz
```

**Place one real phone call afterwards** to confirm the SIP path is untouched.

---

## 6. What the page does

1. **Details.** The visitor gives:
   - their name
   - company
   - company email
   - business category (free text)
   - what they'd like a voicebot to do (free text)
   - WhatsApp number
2. **WhatsApp code.** They verify the number. The gate is enforced on `/api/token`, not just in the page.
3. **Pick a voicebot.** The dropdown lists the bots in `DEMO_BOTS`. The card under it says what the bot does, its market and voice, and **who the visitor will play** — for example "You run a café in Australia, after around $30,000". Under it, a **Live with our clients** note tells visitors these are real, working bots their clients use, and asks them to follow the script during the call. The wording is in `static/index.html` (`class="notice"`).
4. **The call.** Three columns on desktop, tabs on a phone:
   - **Your script.** Each step shows what the bot will ask and a reply that keeps the call on track. Steps tick off as the bot captures each answer, and the current step is highlighted. *Try these too* lists the moments worth showing a client, such as a do-not-call request, "are you a robot?", or a disqualifying answer.
   - **Centre.** The orb, driven by the agent's real audio. Under it are the agent's state, and Mute / End.
   - **Captured live.** Every field the bot records, filling in as it saves them, with an amber dot on the ones a handoff needs. It also shows the outcome and whether consent was given. Smart Health adds its monthly premium check.
5. **Call ended.** Shows the outcome and a summary of everything captured.

`?bot=roof` in the link preselects a bot. That's handy when you send a prospect straight
to the relevant one.

### The newest bots

| Bot | Introduces itself as | Voice | Worth showing a client |
|---|---|---|---|
| Moving Services Booking | Jack, EasyMove Removals | Australian, masculine | quotes from a rate card worked out in code, offers real arrival windows, books with a full read-back |
| Mortgage Broker Leads | Ethan, Keystone Lead Partners | Australian, masculine | a B2B call; prices any loan size in code; never discloses a client's details |
| Electricity Credit Assistance | Grace, ClearWatt Energy Assist | Australian, feminine | never asks for, keeps or even lets the AI see a card, bank or account number |
| Appliance Cover | Hannah, Brightwell Home Cover | British, feminine | takes no payment at all, refuses to sell cover for an appliance that's already faulty, and decides eligibility from the age in code |

Appliance cover is a phone bot now too (`WARRANTY_UK_DEPLOY.md`), so it reads its
telephony config, `/opt/livekit/warranty-bot/.env` — `PLAN_PRICE`, `PLAN_MONTHS`,
`PLAN_INCLUDES` and `MAX_APPLIANCE_YEARS` live there.

**The names, companies, prices and time windows are placeholders.** Mortgage and
electricity read an optional `/opt/livekit/web-demo/bots/<bot>.env` (`mortgage.env`,
`energy.env`); moving services and appliance cover read their telephony configs,
`/opt/livekit/movers-bot/.env` and `/opt/livekit/warranty-bot/.env`. The settings each one takes are listed at the top of its
agent file. To demo the movers bot with a prospect's own rate card:

```bash
nano /opt/livekit/movers-bot/.env
# COMPANY_NAME=Harbour Removals
# MOVE_RATES=1:2:4.5:140,2:2:6:165,3:3:8:210,4:4:10:260
# MIN_HOURS=3
# CALLOUT_FEE=90
# DEPOSIT=150
# RATES_INCLUDE_GST=1
# ARRIVAL_WINDOWS=7 to 8 am|1 to 2 pm
```

No restart needed for a demo call — every call reads the file fresh. **The phone worker
does not**: it reads its `.env` at import, so `systemctl restart movers-bot` after
editing, or the phone and the demo will quote different prices.

`MOVE_RATES` is `bedrooms:movers:truck tonnes:hourly rate`, one tier per comma.
`RATES_INCLUDE_GST=0` makes it say "plus GST". The mortgage bot's price is
`PRICE_PER_100K`. The appliance bot's plan is
`PLAN_PRICE`, `PLAN_MONTHS`, `PLAN_INCLUDES` (pipe-separated) and `MAX_APPLIANCE_YEARS`,
the age above which it stops quoting and refers the appliance for checking.

**The appliance bot never takes a payment**, and there is no tool it could use to take
one: the policy goes out in writing and payment happens from there. That is deliberately
stricter than the script it was built from, which left phone payment open — on an
outbound call to a householder, card details by voice are the scam signature. It also
refuses to sell cover for an appliance that already has a fault, and says so plainly
rather than quietly moving on.

**Card numbers never reach the AI.** On all three, anything that sounds like a card or
account number (12 or more digits, spoken or written) is removed from what the caller
said *before* the model sees it. The live panel counts each one as "Card numbers
blocked". The electricity bot also refuses to store any answer containing a number.

**The movers demo follows up an inquiry.** The page sends it a pretend online inquiry:
Parramatta to Bondi Junction, on a Saturday about two weeks out. The bot confirms these the way the
real script does, and the visitor's brief on the bot card says the same. Change them in
`_movers_attrs()` in `demo_bots.py`. On a real call these arrive as the SIP headers
`X-Pickup`, `X-Dropoff` and `X-Move-Date` — `MOVERS_AU_DEPLOY.md` step 7.

### Adding or removing a bot

- **Hide one:** remove it from `DEMO_BOTS` in `/opt/smarthealth-webdemo/.env`, then run `systemctl restart smarthealth-webdemo`.
- **A new bot:** needs three things.
  1. A `BOTS` entry in `agent_voicebot_web.py`: its file and `.env`. A demo-only bot's
     file goes in `/opt/livekit/web-demo/bots/`.
  2. A catalog entry in `demo_bots.py`: script, fields, outcomes.
  3. Its key in `DEMO_BOTS`.

The catalog's field and outcome keys must match the bot's real code. A key that doesn't
exist shows as a row that never fills in.

### When a production script changes

The bot changes automatically — the demo runs the same file. **The script shown on the
page does not**: it lives in `demo_bots.py`. If you reorder questions or rename a field,
update the matching entry there too.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Call connects, agent never speaks or joins | `voicebot-web` not registered, or `DEMO_AGENT_NAME` differs between the unit and `.env` | `journalctl -u voicebot-web -n 50`; both must be `voicebot-web` |
| "This voicebot couldn't start just now" | the bot's file failed to load | `journalctl -u voicebot-web \| grep -A20 "could not start bot"` — the traceback names the file and line |
| A demo-only bot "couldn't start" | its file isn't in `/opt/livekit/web-demo/bots/` | `ls /opt/livekit/web-demo/bots/`; the journal names the path it looked for |
| Moving services "couldn't start" | it is a phone bot now and lives in `/opt/livekit/movers-bot/` | install it with `MOVERS_AU_DEPLOY.md`, or drop `movers` from `DEMO_BOTS` until you do |
| Appliance cover "couldn't start" | same — it lives in `/opt/livekit/warranty-bot/` | install it with `WARRANTY_UK_DEPLOY.md`, or drop `warranty` from `DEMO_BOTS` |
| "That voicebot isn't available" | page offered a bot the worker doesn't know | `DEMO_BOTS` keys must exist in `agent_voicebot_web.py` |
| Roof bot has an Australian accent | worker running an old file, or `WEB_BOT_ROOF_ENV` points at the wrong `.env` | `journalctl -u voicebot-web \| grep "bot=roof"` shows the agent and company it loaded |
| Script panel shows the wrong agent name before the call | server can't read the bot's `.env` | `DEMO_BOT_ENV_<BOT>` in the server `.env`; the correct name appears once the call starts anyway |
| A script step never ticks | the bot heard it but didn't save that field | harmless — the tracker moves on when a later step is captured |
| Captured panel never updates, audio fine | telemetry failed | `journalctl -u voicebot-web \| grep -i "telemetry\|wire"`; the call itself is unaffected by design |
| `voicebot-web` crash-loops, `EADDRINUSE` | something else on 8085 | `ss -lntp \| grep 808`; change `DEMO_WORKER_HTTP_PORT` in the unit |
| Page still shows the old form | browser cached `app.js` | hard refresh; console must say `app.js build 15` |
| "This number is locked" for a real customer | 3 wrong codes, or 3 codes in 15 minutes | clear it in `demo_guard` — see **Attempts, locks and how many demos** |
| "Demo limit reached" on a first visit | the cap counts the NUMBER and the NETWORK, not the browser | someone else on that connection used theirs; raise `DEMO_MAX_CALLS_PER_IP` or clear `demo_call_log` |
| Verification refuses a valid number | `countries.py` missing on the server | `ls /opt/smarthealth-webdemo/countries.py`; the journal shows `No module named 'countries'` |
| Mic prompt never appears | page not on HTTPS | `journalctl -u caddy -n 50` |
| Connects, then silence both ways | UDP 50000-60000 closed | see §3c |
| Works on the server, not remotely | `PUBLIC_LIVEKIT_URL` still `ws://127.0.0.1` | set it to `wss://demo.btbiz.in` |

### If ICE never establishes

`livekit.yaml` has `use_external_ip: false`. On a DigitalOcean droplet the public IP sits
directly on `eth0`, so browsers connect fine. If your host is behind 1:1 NAT instead
(AWS/GCP style), browser media will fail while SIP keeps working. Only then set
`rtc.use_external_ip: true` — that restarts the LiveKit container, so do it in a
maintenance window and place a test phone call afterwards.

---

## 8. Cost and exposure

Every **Start** spawns a real agent: speech-to-text, the LLM and text-to-speech, billed as
a normal call. The guards:

- The signup gate: a verified WhatsApp number is required.
- `DEMO_MAX_CALLS_PER_SIGNUP` (default 5) per visitor.
- `DEMO_MAX_STARTS_PER_10MIN` (default 12) across everyone.
- Each bot's own `MAX_CALL_SECONDS`, so a forgotten tab can't run forever.
- The page is `noindex, nofollow`.

---

## 9. Rollback

Point the page back at the old worker and restore the old page and server. This needs
the backup from §4a.

```bash
systemctl enable --now smarthealth-web
cp -a /root/webdemo-backup/server.py /root/webdemo-backup/.env /opt/smarthealth-webdemo/
cp -a /root/webdemo-backup/static/. /opt/smarthealth-webdemo/static/
systemctl restart smarthealth-webdemo
systemctl disable --now voicebot-web
```

Keep the current `demo_signups.db`, so signups taken since the upgrade aren't lost. The
old server simply ignores the two new columns. Telephony was never involved.

---

## Signup gate

Nobody reaches an agent without submitting their details and verifying their WhatsApp
number. **The check is enforced on `/api/token`**, not just in the page.

```
details form ─→ POST /api/otp/send    we call wapanel, store a PENDING row bound to otpId
code         ─→ POST /api/otp/verify  wapanel verifies → row VERIFIED → signed httpOnly cookie
start        ─→ POST /api/token       refused without that cookie; capped per signup
```

- The OTP key lives only in `.env` on the server. The browser never sees it.
- The verified phone comes from **our** row for that `otpId`, never from the request.
- The chosen bot is validated **before** a call is reserved. A call is refunded if dispatch fails.

### Attempts, locks and how many demos

Every limit below is enforced in `server.py` and stored in `demo_signups.db`, **not** in
the page and **not** in memory — a restart must not hand anyone a clean slate. All of
them are settings in `/opt/smarthealth-webdemo/.env`.

| Limit | Default | Setting |
|---|---|---|
| Wrong codes before a number is locked | 3 | `OTP_MAX_ATTEMPTS` |
| How long it's locked | 15 min, then an hour | `OTP_LOCK_LADDER_MINUTES=15,60` |
| Ladder resets after | 24 h quiet | `OTP_STRIKE_RESET_HOURS` |
| Wrong codes forgotten after | 60 min | `OTP_FAIL_WINDOW_MINUTES` |
| Wrong codes from one network (any number) | 10 | `OTP_MAX_FAILS_PER_IP` |
| Wait between codes to one number | 60 s | `OTP_RESEND_COOLDOWN_SEC` |
| Codes per number, then it's locked | 3 per 15 min | `OTP_MAX_SENDS_PER_PHONE` / `OTP_SEND_WINDOW_MINUTES` |
| Demo calls per signup / number / network | 2 / 2 / 2 | `DEMO_MAX_CALLS_PER_SIGNUP` / `_PHONE` / `_IP` |
| Window for the number and network caps | 24 h (`0` = for ever) | `DEMO_CALL_WINDOW_HOURS` |

**The lock is on the number, not the code.** Asking for a new code does not reset it —
that was the hole worth closing, because the provider's own attempt counter resets with
every code it sends, so on its own it cannot stop someone grinding away at a number all
day. The escalation is deliberate: one bad day costs 15 minutes, a pattern costs an hour.

**The three demo caps are all enforced, and the page shows the smallest.** Per phone and
per network hold across signup rows, so filling the form in again with the same number
buys nothing. Every reserved call is a row in `demo_call_log` (refunded rows are
deleted), which is also the audit trail for "how many demos did that prospect take?".

Locked numbers are visible at a glance:

```bash
curl -s localhost:8090/healthz | python3 -m json.tool   # lockedNow, and every limit in force

sqlite3 /opt/smarthealth-webdemo/demo_signups.db \
  "SELECT subject, fails, strikes, locked_until FROM demo_guard
   WHERE locked_until > datetime('now') ORDER BY locked_until DESC;"
```

**To let a real customer back in early** — they mistyped three times, they're on the
phone to you, the demo is in ten minutes:

```bash
sqlite3 /opt/smarthealth-webdemo/demo_signups.db \
  "UPDATE demo_guard SET locked_until=NULL, fails=0 WHERE subject='phone:+919812345601';"
# Also clear the resend cooldown if they need a code immediately:
sqlite3 /opt/smarthealth-webdemo/demo_signups.db \
  "DELETE FROM demo_otp_sends WHERE phone='+919812345601';"
```

Leave `strikes` alone unless you mean to forgive the history too — it is what makes the
next lock an hour rather than 15 minutes. To give someone extra demo calls instead:

```bash
sqlite3 /opt/smarthealth-webdemo/demo_signups.db \
  "DELETE FROM demo_call_log WHERE phone='+919812345601';
   UPDATE demo_signups SET demo_calls=0 WHERE phone='+919812345601';"
```

No restart is needed for any of this: every check reads the database per request.

### The phone number a visitor types

`countries.py` holds every country's dialling code and the one normaliser both sides
use. The page's picker is searchable — "uk", "gb", "44" and "britain" all find the
United Kingdom — and the server re-derives the number from what was posted rather than
trusting it.

It fixes SHAPE, never digits: a leading trunk 0, `00` instead of `+`, the country code
typed as well as picked, brackets and dashes. It never invents or reorders digits, and
when the result isn't a plausible E.164 number the visitor gets the field back with a
reason instead of a code sent into the void. Numbers with no country at all are refused
rather than guessed — guessing is how a code reaches a stranger in another country.

### Where the leads are

`/opt/smarthealth-webdemo/demo_signups.db`. Export verified signups, including what they
want a voicebot for and which bot they last tried:

```bash
cd /opt/smarthealth-webdemo && ./.venv/bin/python -c "
import sqlite3, csv, sys
c = sqlite3.connect('demo_signups.db')
cur = c.execute('''SELECT id, verified_at, person_name, company_name, company_email, phone,
  business_category, usecase, demo_calls, last_bot, last_call_at FROM demo_signups
  WHERE status='verified' ORDER BY id DESC''')
w = csv.writer(sys.stdout); w.writerow([d[0] for d in cur.description]); w.writerows(cur)
" > signups.csv
```

`pending` rows are people who requested a code but never entered it. They showed intent,
so they're worth a look too. Signups from before the upgrade have an empty `usecase`.

### Company email only

The details form rejects personal and throwaway addresses before any WhatsApp code is
sent. The rule lives in `email_policy.py`, and `/api/otp/send` enforces it.

- **Free webmail:** Gmail, Yahoo, Outlook/Hotmail, iCloud, Proton, Rediffmail, including country variants
- **ISP mailboxes:** BigPond, BT, Sky, Comcast
- **Disposable inboxes:** Mailinator, 10MinuteMail, YOPmail
- **Placeholders:** `example.com`, `company.com`, `test.com`

If a genuine prospect is blocked, add their domain to `EMAIL_ALLOW_DOMAINS` in `.env`
(comma-separated) and restart. `EMAIL_BLOCK_DOMAINS` adds domains to the block list, and
`EMAIL_REQUIRE_COMPANY=0` turns the rule off.

> Before this upgrade, those three `EMAIL_*` settings in `.env` were **ignored**. The
> server loaded `.env` after the email rules had already been read, so the built-in
> defaults always applied. They take effect now.

Rejections are logged by domain only:

```bash
journalctl -u smarthealth-webdemo --since today | grep "non-company email"
```

### Kill switch

OTP provider down in the middle of a pitch? Set `DEMO_REQUIRE_SIGNUP=0` in `.env`, then
`systemctl restart smarthealth-webdemo`. The page skips signup and goes straight to the
bot picker.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Verification is temporarily unavailable" | `OTP_API_KEY` unset, or wapanel rejected it | `journalctl -u smarthealth-webdemo \| grep -i "api key"` — a `401` means the key is wrong or revoked |
| Everyone has to verify again after a restart | `DEMO_SESSION_SECRET` blank | set it once: `openssl rand -hex 32` |
| Verifies, but Start says the session expired | cookie not stored — page served over plain HTTP | serve through Caddy (HTTPS) |
| "Too many code requests from your network" for everyone | all visitors share one IP — Caddy bypassed | reach the site via Caddy, never port 8090 directly |
| "We're getting a lot of requests right now" | `OTP_MAX_SENDS_PER_HOUR` reached — possible abuse | check the log for repeated phones/IPs before raising it |
| A real prospect's work email is refused | domain matched a free-mail, ISP or placeholder rule | add it to `EMAIL_ALLOW_DOMAINS`, restart |
| Codes never arrive | number not on WhatsApp, or wapanel delivery failing | check wapanel's logs using the `demo_…` reference |
