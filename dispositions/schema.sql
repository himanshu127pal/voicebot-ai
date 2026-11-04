-- schema.sql — disposition store for every voice campaign.
--
-- Run once:  mysql -u root -p < schema.sql
-- Then set the receiver's DB_PASSWORD to whatever you put in CHANGE_ME_STRONG_PASSWORD.
--
-- Design notes that matter (full reasoning in DISPOSITION_WEBHOOK_SPEC.md):
--   • call_id is NOT unique — the dialer has been sending a constant value. It gets a
--     plain index, never a UNIQUE constraint, or inserts start failing silently.
--   • Every qualification column is VARCHAR. Callers say "about three hundred and forty
--     dollars" as often as "$340"; normalisation is a downstream job, deliberately not
--     done on a live call.
--   • utf8mb4, not utf8. MySQL's "utf8" is 3-byte and rejects characters the
--     transcriber genuinely produces.
--   • raw_payload keeps the whole body verbatim. When a value looks wrong six weeks
--     from now, that column is the difference between an answer and a shrug.

CREATE DATABASE IF NOT EXISTS voicebot
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE voicebot;

-- The receiver connects over loopback only.
CREATE USER IF NOT EXISTS 'voicebot'@'127.0.0.1'
  IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';
GRANT SELECT, INSERT, UPDATE ON voicebot.* TO 'voicebot'@'127.0.0.1';

-- Optional read-only account for reporting / the Vici box. Only useful if you also
-- open MySQL to that host — see the deploy runbook before you do.
-- CREATE USER IF NOT EXISTS 'voicebot_ro'@'%' IDENTIFIED BY 'CHANGE_ME_TOO';
-- GRANT SELECT ON voicebot.* TO 'voicebot_ro'@'%';

FLUSH PRIVILEGES;


-- ═══════════════════════════════════════════════════════════════════════════
-- Campaign A — Smart Health (AU health insurance)
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sh_call_dispositions (
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

  started_at              DATETIME(6)  NULL,   -- UTC, offset stripped on insert
  ended_at                DATETIME(6)  NULL,
  duration_seconds        DECIMAL(8,1) NULL,

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


-- ═══════════════════════════════════════════════════════════════════════════
-- Campaign B — Roof UK (free assessment)
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS roof_call_dispositions (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id               VARCHAR(64)  NULL,
  campaign              VARCHAR(64)  NULL,
  phone                 VARCHAR(32)  NULL,
  prospect_name         VARCHAR(120) NULL,

  started_at            DATETIME(6)  NULL,   -- UTC, offset stripped on insert
  ended_at              DATETIME(6)  NULL,
  duration_sec          INT UNSIGNED NULL,

  outcome               VARCHAR(32)  NOT NULL,
  outcome_note          VARCHAR(80)  NULL,
  consent_given         TINYINT(1)   NOT NULL DEFAULT 0,
  callback_agreed       TINYINT(1)   NOT NULL DEFAULT 0,
  callback_request      VARCHAR(120) NULL,
  decline_reason        TEXT         NULL,
  remarks               TEXT         NULL,

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


-- ═══════════════════════════════════════════════════════════════════════════
-- Campaign C — Finance AU (personal & business loans)
-- ═══════════════════════════════════════════════════════════════════════════
-- Added after the first two. On a server that ALREADY has this database, run
-- schema_finance.sql instead — it is this same table on its own.
--
-- qual_email is validated and deliverable or NULL, never a guess. qual_email_spoken is
-- the raw transcript kept even when the address did not validate; it is an audit trail
-- and a repair hint, NOT an address. Do not send mail to it.
CREATE TABLE IF NOT EXISTS fin_call_dispositions (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                VARCHAR(64)  NULL,
  campaign               VARCHAR(64)  NULL,
  phone                  VARCHAR(32)  NULL,
  prospect_name          VARCHAR(120) NULL,

  started_at             DATETIME(6)  NULL,   -- UTC, offset stripped on insert
  ended_at               DATETIME(6)  NULL,
  duration_sec           INT UNSIGNED NULL,

  outcome                VARCHAR(32)  NOT NULL,
  outcome_note           VARCHAR(80)  NULL,
  consent_given          TINYINT(1)   NOT NULL DEFAULT 0,
  info_pack_sent         TINYINT(1)   NOT NULL DEFAULT 0,
  callback_request       VARCHAR(120) NULL,
  decline_reason         TEXT         NULL,
  remarks                TEXT         NULL,

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
  qual_email               VARCHAR(160) NULL,
  qual_email_spoken        VARCHAR(255) NULL,
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

-- ═══════════════════════════════════════════════════════════════════════════
-- Campaign D — Movers AU (removalist booking)
-- ═══════════════════════════════════════════════════════════════════════════
-- Added after the first three. On a server that ALREADY has this database, run
-- schema_movers.sql instead — it is this same table on its own.
--
-- The quote_* columns are numeric on purpose: they are not what the customer said, they
-- are what the rate card computed in code and the bot read out, in dollars. A pricing
-- dispute is settled by quote_rate and quote_estimate_min, not by a transcript.

CREATE TABLE IF NOT EXISTS mov_call_dispositions (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                VARCHAR(64)  NULL,
  campaign               VARCHAR(64)  NULL,
  phone                  VARCHAR(32)  NULL,
  prospect_name          VARCHAR(120) NULL,

  started_at             DATETIME(6)  NULL,   -- UTC, offset stripped on insert
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
  qual_details_confirmed VARCHAR(32)  NULL,   -- route from the inquiry confirmed
  qual_date_confirmed    VARCHAR(32)  NULL,
  qual_pickup_area       VARCHAR(120) NULL,
  qual_dropoff_area      VARCHAR(120) NULL,
  qual_move_date         VARCHAR(120) NULL,
  qual_property_size     VARCHAR(120) NULL,
  qual_items             TEXT         NULL,
  qual_furniture         TEXT         NULL,
  qual_heavy_items       TEXT         NULL,   -- 'none' when there are none
  qual_fragile_items     TEXT         NULL,
  qual_access            TEXT         NULL,   -- floors, stairs, lifts — both ends
  qual_parking           TEXT         NULL,   -- driveway, permit, suspended bay
  qual_notes             TEXT         NULL,
  qual_arrival_window    VARCHAR(64)  NULL,
  qual_full_name         VARCHAR(120) NULL,
  qual_contact_number    VARCHAR(32)  NULL,
  qual_email             VARCHAR(160) NULL,   -- validated, or NULL. Never a guess
  qual_email_spoken      VARCHAR(255) NULL,   -- raw transcript. NOT an address
  qual_pickup_address    VARCHAR(255) NULL,
  qual_delivery_address  VARCHAR(255) NULL,

  -- quote.* — computed in code from the rate card, in AUD. What the customer was told.
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

-- ═══════════════════════════════════════════════════════════════════════════
-- Campaign E — Warranty UK (appliance repair cover)
-- ═══════════════════════════════════════════════════════════════════════════
-- Added after the first four. On a server that ALREADY has this database, run
-- schema_warranty.sql instead — it is this same table on its own.
--
-- No payment column, on purpose: the bot never takes one. The successful outcome
-- is INFO_REQUESTED — the policy sent in writing — and nothing else.

CREATE TABLE IF NOT EXISTS wty_call_dispositions (
  id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  received_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  call_id                 VARCHAR(64)  NULL,
  campaign                VARCHAR(64)  NULL,
  phone                   VARCHAR(32)  NULL,
  prospect_name           VARCHAR(120) NULL,

  started_at              DATETIME(6)  NULL,   -- UTC, offset stripped on insert
  ended_at                DATETIME(6)  NULL,
  duration_sec            INT UNSIGNED NULL,

  outcome                 VARCHAR(32)  NOT NULL,
  outcome_note            VARCHAR(80)  NULL,
  consent_given           TINYINT(1)   NOT NULL DEFAULT 0,   -- agreed to BE SENT details
  info_sent               TINYINT(1)   NOT NULL DEFAULT 0,   -- the pack went out
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

  -- quote.* — the plan as the code stated it, in GBP. NULL if nothing was quoted.
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
