# Call disposition webhook — integration spec

What the five voice bots POST at the end of every call, and the MySQL tables to
receive it. Written for the developer building the endpoint.

| | |
|---|---|
| Campaigns | 5 (Smart Health AU, Roof UK, Finance AU, Movers AU, Warranty UK) |
| Tables | 5 |
| Method | `POST`, `application/json` |
| Source | LiveKit voice agents |

---

## Are the JSON keys fixed?

**Yes — the shape never changes.** Every payload is built from a fixed dictionary in
code, and the nested `qualification` object comes from a Python dataclass, so *every
field is present on every call*.

A question the caller never answered arrives as an empty string `""`, **never as a
missing key**. Only the *values* vary with the conversation.

The one exception is `missing_required` on the Roof, Finance, Movers and Warranty
campaigns, which is a variable-length array. Movers and Warranty add a second: their
`quote` object is `{}` when the call ended before a price was worked out (§6, §7).

---

## 1. Transport & delivery

| Property | Value |
|---|---|
| Method | `POST` |
| Content-Type | `application/json` |
| Auth | `Authorization: Bearer <token>` — sent only if a token is configured on the bot |
| Timeout | **10 seconds**, total |
| Retries | **None.** One attempt per call |
| Frequency | Exactly once, when the call ends |
| Expected response | Any 2xx. The body is logged but not parsed |

> ⚠️ **There is no retry.** If the endpoint is slow or down, that lead is not re-sent —
> it survives only in a local CSV on the bot server. Insert fast and return 200
> immediately; do any heavy processing asynchronously. A 10-second insert is a lost lead.

Each bot posts to its own URL, configured separately. They can be three routes on one
service or three services — the bots don't care.

---

## 2. Before you write the DDL

Five things that will bite otherwise.

**1. `call_id` is not guaranteed unique.** It comes from the dialer's `X-Call-Id` SIP
header, and in live traffic we have seen the constant value `CALL-123456789` on every
call. **Do not make it the primary key or add a UNIQUE index** — inserts would start
failing. Use an `AUTO_INCREMENT` surrogate key and a plain index on `call_id`.

**2. Timestamps are ISO-8601 with a UTC offset** —
`2026-09-01T05:51:38.060361+00:00`. MySQL `DATETIME` has no timezone, so strip the
offset and store UTC, or use `VARCHAR(35)` to keep the original string. Note the
microseconds: `DATETIME(6)` keeps them, `DATETIME` truncates.

**3. Qualification values are free text, exactly as the caller said them.**
`premium_amount` arrives as `"about three hundred and forty dollars"` as often as
`"$340"`. `roof_age` may be `"no idea"`. **Every qualification column must be VARCHAR** —
never numeric. Normalisation is a downstream job, deliberately not done on a live call.

**4. Several top-level fields can be JSON `null`** — `call_id`, `campaign`, `phone` and
the prospect name all come from optional SIP headers. Columns must be `NULL`-able. Note
this differs from qualification fields, which are `""` rather than null.

**5. Keep the raw payload.** Both tables below include a `raw_payload JSON` column. Store
the whole body verbatim alongside the parsed columns — when a field is added or a value
looks wrong six weeks from now, that column is the difference between an answer and a
shrug.

---

## 3. Campaign A — Smart Health (AU health insurance)

Source: `agent_smarthealth_claude_dg.py`
Qualify a private-health-insurance holder and hand them to an advisor.

### Sample payload

```json
{
  "call_id": "CALL-123456789",
  "agent": "smarthealth-bot",
  "campaign": "Smart-healt-insurance-2026",
  "prospect": {
    "name": "Basant",
    "phone": "+61400123456",
    "state": null,
    "postcode": "3121"
  },
  "outcome": "QUALIFIED_PENDING",
  "disposition_code": "Connected - Qualified, advisor to call",
  "qualification": {
    "holds_policy": "yes",
    "over_min_age": "yes",
    "insurer": "BUPA",
    "cover_type": "family",
    "premium_amount": "about three hundred and forty dollars",
    "premium_frequency": "monthly",
    "hospital_extras": "both",
    "permanent_resident": "",
    "main_interest": "reduce premiums",
    "first_name": "Alex",
    "last_name": "",
    "email": "",
    "postcode": "3121",
    "decision_maker": ""
  },
  "consent_to_contact": true,
  "callback_request": "",
  "remarks": "Family cover with BUPA, wants to reduce premiums.",
  "decline_reason": "",
  "started_at": "2026-09-01T05:51:38.060361+00:00",
  "ended_at": "2026-09-01T05:55:12.884210+00:00",
  "duration_seconds": 214.8,
  "usage": {
    "stt_audio_duration": 198.44,
    "llm_total_tokens": 18734,
    "tts_characters_count": 2891
  }
}
```

Note `"permanent_resident": ""` — never asked, so empty string, not null.

### Field reference

| JSON path | Type | Notes |
|---|---|---|
| `call_id` | string\|null | From the dialer. Not unique — see gotcha 1 |
| `agent` | string | Always `"smarthealth-bot"` |
| `campaign` | string\|null | Dialer campaign name |
| `prospect.name` | string\|null | Raw dialer value, unvalidated |
| `prospect.phone` | string\|null | May contain junk (`"s"` has been seen) |
| `prospect.state` | string\|null | AU state code |
| `prospect.postcode` | string\|null | Captured value, falling back to the dialer's |
| `outcome` | string | Machine enum — see §8 |
| `disposition_code` | string | Human label, many-to-one with outcome |
| `qualification.*` | string | 14 keys, always present, free text |
| `consent_to_contact` | boolean | Explicit yes to advisor contact. **Your consent artifact — keep it** |
| `callback_request` | string | Free text, e.g. `"Tuesday 4pm"` |
| `remarks` | string | One line for the advisor |
| `decline_reason` | string | Populated on `NOT_INTERESTED` |
| `started_at` / `ended_at` | string | ISO-8601 UTC with microseconds |
| `duration_seconds` | float | One decimal place |
| `usage.*` | number | 3 keys — per-call STT/LLM/TTS cost drivers |

### Table

```sql
CREATE TABLE sh_call_dispositions (
  id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                 VARCHAR(64)  NULL,
  agent                   VARCHAR(32)  NULL,
  campaign                VARCHAR(64)  NULL,

  prospect_name           VARCHAR(120) NULL,
  prospect_phone          VARCHAR(32)  NULL,
  prospect_state          VARCHAR(8)   NULL,
  prospect_postcode       VARCHAR(12)  NULL,

  outcome                 VARCHAR(32)  NOT NULL,
  disposition_code        VARCHAR(64)  NULL,
  consent_to_contact      TINYINT(1)   NOT NULL DEFAULT 0,
  callback_request        VARCHAR(120) NULL,
  remarks                 TEXT         NULL,
  decline_reason          TEXT         NULL,

  started_at              DATETIME(6)  NULL,   -- store UTC; offset stripped
  ended_at                DATETIME(6)  NULL,
  duration_seconds        DECIMAL(8,1) NULL,

  -- qualification.* — ALL free text, never numeric
  qual_holds_policy       VARCHAR(32)  NULL,
  qual_over_min_age       VARCHAR(32)  NULL,
  qual_insurer            VARCHAR(64)  NULL,
  qual_cover_type         VARCHAR(32)  NULL,
  qual_premium_amount     VARCHAR(120) NULL,
  qual_premium_frequency  VARCHAR(32)  NULL,
  qual_hospital_extras    VARCHAR(32)  NULL,
  qual_permanent_resident VARCHAR(32)  NULL,
  qual_main_interest      VARCHAR(64)  NULL,
  qual_first_name         VARCHAR(80)  NULL,
  qual_last_name          VARCHAR(80)  NULL,
  qual_email              VARCHAR(160) NULL,
  qual_postcode           VARCHAR(12)  NULL,
  qual_decision_maker     VARCHAR(32)  NULL,

  usage_stt_seconds       DECIMAL(9,2) NULL,
  usage_llm_tokens        INT UNSIGNED NULL,
  usage_tts_characters    INT UNSIGNED NULL,

  raw_payload             JSON NULL,

  PRIMARY KEY (id),
  KEY idx_call_id (call_id),
  KEY idx_phone   (prospect_phone),
  KEY idx_outcome (outcome),
  KEY idx_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**Why `utf8mb4`:** callers say things the transcriber renders with accented characters
and the occasional symbol. MySQL's `utf8` is only 3 bytes and will reject them
mid-insert.

---

## 4. Campaign B — Roof UK (free assessment)

Source: `agent_roof_uk.py`
Confirm a homeowner and book an adviser callback. **Different shape to Campaign A** —
see §9.

### Sample payload

```json
{
  "call_id": "CALL-987654321",
  "campaign": "roof-uk",
  "phone": "+447700900123",
  "prospect_name": null,
  "started_at": "2026-09-02T10:14:02.113900+00:00",
  "ended_at": "2026-09-02T10:16:48.441200+00:00",
  "duration_sec": 166,
  "outcome": "CALLBACK_AGREED",
  "outcome_note": "Adviser callback agreed - details captured",
  "consent_given": true,
  "callback_agreed": true,
  "callback_request": "",
  "decline_reason": "",
  "remarks": "Roof approx 20 years, never checked.",
  "qualification": {
    "homeowner": "yes",
    "interested": "yes",
    "roof_age": "around 20 years I think",
    "checked_recently": "no",
    "first_name": "Margaret",
    "last_name": "",
    "address": "14 Elm Road, Bristol BS7 8QX",
    "callback_number": "+447700900123"
  },
  "missing_required": []
}
```

### Field reference

| JSON path | Type | Notes |
|---|---|---|
| `call_id` | string\|null | Not unique — see gotcha 1 |
| `campaign` | string | Falls back to `"roof-uk"` |
| `phone` | string\|null | Top level here, unlike Campaign A |
| `prospect_name` | string\|null | Raw dialer value, unvalidated |
| `started_at` / `ended_at` | string | ISO-8601 UTC with microseconds |
| `duration_sec` | integer | Whole seconds — **not** a float |
| `outcome` | string | Machine enum — see §8 |
| `outcome_note` | string | Human label |
| `consent_given` | boolean | Explicit yes to a callback. **Consent artifact** |
| `callback_agreed` | boolean | Handoff completed with all details |
| `callback_request` | string | Set when they asked for a specific time |
| `decline_reason` | string | Populated on `NOT_INTERESTED` |
| `remarks` | string | One line for the adviser |
| `qualification.*` | string | 8 keys, always present, free text |
| `qualification.address` | string | **Personal data** — full property address |
| `missing_required` | array\<string\> | Subset of `first_name`, `address`, `callback_number`. Non-empty on `CALLBACK_INCOMPLETE` |

### Table

```sql
CREATE TABLE roof_call_dispositions (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id               VARCHAR(64)  NULL,
  campaign              VARCHAR(64)  NULL,
  phone                 VARCHAR(32)  NULL,
  prospect_name         VARCHAR(120) NULL,

  started_at            DATETIME(6)  NULL,   -- store UTC; offset stripped
  ended_at              DATETIME(6)  NULL,
  duration_sec          INT UNSIGNED NULL,

  outcome               VARCHAR(32)  NOT NULL,
  outcome_note          VARCHAR(80)  NULL,
  consent_given         TINYINT(1)   NOT NULL DEFAULT 0,
  callback_agreed       TINYINT(1)   NOT NULL DEFAULT 0,
  callback_request      VARCHAR(120) NULL,
  decline_reason        TEXT         NULL,
  remarks               TEXT         NULL,

  -- qualification.* — ALL free text, never numeric
  qual_homeowner        VARCHAR(32)  NULL,
  qual_interested       VARCHAR(32)  NULL,
  qual_roof_age         VARCHAR(120) NULL,
  qual_checked_recently VARCHAR(64)  NULL,
  qual_first_name       VARCHAR(80)  NULL,
  qual_last_name        VARCHAR(80)  NULL,
  qual_address          VARCHAR(255) NULL,
  qual_callback_number  VARCHAR(32)  NULL,

  missing_required      JSON NULL,
  raw_payload           JSON NULL,

  PRIMARY KEY (id),
  KEY idx_call_id (call_id),
  KEY idx_phone   (phone),
  KEY idx_outcome (outcome),
  KEY idx_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

---

## 5. Campaign C — Finance AU (personal & business loans)

Source: `agent_finance_au.py`
Qualify a loan enquiry and send an information email.

**Shape follows Campaign B deliberately** — same key names, same types — so there is one
fewer inconsistency to absorb (§9). It adds two things: a `usage` object like Campaign A,
and a **pair** of email fields.

### The two email fields — read this before you use either

The whole close of this campaign is an information email, so the address is the payload's
most valuable field and its least reliable one. It is therefore delivered twice:

| Field | What it is | Use it for |
|---|---|---|
| `qualification.email` | **Validated and normalised**, or `""`. Never a guess | Sending the email |
| `qualification.email_spoken` | The **raw transcript** of what the caller said | Repairing a lead by hand |

The bot normalises spoken forms in code — `"dan at bright cafe dot com dot au"` becomes
`dan@brightcafe.com.au` — and reads the result back to the caller to confirm. **If it
cannot produce a valid address, `email` is empty and the call ends as
`QUALIFIED_CALLBACK` instead of `INFO_REQUESTED`** — a real, qualified lead that has to
be worked by phone. `email_spoken` still holds what they said, which is usually enough
for a person to work out the address the transcriber could not.

> **Never send mail to `email_spoken`.** It is not an address.

### Sample payload

```json
{
  "call_id": "CALL-456789123",
  "campaign": "finance-au",
  "phone": "+61412345678",
  "prospect_name": null,
  "started_at": "2026-09-15T04:11:07.220400+00:00",
  "ended_at": "2026-09-15T04:15:02.884100+00:00",
  "duration_sec": 236,
  "outcome": "INFO_REQUESTED",
  "outcome_note": "Information pack agreed - email captured",
  "consent_given": true,
  "info_pack_sent": true,
  "callback_request": "",
  "decline_reason": "",
  "remarks": "Existing cafe, wants 30k for a fit-out. Has a car loan.",
  "qualification": {
    "looking_for_finance": "yes",
    "loan_purpose": "business",
    "amount": "around thirty thousand",
    "business_stage": "existing",
    "business_age": "about four years",
    "monthly_turnover": "sixty to seventy thousand a month",
    "loan_use": "",
    "employment_status": "",
    "income_amount": "",
    "income_frequency": "",
    "liabilities": "just a car loan and one credit card",
    "first_name": "Daniel",
    "last_name": "",
    "email": "dan@brightcafe.com.au",
    "email_spoken": "dan at bright cafe dot com dot au",
    "contact_number": "+61412345678"
  },
  "missing_required": [],
  "usage": {
    "stt_audio_duration": 231.4,
    "llm_total_tokens": 14208,
    "tts_characters_count": 2871
  }
}
```

The personal path fills `loan_use`, `employment_status`, `income_amount` and
`income_frequency` instead of the three `business_*` fields. **Both sets of keys are
always present**; the ones the branch didn't use are `""`.

### Field reference

| JSON path | Type | Notes |
|---|---|---|
| `call_id` | string\|null | Not unique — see gotcha 1 |
| `campaign` | string | Falls back to `"finance-au"` |
| `phone` | string\|null | The number dialled |
| `prospect_name` | string\|null | Raw dialer value, unvalidated |
| `started_at` / `ended_at` | string | ISO-8601 UTC with microseconds |
| `duration_sec` | integer | Whole seconds — **not** a float |
| `outcome` | string | Machine enum — see §8 |
| `outcome_note` | string | Human label |
| `consent_given` | boolean | Explicit yes to being sent the information. **Consent artifact** |
| `info_pack_sent` | boolean | The handoff completed with all required details |
| `callback_request` | string | Set when they asked for a specific time |
| `decline_reason` | string | Populated on `NOT_INTERESTED` |
| `remarks` | string | One line for the adviser |
| `qualification.*` | string | 16 keys, always present, free text |
| `qualification.loan_purpose` | string | `"personal"` or `"business"` — the branch |
| `qualification.amount` | string | **As spoken**: `"around thirty grand"` is normal. Never parsed |
| `qualification.email` | string | Validated, or `""`. **Safe to send to** |
| `qualification.email_spoken` | string | Raw transcript. **Not an address** |
| `qualification.income_amount` | string | After tax, as spoken. **Personal data** |
| `missing_required` | array\<string\> | Subset of `loan_purpose`, `amount`, `first_name`, `contact_number`. Non-empty on `INFO_INCOMPLETE` |
| `usage.*` | number | Cost drivers for this call. May be `{}` if metrics were unavailable |

### Table

```sql
CREATE TABLE fin_call_dispositions (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                VARCHAR(64)  NULL,
  campaign               VARCHAR(64)  NULL,
  phone                  VARCHAR(32)  NULL,
  prospect_name          VARCHAR(120) NULL,

  started_at             DATETIME(6)  NULL,   -- store UTC; offset stripped
  ended_at               DATETIME(6)  NULL,
  duration_sec           INT UNSIGNED NULL,

  outcome                VARCHAR(32)  NOT NULL,
  outcome_note           VARCHAR(80)  NULL,
  consent_given          TINYINT(1)   NOT NULL DEFAULT 0,
  info_pack_sent         TINYINT(1)   NOT NULL DEFAULT 0,
  callback_request       VARCHAR(120) NULL,
  decline_reason         TEXT         NULL,
  remarks                TEXT         NULL,

  -- qualification.* — ALL free text, never numeric
  qual_looking_for_finance VARCHAR(32)  NULL,
  qual_loan_purpose        VARCHAR(32)  NULL,   -- personal | business
  qual_amount              VARCHAR(120) NULL,
  qual_business_stage      VARCHAR(32)  NULL,   -- existing | start | purchase
  qual_business_age        VARCHAR(120) NULL,
  qual_monthly_turnover    VARCHAR(120) NULL,
  qual_loan_use            VARCHAR(255) NULL,
  qual_employment_status   VARCHAR(64)  NULL,
  qual_income_amount       VARCHAR(120) NULL,
  qual_income_frequency    VARCHAR(32)  NULL,
  qual_liabilities         TEXT         NULL,
  qual_first_name          VARCHAR(80)  NULL,
  qual_last_name           VARCHAR(80)  NULL,
  qual_email               VARCHAR(160) NULL,   -- validated, or NULL. Never a guess
  qual_email_spoken        VARCHAR(255) NULL,   -- raw transcript. NOT an address
  qual_contact_number      VARCHAR(32)  NULL,

  usage_stt_seconds      DECIMAL(9,2) NULL,
  usage_llm_tokens       INT UNSIGNED NULL,
  usage_tts_characters   INT UNSIGNED NULL,

  missing_required       JSON NULL,
  raw_payload            JSON NULL,

  PRIMARY KEY (id),
  KEY idx_call_id (call_id),
  KEY idx_phone   (phone),
  KEY idx_outcome (outcome),
  KEY idx_started (started_at),
  KEY idx_email   (qual_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

The receiver stores an empty `email` as `NULL`, not `""`, so
`WHERE qual_email IS NOT NULL` means exactly "we have an address to send to".

---

## 6. Campaign D — Movers AU (removalist booking)

Source: `agent_movers_au.py`
Follow up an online moving inquiry, quote it from the rate card, and book the move.

**Shape follows Campaigns B and C** — same key names, same types. Two differences matter:

- **There is no `usage` object.** Nothing to map; don't wait for one.
- **There is a `quote` object, and it is the only numeric part of any payload.** Every
  other value in every campaign is what a human said, in their words. `quote` is what the
  bot's rate card computed *in code* and read out to the customer, in **Australian
  dollars**.

### The `quote` object — the price the customer was actually told

| Field | Type | Notes |
|---|---|---|
| `quote.bedrooms` | integer | The tier that was applied, derived from what they described |
| `quote.movers` | integer | Crew size |
| `quote.truck_tonnes` | number | The truck's tonnage, as the crew was described |
| `quote.rate` | number | $ per hour |
| `quote.min_hours` | number | Minimum booking |
| `quote.callout` | number | $ call-out fee |
| `quote.deposit` | number | $ to secure the booking, paid **after** the call |
| `quote.estimate_min` | number | `rate × min_hours + callout`. The figure they remember |

> **`quote` is `{}` when the call ended before a price was given** — wrong person, not
> moving any more, hung up early. Store every column as `NULL` in that case, not `0`:
> "never quoted" and "quoted nothing" are different facts and the office reports on the
> first. Whether the rates include GST is a per-client setting, not part of the payload;
> the bot says which, and it is the same for every call on that campaign.

The customer's own words still arrive in `qualification` — `property_size`,
`access`, `parking` and the rest — so a disputed price is settled by `quote_rate` and
`quote_estimate_min`, and the *reason* for it by the qualification fields.

### `card_numbers_blocked` — normally 0

An integer count of how many times the caller began reading out a card or bank-account
number and the agent stripped it **before the language model saw it** (any run of 12+
digits, written or spoken as words). The bot never takes payment on the call. A row above
0 is not an error; it is the guard working, and worth a listen for training.

### Sample payload

```json
{
  "call_id": "CALL-778899001",
  "campaign": "movers-au",
  "phone": "+61412345678",
  "prospect_name": "Alex Taylor",
  "started_at": "2026-09-28T09:11:07.220400+00:00",
  "ended_at": "2026-09-28T09:17:02.884100+00:00",
  "duration_sec": 355,
  "outcome": "BOOKING_PENDING_DEPOSIT",
  "outcome_note": "Booked - awaiting deposit",
  "consent_given": true,
  "booking_confirmed": true,
  "callback_request": "",
  "decline_reason": "",
  "remarks": "customer asked about boxes - office to confirm in writing",
  "qualification": {
    "details_confirmed": "yes",
    "date_confirmed": "yes",
    "pickup_area": "Parramatta",
    "dropoff_area": "Bondi Junction",
    "move_date": "Saturday 11 October",
    "property_size": "two-bedroom unit",
    "items": "furniture, boxes and a fridge",
    "furniture": "a queen bed, a sofa, a dining table and two wardrobes",
    "heavy_items": "none",
    "fragile_items": "the TV and some glassware",
    "access": "second floor with a lift at pickup, ground floor at the new place",
    "parking": "driveway at both ends",
    "notes": "",
    "arrival_window": "7 to 8 am",
    "full_name": "Alex Taylor",
    "contact_number": "+61412345678",
    "email": "alex@example.com.au",
    "email_spoken": "alex at example dot com dot au",
    "pickup_address": "12 Church Street, Parramatta",
    "delivery_address": "5 Oxford Street, Bondi Junction"
  },
  "quote": {
    "bedrooms": 2,
    "movers": 2,
    "truck_tonnes": 6.0,
    "rate": 150.0,
    "min_hours": 3.0,
    "callout": 80.0,
    "deposit": 100.0,
    "estimate_min": 530.0
  },
  "missing_required": [],
  "card_numbers_blocked": 0
}
```

### Field reference

| JSON path | Type | Notes |
|---|---|---|
| `call_id` | string\|null | Not unique — see gotcha 1 |
| `campaign` | string | Falls back to `"movers-au"` |
| `phone` | string\|null | The number dialled |
| `prospect_name` | string\|null | Raw dialer value, unvalidated. Used in the greeting |
| `started_at` / `ended_at` | string | ISO-8601 UTC with microseconds |
| `duration_sec` | integer | Whole seconds — **not** a float |
| `outcome` | string | Machine enum — see §8 |
| `outcome_note` | string | Human label |
| `consent_given` | boolean | Explicit yes to going ahead with the booking. **Consent artifact** |
| `booking_confirmed` | boolean | The full read-back was completed and confirmed |
| `callback_request` | string | Set when they asked to be called at a specific time |
| `decline_reason` | string | Populated on `NOT_INTERESTED` |
| `remarks` | string | One line for the office |
| `qualification.*` | string | 20 keys, always present, free text |
| `qualification.move_date` | string | **As spoken**: `"Saturday 11 October"`, `"the 3rd"`. Never parsed |
| `qualification.property_size` | string | As described; the bot derives the tier from it in code |
| `qualification.arrival_window` | string | One of the windows the bot offered, verbatim |
| `qualification.email` | string | Validated, or `""`. **Safe to send to** |
| `qualification.email_spoken` | string | Raw transcript. **Not an address** |
| `qualification.pickup_address` / `delivery_address` | string | Full address including postcode |
| `quote.*` | number | Computed in code, in AUD. `{}` when no quote was given |
| `missing_required` | array\<string\> | Subset of `move_date`, `property_size`, `arrival_window`, `full_name`, `contact_number`, `pickup_address`, `delivery_address`. Non-empty on `BOOKING_INCOMPLETE` |
| `card_numbers_blocked` | integer | Card/account numbers stripped before the LLM. Normally 0 |

Note there is **no `usage` object** on this campaign.

### Table

```sql
CREATE TABLE mov_call_dispositions (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                VARCHAR(64)  NULL,
  campaign               VARCHAR(64)  NULL,
  phone                  VARCHAR(32)  NULL,
  prospect_name          VARCHAR(120) NULL,

  started_at             DATETIME(6)  NULL,   -- store UTC; offset stripped
  ended_at               DATETIME(6)  NULL,
  duration_sec           INT UNSIGNED NULL,

  outcome                VARCHAR(32)  NOT NULL,
  outcome_note           VARCHAR(80)  NULL,
  consent_given          TINYINT(1)   NOT NULL DEFAULT 0,   -- said yes to booking
  booking_confirmed      TINYINT(1)   NOT NULL DEFAULT 0,   -- read-back completed
  callback_request       VARCHAR(120) NULL,
  decline_reason         TEXT         NULL,
  remarks                TEXT         NULL,

  -- qualification.* — ALL free text, in the customer's words
  qual_details_confirmed VARCHAR(32)  NULL,
  qual_date_confirmed    VARCHAR(32)  NULL,
  qual_pickup_area       VARCHAR(120) NULL,
  qual_dropoff_area      VARCHAR(120) NULL,
  qual_move_date         VARCHAR(120) NULL,
  qual_property_size     VARCHAR(120) NULL,
  qual_items             TEXT         NULL,
  qual_furniture         TEXT         NULL,
  qual_heavy_items       TEXT         NULL,   -- 'none' when there are none
  qual_fragile_items     TEXT         NULL,
  qual_access            TEXT         NULL,
  qual_parking           TEXT         NULL,
  qual_notes             TEXT         NULL,
  qual_arrival_window    VARCHAR(64)  NULL,
  qual_full_name         VARCHAR(120) NULL,
  qual_contact_number    VARCHAR(32)  NULL,
  qual_email             VARCHAR(160) NULL,   -- validated, or NULL. Never a guess
  qual_email_spoken      VARCHAR(255) NULL,   -- raw transcript. NOT an address
  qual_pickup_address    VARCHAR(255) NULL,
  qual_delivery_address  VARCHAR(255) NULL,

  -- quote.* — computed in code from the rate card, in AUD. NULL if never quoted.
  quote_bedrooms         TINYINT UNSIGNED NULL,
  quote_movers           TINYINT UNSIGNED NULL,
  quote_vehicle_tonnes   DECIMAL(5,1)  NULL,
  quote_rate             DECIMAL(9,2)  NULL,   -- per hour
  quote_min_hours        DECIMAL(4,1)  NULL,
  quote_callout          DECIMAL(9,2)  NULL,
  quote_deposit          DECIMAL(9,2)  NULL,
  quote_estimate_min     DECIMAL(10,2) NULL,   -- rate * min_hours + callout

  card_numbers_blocked   SMALLINT UNSIGNED NOT NULL DEFAULT 0,

  missing_required       JSON NULL,
  raw_payload            JSON NULL,

  PRIMARY KEY (id),
  KEY idx_call_id  (call_id),
  KEY idx_phone    (phone),
  KEY idx_outcome  (outcome),
  KEY idx_started  (started_at),
  KEY idx_movedate (qual_move_date),
  KEY idx_email    (qual_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

As on Campaign C, an empty `email` is stored as `NULL`, not `""` — the confirmation goes
by text when there is no address, so `WHERE qual_email IS NOT NULL` means exactly "we can
email this booking".

---

## 7. Campaign E — Warranty UK (appliance repair cover)

Source: `agent_warranty_uk.py`
Offer optional repair cover for a household appliance and send the policy **in writing**.

**Nothing is sold on this call and no payment is ever taken** — there is no tool in the
agent that could take one. The successful outcome is `INFO_REQUESTED`: the written
details agreed and on their way. If a payment ever appears against one of these rows, it
did not come from this call. There is no payment field here, and nothing to map one to.

**Shape follows Campaigns B, C and D.** Two things to know:

- **There is no `usage` object** (as on Movers).
- **`quote` is `{}` unless a price was actually given.** It holds the plan as the code
  stated it — `months`, `price` in pounds, and `includes`, the list of what's covered,
  spoken verbatim. Every call where the appliance was too old to quote for has an empty
  one, because the agent is forbidden from quoting those.

### The two columns this campaign is really about

| Field | Why it matters |
|---|---|
| `eligibility` | The CODE's verdict from the appliance's age, not the customer's opinion and not the model's: `eligible`, `check_required`, `unclear`, or `""`. `check_required` means it was over the plan's age limit and was referred rather than quoted |
| `qualification.existing_fault` | The compliance column. A fault named here means the agent STOPPED selling: an existing fault is not covered by a new policy, it says so plainly, and the call ends `EXISTING_FAULT`. A row that names a fault should never show a sale |

`card_numbers_blocked` counts the times the caller started reading out a card number and
the agent stripped it before the language model saw it. Normally 0; above 0 is the guard
working, not an error.

### Sample payload

```json
{
  "call_id": "CALL-903112004",
  "campaign": "warranty-uk",
  "phone": "+447700900123",
  "prospect_name": "Alex Taylor",
  "started_at": "2026-10-05T10:02:11.550300+00:00",
  "ended_at": "2026-10-05T10:06:48.113900+00:00",
  "duration_sec": 277,
  "outcome": "INFO_REQUESTED",
  "outcome_note": "Written details agreed - sending",
  "consent_given": true,
  "info_sent": true,
  "eligibility": "eligible",
  "callback_request": "",
  "decline_reason": "",
  "remarks": "asked whether a replacement is included - office to confirm in writing",
  "qualification": {
    "appliance": "washing machine",
    "appliance_confirmed": "yes",
    "appliance_age": "about three years",
    "working_normally": "yes",
    "existing_fault": "none",
    "already_covered": "no",
    "interest": "wants the details",
    "discuss_with": "",
    "notes": "",
    "first_name": "Alex",
    "last_name": "Taylor",
    "email": "alex@example.co.uk",
    "email_spoken": "alex at example dot co dot uk",
    "contact_number": "+447700900123"
  },
  "quote": {
    "months": 12,
    "price": 99.0,
    "includes": ["engineer call-out", "labour", "replacement parts", "repair costs"]
  },
  "missing_required": [],
  "card_numbers_blocked": 0
}
```

### Field reference

| JSON path | Type | Notes |
|---|---|---|
| `call_id` | string\|null | Not unique — see gotcha 1 |
| `campaign` | string | Falls back to `"warranty-uk"` |
| `phone` | string\|null | The number dialled |
| `prospect_name` | string\|null | Raw dialer value, unvalidated. Used in the greeting |
| `started_at` / `ended_at` | string | ISO-8601 UTC with microseconds |
| `duration_sec` | integer | Whole seconds — **not** a float |
| `outcome` | string | Machine enum — see §8 |
| `outcome_note` | string | Human label |
| `consent_given` | boolean | Explicit yes to **being sent the details**. Never consent to buy. **Consent artifact** |
| `info_sent` | boolean | The details were agreed and all the contact fields were captured |
| `eligibility` | string | `eligible` \| `check_required` \| `unclear` \| `""` — decided in code from the age |
| `callback_request` | string | Set when they asked for a specific time |
| `decline_reason` | string | Populated on `NOT_INTERESTED` |
| `remarks` | string | One line for the office |
| `qualification.*` | string | 14 keys, always present, free text |
| `qualification.appliance_age` | string | **As spoken**: `"about three years"`. Never parsed into the payload |
| `qualification.existing_fault` | string | `"none"`, or the fault in their words. **See above** |
| `qualification.email` | string | Validated, or `""`. **Safe to send to** |
| `qualification.email_spoken` | string | Raw transcript. **Not an address** |
| `quote.months` / `quote.price` | number | The plan as stated, GBP. Absent when `quote` is `{}` |
| `quote.includes` | array\<string\> | What the customer was told is covered, verbatim |
| `missing_required` | array\<string\> | Subset of `first_name`, `contact_number`. Non-empty on `INFO_INCOMPLETE` |
| `card_numbers_blocked` | integer | Card numbers stripped before the LLM. Normally 0 |

Note there is **no `usage` object** and **no payment field** on this campaign.

### Table

```sql
CREATE TABLE wty_call_dispositions (
  id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                 VARCHAR(64)  NULL,
  campaign                VARCHAR(64)  NULL,
  phone                   VARCHAR(32)  NULL,
  prospect_name           VARCHAR(120) NULL,

  started_at              DATETIME(6)  NULL,   -- store UTC; offset stripped
  ended_at                DATETIME(6)  NULL,
  duration_sec            INT UNSIGNED NULL,

  outcome                 VARCHAR(32)  NOT NULL,
  outcome_note            VARCHAR(80)  NULL,
  consent_given           TINYINT(1)   NOT NULL DEFAULT 0,   -- agreed to BE SENT details
  info_sent               TINYINT(1)   NOT NULL DEFAULT 0,
  eligibility             VARCHAR(16)  NULL,   -- eligible | check_required | unclear
  callback_request        VARCHAR(120) NULL,
  decline_reason          TEXT         NULL,
  remarks                 TEXT         NULL,

  -- qualification.* — ALL free text, in the customer's words
  qual_appliance          VARCHAR(120) NULL,
  qual_appliance_confirmed VARCHAR(32) NULL,
  qual_appliance_age      VARCHAR(120) NULL,
  qual_working_normally   VARCHAR(32)  NULL,
  qual_existing_fault     TEXT         NULL,   -- 'none' when there is none
  qual_already_covered    VARCHAR(160) NULL,
  qual_interest           VARCHAR(120) NULL,
  qual_discuss_with       VARCHAR(120) NULL,
  qual_notes              TEXT         NULL,
  qual_first_name         VARCHAR(80)  NULL,
  qual_last_name          VARCHAR(80)  NULL,
  qual_email              VARCHAR(160) NULL,   -- validated, or NULL. Never a guess
  qual_email_spoken       VARCHAR(255) NULL,   -- raw transcript. NOT an address
  qual_contact_number     VARCHAR(32)  NULL,

  -- quote.* — the plan as the code stated it, GBP. NULL if nothing was quoted.
  quote_months            TINYINT UNSIGNED NULL,
  quote_price             DECIMAL(9,2) NULL,
  quote_includes          JSON         NULL,

  card_numbers_blocked    SMALLINT UNSIGNED NOT NULL DEFAULT 0,

  missing_required        JSON NULL,
  raw_payload             JSON NULL,

  PRIMARY KEY (id),
  KEY idx_call_id     (call_id),
  KEY idx_phone       (phone),
  KEY idx_outcome     (outcome),
  KEY idx_started     (started_at),
  KEY idx_eligibility (eligibility),
  KEY idx_email       (qual_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

As on Campaigns C and D, an empty `email` is stored as `NULL`, not `""` — the details go
by text when there is no address, so `WHERE qual_email IS NOT NULL` means exactly "we
could email this one".

---

## 8. Outcome reference

`outcome` is the machine value and is safe to branch on. The human label is many-to-one
with it — several outcomes share a label — so **store both, and report on `outcome`**.

### Campaign A — Smart Health

| outcome | disposition_code |
|---|---|
| `TRANSFERRED` | Connected - Transferred to advisor |
| `QUALIFIED_PENDING` | Connected - Qualified, advisor to call |
| `CALLBACK_BOOKED` | Connected - Call back required |
| `NOT_QUALIFIED_NO_POLICY` | Connected - Not qualified |
| `NOT_QUALIFIED_AGE` | Connected - Not qualified |
| `NOT_QUALIFIED_PREMIUM` | Connected - Not qualified |
| `NOT_DECISION_MAKER` | Connected - Not qualified |
| `NOT_INTERESTED` | Connected - Not interested |
| `DNC` | Do Not Call |
| `VOICEMAIL` | Not connected |
| `MACHINE_DETECTED` | Not connected - reached a machine |
| `NO_OUTCOME` | Connected - Call back required |
| `DROPPED` | Not connected |

### Campaign B — Roof UK

| outcome | outcome_note |
|---|---|
| `CALLBACK_AGREED` | Adviser callback agreed - details captured |
| `CALLBACK_INCOMPLETE` | Agreed to callback - some details not given |
| `CALLBACK_BOOKED` | Callback requested for a specific time |
| `NOT_INTERESTED` | Not interested |
| `NOT_HOMEOWNER` | Not the property owner |
| `WRONG_NUMBER` | Wrong number |
| `DNC` | Do not call - opt out requested |
| `VOICEMAIL` | Answering machine |
| `MACHINE_DETECTED` | Reached a machine - IVR, voicemail or another AI |
| `NO_OUTCOME` | Connected - call back required |
| `DROPPED` | No answer / dropped before engagement |


### Campaign C — Finance AU

| outcome | outcome_note |
|---|---|
| `INFO_REQUESTED` | Information pack agreed - email captured |
| `QUALIFIED_CALLBACK` | Qualified - no usable email, adviser to call |
| `INFO_INCOMPLETE` | Agreed to information - some details not given |
| `CALLBACK_BOOKED` | Callback requested for a specific time |
| `NOT_LOOKING` | Not looking for finance |
| `NOT_INTERESTED` | Not interested |
| `WRONG_NUMBER` | Wrong number |
| `DNC` | Do not call - opt out requested |
| `VOICEMAIL` | Answering machine |
| `MACHINE_DETECTED` | Reached a machine - IVR, voicemail or another AI |
| `NO_OUTCOME` | Connected - call back required |
| `DROPPED` | No answer / dropped before engagement |

**Three outcomes here are all leads, and all need working differently.**
`INFO_REQUESTED` is complete: send the email. `QUALIFIED_CALLBACK` is a fully qualified
lead whose email could not be captured — it must be phoned, and it will never appear in
an "emails sent" report, so route it deliberately or it disappears. `INFO_INCOMPLETE`
means they agreed but stopped short of giving everything; `missing_required` says what.

**Two outcomes deserve their own handling.** `DNC` must flag the number as
never-call-again in the dialer, not merely be recorded. `CALLBACK_INCOMPLETE` means the
person agreed but didn't give everything — the lead is still worth working, and
`missing_required` says what's absent.

---

### Campaign D — Movers AU

| outcome | outcome_note |
|---|---|
| `BOOKING_PENDING_DEPOSIT` | Booked - awaiting deposit |
| `BOOKING_INCOMPLETE` | Agreed to book - details missing |
| `QUOTE_ONLY` | Quote given - not booked |
| `CALLBACK_BOOKED` | Callback requested for a specific time |
| `NOT_MOVING` | No longer moving |
| `NOT_INTERESTED` | Not interested |
| `WRONG_PERSON` | Wrong person / no inquiry |
| `DNC` | Do not call - opt out requested |
| `VOICEMAIL` | Answering machine |
| `MACHINE_DETECTED` | Reached a machine - IVR, voicemail or another AI |
| `NO_OUTCOME` | Connected - call back required |
| `DROPPED` | No answer / dropped before engagement |

`BOOKING_PENDING_DEPOSIT` is the win, and it is deliberately not "booked": the deposit is
paid after the call, so the office confirms the job once it lands. `BOOKING_INCOMPLETE`
means they said yes and the call ended before every required detail was captured — a
warm lead to finish by phone, not a lost one. `missing_required` lists what to ask for.

---

### Campaign E — Warranty UK

| outcome | outcome_note |
|---|---|
| `INFO_REQUESTED` | Written details agreed - sending |
| `INFO_INCOMPLETE` | Agreed to details - contact missing |
| `EXISTING_FAULT` | Existing fault - not sold, referred |
| `NOT_ELIGIBLE` | Appliance age - cover to be checked |
| `ALREADY_COVERED` | Already has cover |
| `THINKING` | Wants to consider it |
| `CALLBACK_BOOKED` | Callback requested for a specific time |
| `NOT_INTERESTED` | Not interested |
| `WRONG_PERSON` | Wrong person |
| `DNC` | Do not call - opt out requested |
| `VOICEMAIL` | Answering machine |
| `MACHINE_DETECTED` | Reached a machine - IVR, voicemail or another AI |
| `NO_OUTCOME` | Connected - call back required |
| `DROPPED` | No answer / dropped before engagement |

`INFO_REQUESTED` is the win, and it is only ever "details sent" — this campaign has no
sale to record. `EXISTING_FAULT` and `NOT_ELIGIBLE` are not failures: they are the agent
refusing to sell something it shouldn't, which is the whole point of the compliance
guards, and both are worth counting as a quality signal rather than a loss.

---

## 9. Known inconsistency — Campaign A vs the rest

Campaign A describes the same concepts with different names and shapes to B and C. This
is an artefact of it being built months earlier. It is documented rather than fixed
because Campaign A already posts to a live endpoint, and changing its shape would break
that consumer.

**Campaigns B and C agree with each other** — C was built on B's shape on purpose — so
this is one odd-one-out, not three dialects.

| Concept | Campaign A | Campaign B | Campaign C |
|---|---|---|---|
| Phone number | `prospect.phone` | `phone` | `phone` |
| Prospect name | `prospect.name` | `prospect_name` | `prospect_name` |
| Duration | `duration_seconds` (float) | `duration_sec` (int) | `duration_sec` (int) |
| Consent | `consent_to_contact` | `consent_given` | `consent_given` |
| Human label | `disposition_code` | `outcome_note` | `outcome_note` |
| Cost metrics | `usage{}` | *absent* | `usage{}` |
| Incomplete fields | *absent* | `missing_required[]` | `missing_required[]` |
| Handoff completed | *absent* | `callback_agreed` | `info_pack_sent` |
| Bot identifier | `agent` | *absent* | *absent* |

Three separate tables and three endpoint handlers — as planned — absorb this cleanly, so
nothing is blocked. If a single unified table is wanted later, only Campaign A needs
aligning, and that is best done **before** its endpoint carries more history rather than
after.
