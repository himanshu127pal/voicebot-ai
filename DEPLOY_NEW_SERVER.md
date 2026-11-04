# Deploy to a new server — from scratch

End state: LiveKit stack + the Naina agent on one new Linux box, FreeSWITCH
(existing) trunking calls to it. Incorporates the lessons from the first box
(CPU sizing, latency fixes).

Notation: **HostB** = this new server. **HostA** = FreeSWITCH (unchanged).

---

## 0. Size the box FIRST (this was the pain point last time)

Silero VAD went "slower than realtime" because server + sip + redis + agent all
shared too few cores. For a pilot handling a handful of concurrent calls:

- **Minimum:** 4 vCPU / 8 GB.
- **Comfortable for ~dozens:** 8 vCPU / 16 GB, and consider splitting the agent
  workers onto a second box later so media and inference don't contend.
- Plain CPU is fine (no GPU needed — STT/LLM/TTS are network APIs; only VAD is local).

## 1. OS prerequisites (HostB)

```bash
curl -fsSL https://get.docker.com | sh
docker compose version

curl -sSL https://get.livekit.io/cli | bash
lk --version

apt-get install -y python3-venv python3-pip
```

## 2. LiveKit stack (HostB)

Copy the stack files to `/etc/livekit/livekit-migration/`:
`docker-compose.yml`, `livekit.yaml`, `sip.yaml`, `trunk.json`, `dispatch.json`.

Reuse the SAME api key/secret as before (or generate new with `openssl rand -hex 32`
and update all three consumers). Edit:
- `livekit.yaml` → `keys: { mykey: <secret> }`
- `sip.yaml` → `api_key/api_secret` (match), `use_external_ip: true`
- `trunk.json` → `allowed_addresses: ["<HostA / FreeSWITCH IP>"]`

Start + verify:
```bash
cd /etc/livekit/livekit-migration
docker compose up -d
docker compose ps
curl http://127.0.0.1:7880        # responds
ss -lunp | grep 5060              # SIP listening
```

## 3. Firewall (HostB) — from FreeSWITCH only

```
UDP 5060            SIP signaling
UDP 10000-20000     RTP media       (open this or you get silent calls)
```

## 4. Trunk + dispatch rule (HostB)

```bash
export LIVEKIT_URL=ws://127.0.0.1:7880
export LIVEKIT_API_KEY=mykey
export LIVEKIT_API_SECRET=<secret>

lk sip inbound create trunk.json          # -> ST_xxxx
lk sip inbound list                       # AllowedAddresses = FreeSWITCH IP (NOT 127.0.0.1)

# put ST_xxxx into dispatch.json trunk_ids, then:
lk sip dispatch create dispatch.json      # includes room_config.agents=[naina-bot]
lk sip dispatch list
```

## 5. The agent (HostB)

```bash
mkdir -p /opt/naina-bot && cd /opt/naina-bot
python3 -m venv .venv
./.venv/bin/pip install "livekit-agents[groq,silero]" livekit-plugins-sarvam python-dotenv num2words
# num2words: spoken-English number conversion used by naina_agent_bilingual.py's tts_node

# copy naina_agent_sarvam.py here
```

`.env` in `/opt/naina-bot/`:
```bash
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=mykey
LIVEKIT_API_SECRET=<secret>
SARVAM_API_KEY=...
GROQ_API_KEY=...
BACKEND_URL=...
NAINA_LANG=hi-IN
OMP_NUM_THREADS=1          # keeps Silero VAD from thrashing threads
```

Download the VAD model, then smoke-test in dev:
```bash
./.venv/bin/python naina_agent_sarvam.py download-files
./.venv/bin/python naina_agent_sarvam.py dev      # should register "naina-bot" and idle
```

## 6. Run the agent as a service (HostB)

`/etc/systemd/system/naina-bot.service`:
```ini
[Unit]
Description=Naina LiveKit voice agent
After=network.target docker.service

[Service]
WorkingDirectory=/opt/naina-bot
ExecStart=/opt/naina-bot/.venv/bin/python naina_agent_sarvam.py start
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now naina-bot
journalctl -u naina-bot -f        # watch it register + take calls
```

## 7. FreeSWITCH (HostA) — point at the NEW server

Update the gateway proxy to HostB's IP, then rescan:
```xml
<param name="proxy" value="HOSTB_IP:5060"/>
```
```bash
fs_cli -x "sofia profile external rescan"
fs_cli -x "sofia status gateway livekit"
```
Dialplan (`dialplan-to-bot.xml`) unchanged — it still sets the `X-headers` and
bridges to `sofia/gateway/livekit/...`.

## 8. Test call — verify all three chains

Watch both at once:
```bash
journalctl -u naina-bot -f                          # agent
docker compose -f /etc/livekit/livekit-migration/docker-compose.yml logs -f livekit-sip
```
Bridge a call to the agent extension. Confirm:
1. INVITE from FreeSWITCH → room `call-xxxx` created
2. agent job dispatched, entrypoint runs
3. **prospect attributes are NOT all None** (means X-headers arrived — this was
   the open item; if still None, check the dialplan sets `sip_h_X-Prospect-Name` etc.
   and that you're calling the extension that has those lines)
4. bot greets in Hindi; two-way conversation
5. no "silero slower than realtime" spam (means the box has enough CPU)
6. on hangup, disposition line logs

## Latency checklist (carried over from the first box)

- LLM = Groq (fast). Do NOT use DeepSeek for live calls — too slow.
- STT language PINNED (`NAINA_LANG`), not "multi" — avoids wrong-language transcripts.
- `OMP_NUM_THREADS=1` set.
- Run `start` (not `dev`) in production.
- If Silero still warns "slower than realtime": the box is CPU-starved — add cores
  or move the agent to its own host, separate from the LiveKit media stack.
