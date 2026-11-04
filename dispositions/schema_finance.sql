-- schema_finance.sql — Campaign C, AU finance brokerage (agent_finance_au.py).
--
-- MIGRATION for a server that already has the voicebot database. Run once:
--     mysql -u root -p < schema_finance.sql
--
-- Safe to re-run: CREATE TABLE IF NOT EXISTS. It does NOT touch the existing tables,
-- the voicebot user, or its grants — GRANT ... ON voicebot.* already covers this table.
--
-- The same CREATE TABLE is also appended to schema.sql, so a fresh server built from
-- that file gets all three campaigns in one go. Keep the two copies identical.
--
-- Design notes carried over from the other two campaigns:
--   • call_id is NOT unique — the dialer has been sending a constant value. Plain
--     index, never a UNIQUE constraint, or inserts start failing silently.
--   • Every qualification column is VARCHAR/TEXT. Callers say "around thirty grand"
--     as often as "$30,000"; normalisation is a downstream job.
--   • utf8mb4, not utf8. MySQL's "utf8" is 3-byte and rejects characters the
--     transcriber genuinely produces.
--   • raw_payload keeps the whole body verbatim. When a value looks wrong six weeks
--     from now, that column is the difference between an answer and a shrug.
--
-- ONE COLUMN IS DIFFERENT TO THE OTHER CAMPAIGNS AND MATTERS:
--   qual_email        — validated, normalised, deliverable, or NULL. Never a guess.
--   qual_email_spoken — the raw transcript of what they said, kept even when the
--                       address did not validate. On an INFO_REQUESTED row it is an
--                       audit trail; on a QUALIFIED_CALLBACK row it is often enough
--                       for a human to work out the address that the STT could not.
--   Do NOT email qual_email_spoken. It is not an address.

USE voicebot;

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
