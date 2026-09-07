-- ============================================================================
-- UDBR STORE — SQLite schema for AIM profile/rule dumps
-- ============================================================================
--
-- Designed against the actual exports (UDBR_2, UDBR_3), not from the AIM data
-- model. Seven properties of the real data drove the decisions below; each is
-- noted where it applies.
--
-- CENTRAL DECISION: SNAPSHOTS ARE IMMUTABLE AND NEVER OVERWRITTEN.
--
-- The dump is a point-in-time export. The whole reverification cycle — showing
-- what the RCM team resolved between one pull and the next — depends on holding
-- every snapshot rather than replacing the last one. A schema that upserts into
-- a single current-state table cannot answer "what changed", which is the main
-- question anyone will ask of this data.
--
-- Every rule and profile row is therefore keyed (snapshot_id, ...). Storage is
-- trivial at this scale: 596 rows per dump.
--
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- snapshots
--
-- One row per export loaded. `source_file` and `exported_at` describe the dump;
-- `loaded_at` describes when we ingested it.
--
-- Both are needed and they are not interchangeable. Keying history by the
-- dump's own date meant re-loading an older export could appear to supersede a
-- newer one — the file order and the run order disagreed. Ordering is by
-- loaded_at; exported_at is provenance.

CREATE TABLE snapshot (
    snapshot_id   INTEGER PRIMARY KEY,
    -- 'production' is an observed state of AIM. 'design' is a proposed target
    -- state that has NOT been applied. Keeping them in one table preserves the
    -- history in order, but they must be distinguishable: without this column
    -- a diff mixes "AIM changed" with "we proposed a change", and the
    -- reverification report would credit design decisions as RCM fixes.
    kind          TEXT    NOT NULL DEFAULT 'production'
                          CHECK (kind IN ('production','design')),
    derived_from  INTEGER REFERENCES snapshot(snapshot_id),  -- design: its base dump
    source_file   TEXT    NOT NULL,
    exported_at   TEXT,                        -- ISO date of the dump itself
    loaded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Counted consistently across kinds. row_count was previously the raw
    -- spreadsheet row count for a production dump (596) and the rule count for
    -- a design (475) - the same column carrying two incomparable meanings, so
    -- no query could reconcile them.
    src_rows      INTEGER NOT NULL,   -- rows in the source file
    src_rules     INTEGER NOT NULL,   -- rule rows the source declares
    src_profiles  INTEGER NOT NULL,   -- profile rows the source declares
    row_count     INTEGER NOT NULL,   -- retained: equals src_rows
    sha256        TEXT,                        -- of the source file
    notes         TEXT,
    UNIQUE (source_file, sha256)               -- same bytes never loaded twice
);

-- ---------------------------------------------------------------- AOH tree
--
-- The dump repeats Organization / Customer / Agency as strings on every row.
-- Normalising them into a self-referencing tree is what makes inheritance
-- queryable — "which agencies sit under this customer", "what does this agency
-- inherit" — instead of requiring string matching on every question.
--
-- tier is constrained because the AOH has exactly four levels and a typo in a
-- tier value would silently break every level-based query.

CREATE TABLE org_unit (
    org_unit_id   INTEGER PRIMARY KEY,
    snapshot_id   INTEGER NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    tier          TEXT    NOT NULL CHECK (tier IN ('System','Organization','Customer','Agency')),
    name          TEXT    NOT NULL,
    parent_id     INTEGER REFERENCES org_unit(org_unit_id),
    UNIQUE (snapshot_id, tier, name)
);

CREATE INDEX ix_org_unit_snap  ON org_unit (snapshot_id, tier);
CREATE INDEX ix_org_unit_parent ON org_unit (parent_id);

-- ---------------------------------------------------------------- profiles
--
-- PROFILE IDENTITY IS ProfileId, NEVER ProfileName.
--
-- ProfileName is not unique: 'Default Mapping Rules', 'NON-BILLABLE' and 'test'
-- each appear more than once in one dump, because a profile exists as a
-- separate instance per tier per entity. Grouping by name merges two customers'
-- unrelated profiles and manufactures conflicts that do not exist — a mistake
-- worth designing out rather than remembering.
--
-- parent_profile_id is the AOH chain, and it is SPARSE: only 5 of 51 profiles
-- have a parent, all of them Default Mapping Rules. Specific profiles exist at
-- exactly one tier with no parent. So this column must be nullable and no query
-- may assume a chain exists.

CREATE TABLE profile (
    profile_row_id      INTEGER PRIMARY KEY,
    snapshot_id         INTEGER NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    profile_guid        TEXT    NOT NULL,      -- AIM ProfileId
    name                TEXT    NOT NULL,
    description         TEXT,
    group_type          TEXT    NOT NULL,      -- 'Profiles' | 'Default Profile'
    tier                TEXT    NOT NULL CHECK (tier IN ('System','Organization','Customer','Agency')),
    org_unit_id         INTEGER NOT NULL REFERENCES org_unit(org_unit_id),
    payers              TEXT,                  -- 'Patient', 'All payers', ...
    has_group_localization INTEGER NOT NULL DEFAULT 0,
    parent_profile_guid TEXT,                  -- nullable and usually absent
    parent_tier         TEXT,
    UNIQUE (snapshot_id, profile_guid)
);

CREATE INDEX ix_profile_snap ON profile (snapshot_id, tier, group_type);
CREATE INDEX ix_profile_name ON profile (snapshot_id, name);
CREATE INDEX ix_profile_org  ON profile (org_unit_id);

-- ---------------------------------------------------------------- rules
--
-- One table for all three rule kinds, discriminated by rule_kind. They share
-- ~90% of their columns and every analysis (priority ordering, condition
-- parsing, shadowing) applies to all three, so splitting them would mean
-- duplicating both the columns and the queries.
--
-- TWO ATTACHMENT PATHS, and this is the awkward part of the real data:
--
--   Mapping rules attach to a PROFILE.
--   Selection rules attach to an ORG UNIT — their ProfileId does not resolve
--   to any profile in the dump (all 25 of them). They are not rules OF a
--   profile; they are rules that CHOOSE a profile.
--
-- Hence both profile_row_id and org_unit_id are nullable, with a CHECK that
-- exactly the right one is populated for the kind. Modelling selection rules
-- as belonging to a profile would have been wrong and would have quietly
-- dropped them from every profile-scoped query.
--
-- agency IS NOT ON THE RULE ROW. Mapping rules carry no Agency value at all
-- (0 of 520). A rule's agency comes from its profile's org_unit. Any query
-- that wants per-agency rules must join through profile.

CREATE TABLE rule (
    rule_row_id      INTEGER PRIMARY KEY,
    snapshot_id      INTEGER NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    rule_guid        TEXT    NOT NULL,         -- AIM RuleId; unique per snapshot
    rule_kind        TEXT    NOT NULL CHECK (rule_kind IN ('mapping','profile_selection','payer_selection')),

    profile_row_id   INTEGER REFERENCES profile(profile_row_id),   -- mapping only
    org_unit_id      INTEGER REFERENCES org_unit(org_unit_id),     -- selection only

    tier             TEXT    NOT NULL CHECK (tier IN ('System','Organization','Customer','Agency')),
    tab              TEXT,                     -- ANSI 5010 | HCFA 1500 | Charges | Payers | ...
    target_field     TEXT    NOT NULL,
    target_field_source     TEXT,
    target_field_value_type TEXT,              -- EYesNo | String | Date | KeyValue | BillItem | ...
    priority         INTEGER,
    description      TEXT,

    conditions_raw   TEXT,                     -- verbatim; SOURCE OF TRUTH
    is_unconditional INTEGER NOT NULL DEFAULT 0,

    outcome_type     TEXT,                     -- Set to | Set from Bill Field | Set from PCR Field
    outcome_value    TEXT,
    outcome_raw      TEXT,

    -- selection-rule specific
    resulting_profile_name TEXT,               -- ProSR: NAME, not id — see note below
    payer_name       TEXT,                     -- PaySR
    payer_form_type  TEXT,
    payer_timing     TEXT,

    is_rule_localization INTEGER NOT NULL DEFAULT 0,

    UNIQUE (snapshot_id, rule_guid),
    CHECK (
        (rule_kind = 'mapping'  AND profile_row_id IS NOT NULL AND org_unit_id IS NULL)
     OR (rule_kind <> 'mapping' AND profile_row_id IS NULL     AND org_unit_id IS NOT NULL)
    )
);

CREATE INDEX ix_rule_snap    ON rule (snapshot_id, rule_kind);
CREATE INDEX ix_rule_profile ON rule (profile_row_id, target_field, priority);
CREATE INDEX ix_rule_field   ON rule (snapshot_id, target_field);
CREATE INDEX ix_rule_guid    ON rule (rule_guid);

-- resulting_profile_name is a NAME because that is what AIM exports. It
-- resolves today (20 of 20, no ambiguity) but profile names are not unique, so
-- this is a latent referential hazard rather than a safe join. The view below
-- reports ambiguity instead of hiding it.

-- ---------------------------------------------------------------- conditions
--
-- Conditions arrive as free text: 'PCR CMS Service Level is one of BLS',
-- 'Bill Number contains all MA', '(always applies)'. The vocabulary is small —
-- seven operators observed — and 30 of 545 are multi-clause with AND/OR.
--
-- Stored PARSED as well as raw, because the analyses that matter need
-- structure: prefix shadowing needs the match pattern, unsatisfiability needs
-- to know a 'contains all' list has several values against a single-valued
-- field.
--
-- conditions_raw remains the source of truth. The parse is derived and CAN BE
-- WRONG — parse_ok records whether it succeeded, so a failed parse is visible
-- rather than silently producing an empty condition set that makes a rule look
-- unconditional.

CREATE TABLE rule_condition (
    condition_id  INTEGER PRIMARY KEY,
    rule_row_id   INTEGER NOT NULL REFERENCES rule(rule_row_id) ON DELETE CASCADE,
    clause_ix     INTEGER NOT NULL,            -- position within the expression
    conjunction   TEXT CHECK (conjunction IN ('AND','OR')),   -- joins to previous clause
    source        TEXT,                        -- 'PCR' | 'Bill' | NULL
    field         TEXT NOT NULL,
    operator      TEXT NOT NULL,               -- equals | is one of | contains all | ...
    negated       INTEGER NOT NULL DEFAULT 0,
    parse_ok      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (rule_row_id, clause_ix)
);

CREATE TABLE rule_condition_value (
    condition_id  INTEGER NOT NULL REFERENCES rule_condition(condition_id) ON DELETE CASCADE,
    value_ix      INTEGER NOT NULL,
    value         TEXT    NOT NULL,
    PRIMARY KEY (condition_id, value_ix)
);

CREATE INDEX ix_cond_field ON rule_condition (field, operator);

-- ============================================================================
-- ANALYTICAL LAYER — decisions and derived facts, deliberately separate
-- ============================================================================
--
-- Everything above is dump data: reload it and you get the same thing back.
-- Everything below is judgement. Keeping them apart means re-deciding
-- "is Baserate Rate Code config-resolved" does not require reloading dumps,
-- and reloading a dump does not silently revert a decision.
--
-- These tables are NOT snapshot-scoped. A decision about a field is about the
-- field, not about one export.

-- Which target fields resolve against an agency's own AIM configuration.
-- Marked at FIELD level because a field is config-resolved wherever it appears
-- — Baserate Rate Code means an agency-specific code in every profile that
-- sets it. Rule-level marking would drift.
--
-- exception_reason exists for fields that are agency-specific for a DIFFERENT
-- reason: MBI/Policy Number is a general-purpose display bucket on the patient
-- invoice, not a lookup into a code table. Same destination, different logic,
-- and the distinction is worth keeping legible.

CREATE TABLE config_resolved_field (
    tab           TEXT NOT NULL,
    field         TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('config_lookup','agency_text')),
    reason        TEXT NOT NULL,
    decided_on    TEXT NOT NULL DEFAULT (date('now')),
    decided_by    TEXT,
    PRIMARY KEY (tab, field),
    FOREIGN KEY (tab, field) REFERENCES field_vocabulary(tab, field)
);

CREATE TABLE profile_scope (
    org_unit_id  INTEGER NOT NULL REFERENCES org_unit(org_unit_id) ON DELETE CASCADE,
    profile_name TEXT    NOT NULL,
    scope        TEXT    NOT NULL CHECK (scope IN ('in_scope','out_of_scope','posture_neutral')),
    reason       TEXT,
    derived      INTEGER NOT NULL DEFAULT 0,   -- 1 = inferred, 0 = stated by a person
    decided_on   TEXT NOT NULL DEFAULT (date('now')),
    PRIMARY KEY (org_unit_id, profile_name)
);

CREATE TABLE analysis_run (
    run_id       INTEGER PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    ran_at       TEXT NOT NULL DEFAULT (datetime('now')),
    analyzer_ver TEXT,
    n_findings   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE finding (
    finding_id   INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES analysis_run(run_id) ON DELETE CASCADE,
    finding_key  TEXT NOT NULL,
    check_code   TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('High','Medium','Low')),
    tier         TEXT,
    entity       TEXT,
    subject      TEXT,
    finding_text TEXT,
    evidence     TEXT,
    profile_guid TEXT,
    UNIQUE (run_id, finding_key)
);
-- Which rules a finding is actually about, so it can be marked on the
-- offending rows rather than only listed against a profile.
CREATE TABLE finding_rule (
    finding_id  INTEGER NOT NULL REFERENCES finding(finding_id) ON DELETE CASCADE,
    rule_row_id INTEGER NOT NULL REFERENCES rule(rule_row_id) ON DELETE CASCADE,
    PRIMARY KEY (finding_id, rule_row_id)
);
CREATE INDEX IF NOT EXISTS ix_finding_key ON finding (finding_key);
CREATE INDEX IF NOT EXISTS ix_run_snap ON analysis_run (snapshot_id, ran_at);

-- Run-to-run comparison. Resolved / new / still-open against the run
-- immediately before, per snapshot lineage.
CREATE VIEW v_reverification AS
WITH ordered AS (
  SELECT run_id, snapshot_id, ran_at,
         LAG(run_id) OVER (ORDER BY ran_at, run_id) AS prev_run
  FROM analysis_run
)
SELECT o.run_id, o.prev_run, o.ran_at,
       f.finding_key, f.check_code, f.severity, f.entity, f.subject,
       CASE WHEN o.prev_run IS NULL THEN 'first-run'
            WHEN NOT EXISTS (SELECT 1 FROM finding p
                             WHERE p.run_id = o.prev_run
                               AND p.finding_key = f.finding_key) THEN 'new'
            ELSE 'carried' END AS state
FROM ordered o JOIN finding f ON f.run_id = o.run_id;

CREATE TABLE retired_check (
    check_code  TEXT PRIMARY KEY,
    check_name  TEXT NOT NULL,
    retired_on  TEXT NOT NULL DEFAULT (date('now')),
    reason      TEXT NOT NULL
);

-- ============================================================================
-- VIEWS
-- ============================================================================

-- A rule with its full AOH context resolved. Agency comes from the profile,
-- never from the rule row.
CREATE VIEW v_rule_context AS
SELECT r.rule_row_id, r.snapshot_id, r.rule_guid, r.rule_kind,
       r.tier            AS rule_tier,
       p.name            AS profile_name,
       p.profile_guid,
       p.group_type,
       cust.name         AS customer,
       agy.name          AS agency,
       COALESCE(agy.name, cust.name, ou.name) AS owning_entity,
       r.tab, r.target_field, r.priority,
       r.conditions_raw, r.is_unconditional,
       r.outcome_type, r.outcome_value,
       r.resulting_profile_name, r.payer_name,
       CASE WHEN c.field IS NOT NULL THEN 1 ELSE 0 END AS is_config_resolved,
       c.kind AS config_kind
FROM rule r
LEFT JOIN profile  p    ON p.profile_row_id = r.profile_row_id
LEFT JOIN org_unit ou   ON ou.org_unit_id  = COALESCE(p.org_unit_id, r.org_unit_id)
LEFT JOIN org_unit agy  ON agy.org_unit_id = CASE WHEN ou.tier='Agency'   THEN ou.org_unit_id END
LEFT JOIN org_unit cust ON cust.org_unit_id = CASE
                                WHEN ou.tier='Customer' THEN ou.org_unit_id
                                WHEN ou.tier='Agency'   THEN ou.parent_id END
-- Joined on (tab, field). config_resolved_field is keyed on the destination,
-- not the field name, so joining on the name alone does not even resolve.
LEFT JOIN config_resolved_field c ON c.tab = r.tab AND c.field = r.target_field;

-- Selection rules whose ResultingProfile name matches more than one profile.
-- Empty today; it will not stay empty as the estate grows, and a silent
-- mis-resolution here selects the wrong profile for a bill.
CREATE VIEW v_ambiguous_profile_reference AS
SELECT r.snapshot_id, r.rule_guid, r.resulting_profile_name,
       COUNT(p.profile_row_id) AS matching_profiles
FROM rule r
JOIN profile p ON p.snapshot_id = r.snapshot_id
              AND p.name        = r.resulting_profile_name
WHERE r.resulting_profile_name IS NOT NULL
GROUP BY r.rule_row_id
HAVING COUNT(p.profile_row_id) <> 1;

-- Localization completeness. For each agency, for each in-scope profile, every
-- config-resolved rule needs a resolvable value at that agency.
--
-- Missing is a defect. out_of_scope contributes nothing. This is the query the
-- whole scope/localization design exists to make answerable.
CREATE VIEW v_localization_requirement AS
SELECT agy.org_unit_id      AS agency_id,
       agy.name             AS agency,
       p.name               AS profile_name,
       r.target_field,
       r.rule_guid,
       COALESCE(s.scope,'in_scope') AS scope,
       CASE
         WHEN COALESCE(s.scope,'in_scope') = 'out_of_scope' THEN 'not_applicable'
         WHEN r.tier = 'Agency' AND r.outcome_value IS NOT NULL
              AND TRIM(r.outcome_value) <> ''                THEN 'supplied'
         ELSE 'missing'
       END                  AS state
FROM rule r
JOIN profile  p   ON p.profile_row_id = r.profile_row_id
JOIN config_resolved_field c ON c.tab = r.tab AND c.field = r.target_field
JOIN org_unit cust ON cust.org_unit_id = p.org_unit_id AND cust.tier IN ('Customer','Agency')
JOIN org_unit agy  ON (agy.org_unit_id = cust.org_unit_id AND cust.tier='Agency')
                   OR (agy.parent_id   = cust.org_unit_id AND agy.tier='Agency')
LEFT JOIN profile_scope s ON s.org_unit_id = agy.org_unit_id AND s.profile_name = p.name
WHERE r.rule_kind = 'mapping';

-- Rule-level diff between two snapshots. Ordering is by loaded_at, not by the
-- dump's own date.
CREATE VIEW v_snapshot_rule_state AS
SELECT r.snapshot_id, r.rule_guid, r.tier, r.target_field, r.priority,
       r.conditions_raw, r.outcome_value,
       COALESCE(p.name,'(selection rule)') AS profile_name
FROM rule r LEFT JOIN profile p ON p.profile_row_id = r.profile_row_id;


-- ============================================================================
-- FIELD VOCABULARY — what the AIM profile UI actually offers
-- ============================================================================
--
-- The dump only emits fields that already have rules, so it shows 45 of the
-- 148 destinations a profile can set. Completeness measured against the dump
-- can therefore only ask "does this profile set what its siblings set", which
-- is a heuristic. Measured against this table it can ask "does it set what the
-- tab offers", which is a check.
--
-- NOT snapshot-scoped: the vocabulary is a property of the product, not of an
-- export. It changes when AIM changes.
--
-- A destination is (tab, field). base_name strips the HCFA box suffix so that
-- 'Signature Provided (12)' and 'Signature Provided' can be recognised as the
-- same underlying concept on different tabs - which they are - while remaining
-- different destinations, which they also are.

CREATE TABLE field_vocabulary (
    tab          TEXT    NOT NULL,
    field        TEXT    NOT NULL,
    tab_order    INTEGER,
    field_order  INTEGER,
    base_name    TEXT    NOT NULL,
    form_box     TEXT,
    -- Set where the same base name appears on another tab. 'Emergency' is the
    -- dangerous case: identical name, two tabs, two destinations. Keying any
    -- analysis on field name alone merges them.
    shares_name_with TEXT,
    PRIMARY KEY (tab, field)
);

CREATE INDEX ix_vocab_base ON field_vocabulary (base_name);

-- Destinations a profile could set but does not. The dump cannot express this
-- at all, because it only contains fields that have rules.
CREATE VIEW v_field_coverage AS
SELECT v.tab, v.field, v.base_name,
       COUNT(DISTINCT r.profile_row_id) AS profiles_setting_it,
       COUNT(r.rule_row_id)             AS rules
FROM field_vocabulary v
LEFT JOIN rule r ON r.tab = v.tab AND r.target_field = v.field
GROUP BY v.tab, v.field;
