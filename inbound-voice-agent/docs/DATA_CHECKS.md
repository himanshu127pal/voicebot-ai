# Data checks — phone numbers, postcodes, names, emails

How the agent makes sure the details it collects are real, and the services behind each
check. Everything in use today is **free**; paid upgrades are listed for when accuracy
needs to go further.

The rule of thumb: **the AI model talks, the code checks.** Each check lives inside a tool.
When something is wrong, the tool answers `NOT SAVED — …` and tells the model exactly what
to ask next, so the rule holds on every call even if the model forgets the script.

## Summary

| Check | How | Cost | Code |
|---|---|---|---|
| Mobile or landline? Which country? | `phonenumbers` library (Google libphonenumber), offline | Free | `phones.py` |
| Postcode real? Which area? | postcodes.io API | Free, no key | `postcodes.py` |
| Full name (first + surname) | Rule in code | Free | `validation.py` |
| Email address | Spoken → written, then format check | Free | `validation.py` |
| Speaking speed | Deepgram Aura `speed` setting | Included | `pipeline.py` |

## 1. Phone numbers — mobile or landline

**Service:** [`phonenumbers`](https://pypi.org/project/phonenumbers/), the Python port of
Google's [libphonenumber](https://github.com/google/libphonenumber). It's the library
Android uses. Apache 2.0 licence, runs offline with no API key, and the numbering plans
for every country are updated with each release. **Keep it updated** (`pip install -U
phonenumbers`) every few months.

**What it tells us:** whether the number is valid, its country, and its type.

| UK number starts with | Type | WhatsApp? |
|---|---|---|
| `07` (most of the range) | Mobile | Yes |
| `01`, `02` | Landline (geographic) | No |
| `03`, `08`, `09` | Business / freephone / premium | No |
| `070` personal numbers, `076` pagers, `056` VoIP | Other | No |
| Some `07` ranges, e.g. `07624` (Isle of Man), and Jersey/Guernsey mobiles | Mobile, but outside the UK; reported with that country | Yes |

You don't have to remember these. The library knows the exact ranges, including the
exceptions.

Carriers deliver caller ID in different shapes (`+447400123456`, `447400123456`,
`07400123456`, `0044…`). `check_phone()` handles all of them.

**How the agent uses it:**

1. **At the start of the call** the caller ID is checked (`worker.py`). The prompt then
   says e.g. *"The caller is ringing from a UK landline ending in 0000. WhatsApp does NOT
   work on it."*
2. **When they give a contact number**, `save_answer("contact_number", …)` checks it and
   saves the type in `answers.number_type` as well. The agent never asks "mobile or
   landline?".
3. **When WhatsApp is agreed**, `share_details("whatsapp", …)` refuses anything that isn't
   a mobile and tells the agent to ask for a mobile instead. The saved WhatsApp number is
   in international format (`+447400123456`), which is what WhatsApp APIs need.
4. **"Yes" / "same number" / "I already told you"** (no digits) means a number we already
   have: for the contact number, the caller ID; for WhatsApp, the contact number they
   confirmed earlier, or the caller ID if they say "the one I'm calling from". The caller
   is never asked to repeat it.

**A number is only saved if it's complete AND it's what the caller actually said.** The
model can "complete" a number: in the 2026-11-10 console test the caller said 9 digits
and the model saved 11 by adding `00`. So before saving, the tool checks the number is
valid (right length for its country) and that its digits appear in the caller's last few
turns (`matches_what_was_said`; spoken digits like "double four" count). If not, it
refuses, tells the agent which digits were actually heard, and the agent asks for the
rest.

**Accuracy and limits:**

- **UK, Australia, India, most of Europe:** reliable. Mobiles and landlines use separate
  number ranges, and a ported UK mobile keeps its `07` number.
- **US and Canada** (no campaign there yet): mobiles and landlines share the same
  ranges, so the result is `mobile_or_landline`. The agent allows WhatsApp there but
  can't be sure. To know for certain you need a paid carrier lookup (below).
- It can't tell whether a number **has WhatsApp installed**. No free official check
  exists. The backend should treat a failed WhatsApp send as a signal to fall back to
  email.
- It checks the number *range* is real. It doesn't check that the line is switched on;
  that needs a paid HLR lookup.

**Paid upgrades (only if needed):**

- [Twilio Lookup](https://www.twilio.com/docs/lookup/v2-api/line-type-intelligence)
  (Line Type Intelligence): real carrier and line type for US/CA. Pay per lookup, around
  a cent each.
- [Telnyx Number Lookup](https://telnyx.com/products/number-lookup): similar.
- [numverify](https://numverify.com/): small free tier, then paid.

## 2. UK postcodes — real postcode and its area

**Service:** [postcodes.io](https://postcodes.io), free and open source (MIT), with no API
key. It's built on the ONS Postcode Directory (official Office for National Statistics
data), which is refreshed every quarter.

| Endpoint | Used for |
|---|---|
| `GET https://api.postcodes.io/postcodes/SW1A1AA` | Postcode exists? → area, region, country |
| `GET https://api.postcodes.io/outcodes/SW1A` | Postcode not found → which area the first half is in |

Example result for `SW1A 1AA`: `admin_district: Westminster`, `region: London`,
`country: England`.

**Address already on file (we called them first):** the caller's number is looked up in
`LEADS_FILE` (`leads.py`; `config/test_leads.yaml` holds fake customers for console
tests). If there's a record, the agent first confirms the name (*"Am I speaking with
Daniel Whitmore?"*), then reads the address back (*"I have your address as 14 Hawthorn
Grove, Stretford, M 3 2, 0 J G. Is that still right?"*) and calls `confirm_address` on
a yes, without asking for the postcode. Nothing on file is read out until the name is
confirmed, since whoever answers might not be the customer. If the caller has moved, or
there's no record, the steps below apply.

**How the agent uses it:**

1. The agent asks **only for the postcode**, then calls `check_postcode` with what the
   caller said.
2. Code turns speech into a clean postcode before any lookup (`normalise_uk_postcode`):
   - `"s w one a one a a"` → `SW1A 1AA`
   - `"S for Sugar W one A, one double A"` → `SW1A 1AA`
   - `"sierra whiskey one alpha…"` → `SW1A…`
   - `"C R O 2 Y R"` → `CR0 2YR` (letter O heard for zero)
   - `"oh X four four…"` → `OX44…` (zero heard for letter O)

   The first letters must be one of the ~120 real UK postcode areas (`POSTCODE_AREAS`),
   so STT garbage is rejected without calling the API.
3. **Found:** the agent reads it back with the area: *"That's S W 1 A, 1 A A, in
   Westminster. Is that right?"*
4. **Not found:** the agent asks for it again letter by letter. If the first half is real,
   it can ask *"Is that in Westminster?"*. After 3 tries it takes what it has and moves
   on, so the caller isn't stuck.
5. Then it asks for the **first line** (house number and street) and saves it with
   `save_address` as said. The street is not read back or checked; only the postcode
   needs to be real (client decision, 2026-11-10).
6. **Channel Islands (JE, GY) and Isle of Man (IM)** are accepted like the UK (client
   decision, 2026-11-10). Any other country found by the lookup triggers the UK ONLY
   rule. To exclude them again, remove them from `UK_COUNTRIES` in `postcodes.py`.

**Fails soft:** if postcodes.io is slow (over 3 seconds) or down, the agent just reads the
postcode back and carries on. `POSTCODE_API_URL=off` in `.env` turns the lookup off.

**Report fields:** `postcode`, `postcode_verified` (found on postcodes.io),
`lead_found`, `address_on_file` (confirmed from the record), and `answers.address`
(first line, area, postcode).

**Accuracy and limits:**

- It confirms the **postcode and area**, which is all we need. It doesn't know street
  addresses, so the first line is saved as the caller said it.
- Postcodes are read back one character at a time with a pause between halves. Earlier
  TTS tests (`qa/tts-2026-11-09`) showed this is the format Deepgram reads most clearly.
- For high volume or strict uptime, postcodes.io can be self-hosted (Docker), then point
  `POSTCODE_API_URL` at it.

**Upgrades for street-level accuracy** (not needed for now):

| Service | What it adds | Cost |
|---|---|---|
| [getAddress.io](https://getaddress.io) | Every address at a postcode (Royal Mail PAF), so the agent can ask "Is it 10 Downing Street?" | Small free tier, then paid |
| [Ideal Postcodes](https://ideal-postcodes.co.uk) | Same, PAF-based | Pay per lookup (pence) |
| [Loqate](https://www.loqate.com) | Same, global | Paid |
| [OpenStreetMap Nominatim](https://nominatim.org) | Check a street exists near a postcode | Free, but max 1 request/second; self-host for production |

## 3. Other countries (for future campaigns)

The phone check already works for any country: set `country:` in the campaign YAML. The
postcode lookup is UK-only today. Free sources for other countries:

| Country | Phone type | Postcode / address data |
|---|---|---|
| Australia | Reliable (`04` = mobile) | [G-NAF](https://data.gov.au/dataset/geocoded-national-address-file-g-naf) (every address, free open data); Australia Post API (registration needed); GeoNames postcodes |
| USA | `mobile_or_landline`, so a paid lookup is needed to be sure | [Zippopotam.us](https://zippopotam.us) (ZIP → city/state, free); [US Census Geocoder](https://geocoding.geo.census.gov/) (address match, free); USPS Addresses API (free with registration) |
| Any | — | [GeoNames postal codes](https://download.geonames.org/export/zip/) (free download, many countries) |

## 4. Full name

`save_answer("full_name", …)` only accepts a first name **and** a surname
(`is_full_name`). Titles and lead-ins are ignored: `"Mr Smith"` and `"my name is
Robert"` are refused, `"Mr Robert Smith"` is accepted. On a refusal the agent asks *"And
your surname, please?"*. After 2 refusals it saves what it has, marked
`(surname not given)`, rather than annoying the caller.

## 5. Email

`share_details("email", …)`:

- is **refused if WhatsApp is already agreed**. Email is only the fallback.
- turns speech into an address (`"john dot smith at g mail dot com"` →
  `john.smith@gmail.com`) and checks the format. An unclear address is refused rather
  than guessed.
- reads the address back to the caller.
- after 3 unclear attempts, stops asking, notes what was heard, and carries on. It never
  offers post: WhatsApp first, email if not, nothing else (client decision, 2026-11-12).

## 6. Speaking speed

Deepgram Aura voices take a `speed` from 0.7 to 1.5 (1.0 = normal). It's set per campaign
in the YAML (`voice.tts_speed`). The UK appliance campaign uses `0.9` after the "talking
faster" feedback. Tune by ear: 0.85 if still fast, 0.95 if it now drags.

## Testing over SIP (caller ID → mobile / landline)

The bot reads the caller ID that FreeSWITCH passes on as `X-Caller` (from
`caller_id_number`; see `deploy/freeswitch/dialplan-inbound.xml`). To test, set the
**caller ID number** on your SIP account or softphone to one of these and call a DID
mapped to `appliance_cover_uk`:

| Caller ID to send | Detected as | What the bot should do |
|---|---|---|
| `+442079460123` | UK landline (London) | Never offers WhatsApp to it; asks for a UK mobile |
| `+441614960123` | UK landline (Manchester) | Same as above |
| `+447400123456` | UK mobile | Offers WhatsApp to "the number ending in 3456" |
| `+448001234567` | UK freephone (not a mobile) | Treated like a landline |
| `+919876543210` | Indian mobile | WhatsApp allowed |
| *(withheld / anonymous)* | Not valid | Asks for a mobile for WhatsApp |

- `020 7946 0xxx` and `0161 496 0xxx` are Ofcom's reserved "drama" numbers, so they never
  reach a real person. Ofcom's drama *mobile* range (`07700 900xxx`) is rejected as
  invalid by the library, so the mobile test number has to be a real-format one. **If
  you agree to WhatsApp on a test call with a made-up mobile, make sure the backend
  doesn't actually send the message**, or use your own UK mobile instead.
- If your carrier forces your real number as caller ID, the test numbers won't come
  through. Check the agent log line `call … caller=…0123 (landline)` to see what the bot
  received.

## Tests

`tests/test_phones.py`, `test_postcodes.py`, `test_validation.py` and `test_tools.py`
cover every rule above. The postcode tests run against a local fake of postcodes.io, so
they need no internet. Run them with:

```
.venv\Scripts\python -m pytest -q
```
