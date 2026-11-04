# Script Guide

How to turn a campaign script into a file the agent can use. No coding needed.

## Steps

1. Copy `config/campaigns/_template.yaml` to `config/campaigns/<campaign>.yaml`.
2. Fill in the fields below. Replace every `REPLACE` — anything left there is spoken aloud.
3. Save. The next call uses the new script (no restart).

## Fields

| Field | What to write | Example |
|---|---|---|
| `key` | Short unique id | `roof_uk` |
| `default` | `true` on exactly one campaign: used for unknown DIDs | `false` |
| `dids` | Every number customers call back on | `"+44 20 7946 0000"` |
| `company` | Name the agent introduces | `Building Regulatory Services` |
| `agent_name` | Agent's first name | `Emma` |
| `purpose` | What we called about — completes "We'd called you about …" | `a free roof assessment for your home` |
| `benefit` | One-line main benefit | `It's free and there's no obligation.` |
| `greeting` | *Optional.* Exact opening line | |
| `facts` | Everything the agent may say about the offer, one sentence each | `The check takes 20 minutes.` |
| `faq` | Common questions + approved answers | see below |
| `questions` | *Optional.* Details to collect from interested callers, in order | see below |
| `extra_instructions` | Any other rule from the script | `Never quote prices.` |
| `channels` | Offer details on WhatsApp / email? | `whatsapp: true` |
| `human_transfer_uri` | *Optional.* Where "talk to a person" goes | ask the tech team |
| `voice` | `sarvam` (Hindi/English) or `deepgram` (English) | see template |

```yaml
faq:
  - q: How did you get my number?
    a: We contact homeowners in your area about free assessments. I can remove your number right now if you prefer.
```

Qualification questions (asked one per turn; each answer lands in the disposition's
`answers` under its `field`). Full example: `config/campaigns/appliance_cover_uk.yaml`.

```yaml
questions:
  - field: machine_make
    ask: May I also confirm the make of your washing machine for our records?
  - field: address
    ask: Could I take your postcode and the first line of your address?
```

## What the agent already does (no need to script it)

- Says who we are and why we called in the first line.
- Offers WhatsApp/email **once** when someone doesn't want calls; otherwise opts them out
  and tells them nobody will call again about this unless they ask.
- Apologises and opts out straight away if the caller is annoyed or asks if this is spam.
- Offers a callback, or a transfer to a person.
- Checks in if the caller goes quiet; hangs up after 3 unanswered check-ins.
- Says yes if asked whether it's an AI; never takes card/bank details (card-length
  numbers are stripped before they reach the AI model).
- Speaks numbers correctly — write numbers as digits (`499`, `20 minutes`).

## Writing tips

- **Short.** Each fact and answer: one sentence. Phone conversations, not brochures.
- **Only true facts.** If it's not in `facts` / `faq`, the agent says a specialist will
  follow up. It will not make anything up — so anything callers often ask belongs here.
- **No brackets or placeholders** like `[Company]` — they get read out.
- **Never pressure.** The agent is built to accept the first "no"; don't add lines that
  push past it.
