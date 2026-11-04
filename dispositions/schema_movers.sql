-- schema_movers.sql — Campaign D, AU removalist booking (agent_movers_au.py).
--
-- MIGRATION for a server that already has the voicebot database. Run once:
--     mysql -u root -p < schema_movers.sql
--
-- Safe to re-run: CREATE TABLE IF NOT EXISTS. It does NOT touch the existing tables,
-- the voicebot user, or its grants — GRANT ... ON voicebot.* already covers this table.
--
-- The same CREATE TABLE is also appended to schema.sql, so a fresh server built from
-- that file gets all four campaigns in one go. Keep the two copies identical.
--
-- Design notes carried over from the other campaigns:
--   • call_id is NOT unique — the dialer has been sending a constant value. Plain
--     index, never a UNIQUE constraint, or inserts start failing silently.
--   • Every qualification column is VARCHAR/TEXT. Customers say "a two-bed flat" and
--     "next Saturday"; normalisation is a downstream job.
--   • utf8mb4, not utf8. MySQL's "utf8" is 3-byte and rejects characters the
--     transcriber genuinely produces.
--   • raw_payload keeps the whole body verbatim. When a value looks wrong six weeks
--     from now, that column is the difference between an answer and a shrug.
--
-- WHAT IS DIFFERENT ABOUT THIS CAMPAIGN:
--   The quote_* columns are NUMERIC, unlike every qualification column. They are not
--   what a customer said — they are what the bot's rate card computed in code and read
--   out verbatim, in AUSTRALIAN DOLLARS. They are the price the customer was quoted, so
--   the office must be able to sum and compare them, and a dispute is settled by
--   quote_rate and quote_estimate_min rather than by a transcript.
--
--   card_numbers_blocked counts the times the caller began reading out a card or
--   account number and the agent stripped it before the LLM saw it. Normally 0. A row
--   above 0 is not an error — it is the guard working, and worth a listen for training.

USE voicebot;

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
  qual_parking           TEXT         NULL,   -- driveway / parking for the truck
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
  quote_vehicle_tonnes   DECIMAL(5,1)  NULL,   -- the truck's tonnage
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
