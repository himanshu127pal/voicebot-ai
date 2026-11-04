-- schema_warranty.sql — Campaign E, UK appliance warranty cover (agent_warranty_uk.py).
--
-- MIGRATION for a server that already has the voicebot database. Run once:
--     mysql -u root -p < schema_warranty.sql
--
-- Safe to re-run: CREATE TABLE IF NOT EXISTS. It does NOT touch the existing tables,
-- the voicebot user, or its grants — GRANT ... ON voicebot.* already covers this table.
--
-- The same CREATE TABLE is also appended to schema.sql, so a fresh server built from
-- that file gets all five campaigns in one go. Keep the two copies identical.
--
-- Design notes carried over from the other campaigns:
--   • call_id is NOT unique — the dialer has been sending a constant value. Plain
--     index, never a UNIQUE constraint, or inserts start failing silently.
--   • Every qualification column is VARCHAR/TEXT. People say "about three years" and
--     "it's the one in the kitchen"; normalisation is a downstream job.
--   • utf8mb4, not utf8. MySQL's "utf8" is 3-byte and rejects characters the
--     transcriber genuinely produces.
--   • raw_payload keeps the whole body verbatim.
--
-- WHAT IS DIFFERENT ABOUT THIS CAMPAIGN:
--   • THERE IS NO PAYMENT COLUMN, because the bot never takes one. Nothing is sold on
--     the call: the only successful outcome is INFO_REQUESTED — the policy sent in
--     writing. If a payment ever appears against one of these rows, it did not come
--     from this call.
--   • eligibility is the CODE's verdict from the appliance's age, not the customer's
--     opinion and not the model's: 'eligible' | 'check_required' | 'unclear' | ''.
--     quote_* is NULL whenever no price was given, which includes every
--     'check_required' row — the bot is forbidden from quoting one.
--   • qual_existing_fault is the compliance column. A row where it names a fault must
--     never show a sale; the bot stops selling and records EXISTING_FAULT. Worth a
--     standing report: SELECT ... WHERE qual_existing_fault NOT IN ('', 'none').
--   • card_numbers_blocked counts the times the caller began reading out a card number
--     and the agent stripped it before the LLM saw it. Above 0 is the guard working.

USE voicebot;

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
