-- Jira -> MSSQL analytics schema
-- Idempotent: safe to run every startup, only creates objects that don't exist yet.

IF OBJECT_ID('dbo.jira_project', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_project (
        project_id          BIGINT          NOT NULL PRIMARY KEY,
        project_key         NVARCHAR(20)    NOT NULL,
        project_name        NVARCHAR(255)   NULL,
        project_type_key    NVARCHAR(50)    NULL,   -- e.g. "software", "business"
        etl_loaded_at        DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_project_key UNIQUE (project_key)
    );
END

IF OBJECT_ID('dbo.jira_issues', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issues (
        issue_id            BIGINT          NOT NULL PRIMARY KEY,
        issue_key           NVARCHAR(20)    NOT NULL,
        project_key         NVARCHAR(20)    NOT NULL,
        project_name        NVARCHAR(255)   NULL,
        issue_type          NVARCHAR(100)   NULL,
        summary             NVARCHAR(1000)  NULL,
        status              NVARCHAR(100)   NULL,
        status_category     NVARCHAR(100)   NULL,
        priority             NVARCHAR(50)    NULL,
        assignee_account_id  NVARCHAR(100)   NULL,
        assignee_name         NVARCHAR(255)   NULL,
        reporter_account_id   NVARCHAR(100)   NULL,
        reporter_name          NVARCHAR(255)   NULL,
        created                 DATETIME2       NULL,
        updated                  DATETIME2       NULL,
        resolutiondate            DATETIME2       NULL,
        labels                     NVARCHAR(MAX)   NULL,   -- JSON array
        components                  NVARCHAR(MAX)   NULL,   -- JSON array
        fix_versions                  NVARCHAR(MAX)   NULL,   -- JSON array
        custom_fields_json              NVARCHAR(MAX)   NULL,   -- raw customfield_* key/value pairs
        raw_json                          NVARCHAR(MAX)   NULL,   -- full unmodified issue payload
        etl_loaded_at                       DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_issues_key UNIQUE (issue_key)
    );
    CREATE INDEX IX_jira_issues_project ON dbo.jira_issues(project_key);
    CREATE INDEX IX_jira_issues_updated ON dbo.jira_issues(updated);
END

-- Added alongside jira_project below: project_id is additive. project_key/project_name
-- stay as-is for now (denormalized-convenience-vs-drop tradeoff is a separate open question).
IF COL_LENGTH('dbo.jira_issues', 'project_id') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD project_id BIGINT NULL;
END

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_jira_issues_project')
BEGIN
    ALTER TABLE dbo.jira_issues ADD CONSTRAINT FK_jira_issues_project
        FOREIGN KEY (project_id) REFERENCES dbo.jira_project(project_id);
END

-- Additive, same pattern as project_id above. due_date is a plain Jira date (no time
-- component), matching jira_fix_versions.release_date's DATE type. environment is stored
-- as NVARCHAR(MAX) rather than flattened further since, like "Description", Jira's
-- schema.type "string" here doesn't rule out ADF rich text -- transform.flatten_issue
-- flattens it through the same ADF-to-text helper used for custom field values if so.
-- security_level is flattened to its .name only, matching how priority/status_category
-- (also lookup-object fields) are already handled on this table -- confirmed against live
-- data that no issue on this site currently sets environment or security (0 of 3,579), so
-- this is schema-ready but unexercised against real non-null values for those two.
IF COL_LENGTH('dbo.jira_issues', 'due_date') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD due_date DATE NULL;
END

IF COL_LENGTH('dbo.jira_issues', 'environment') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD environment NVARCHAR(MAX) NULL;
END

IF COL_LENGTH('dbo.jira_issues', 'security_level') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD security_level NVARCHAR(255) NULL;
END

-- Additive, same pattern as above. Issue-level time-tracking rollups (Jira's own
-- Original Estimate / Remaining Estimate / Time Spent on the issue itself) -- distinct
-- from jira_worklogs.time_spent_seconds, which is per individual work-log entry, not the
-- issue's aggregate. All three are plain integer seconds on the wire, same representation
-- jira_worklogs already uses, confirmed against live data (non-zero on 4/15/12 issues
-- respectively out of 3,579).
IF COL_LENGTH('dbo.jira_issues', 'original_estimate_seconds') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD original_estimate_seconds INT NULL;
END

IF COL_LENGTH('dbo.jira_issues', 'remaining_estimate_seconds') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD remaining_estimate_seconds INT NULL;
END

IF COL_LENGTH('dbo.jira_issues', 'time_spent_seconds') IS NULL
BEGIN
    ALTER TABLE dbo.jira_issues ADD time_spent_seconds INT NULL;
END

-- General field-change history. Status transitions are excluded here and land in
-- jira_issue_status_history instead -- Atlassian's recommended structure keeps the two
-- separate, with status referencing a proper jira_statuses lookup instead of plain text.
IF OBJECT_ID('dbo.jira_issue_changelog', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_changelog (
        row_id               BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id              BIGINT          NOT NULL,
        issue_key               NVARCHAR(20)    NOT NULL,
        changelog_id              NVARCHAR(50)    NOT NULL,   -- Jira history "id"
        item_index                  INT             NOT NULL,   -- position within the history entry's items[]
        field_name                  NVARCHAR(100)   NOT NULL,   -- e.g. "priority" ("status" excluded, see above)
        field_type                    NVARCHAR(50)    NULL,       -- "jira" | "custom"
        from_value                      NVARCHAR(MAX)   NULL,       -- raw id/value
        from_string                       NVARCHAR(MAX)   NULL,       -- display value
        to_value                           NVARCHAR(MAX)   NULL,
        to_string                            NVARCHAR(MAX)   NULL,
        author_account_id                      NVARCHAR(100)   NULL,
        author_name                              NVARCHAR(255)   NULL,
        changed_at                                 DATETIME2       NOT NULL,
        etl_loaded_at                                DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_changelog_event UNIQUE (issue_id, changelog_id, item_index)
    );
    CREATE INDEX IX_jira_changelog_issue_key ON dbo.jira_issue_changelog(issue_key);
    CREATE INDEX IX_jira_changelog_field ON dbo.jira_issue_changelog(field_name);
END

IF OBJECT_ID('dbo.jira_status_categories', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_status_categories (
        category_id    INT             NOT NULL PRIMARY KEY,
        category_key   NVARCHAR(50)    NULL,   -- e.g. "new", "indeterminate", "done"
        category_name  NVARCHAR(100)   NULL,   -- e.g. "To Do", "In Progress", "Done"
        color_name     NVARCHAR(50)    NULL,
        etl_loaded_at  DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

IF OBJECT_ID('dbo.jira_statuses', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_statuses (
        status_id      NVARCHAR(50)    NOT NULL PRIMARY KEY,   -- Jira status "id"
        status_name    NVARCHAR(100)   NULL,
        category_id    INT             NULL,
        etl_loaded_at  DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_jira_statuses_category FOREIGN KEY (category_id)
            REFERENCES dbo.jira_status_categories(category_id)
    );
END

-- Populated from the same /issue/{key}/changelog entries as jira_issue_changelog, but
-- only the "status" items, with from/to as proper FKs into jira_statuses rather than
-- plain text -- this is the second table of Atlassian's recommended 2-table history split.
IF OBJECT_ID('dbo.jira_issue_status_history', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_status_history (
        row_id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id             BIGINT          NOT NULL,
        issue_key              NVARCHAR(20)    NOT NULL,
        changelog_id             NVARCHAR(50)    NOT NULL,   -- Jira history "id"
        item_index                 INT             NOT NULL,   -- position within that history entry's items[]
        from_status_id                NVARCHAR(50)    NULL,       -- NULL for an issue's first-ever status
        to_status_id                     NVARCHAR(50)    NOT NULL,
        author_account_id                   NVARCHAR(100)   NULL,
        author_name                           NVARCHAR(255)   NULL,
        changed_at                               DATETIME2       NOT NULL,
        etl_loaded_at                               DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_status_history_event UNIQUE (issue_id, changelog_id, item_index),
        CONSTRAINT FK_status_history_from FOREIGN KEY (from_status_id) REFERENCES dbo.jira_statuses(status_id),
        CONSTRAINT FK_status_history_to FOREIGN KEY (to_status_id) REFERENCES dbo.jira_statuses(status_id)
    );
    CREATE INDEX IX_jira_status_history_issue_key ON dbo.jira_issue_status_history(issue_key);
END

IF OBJECT_ID('dbo.jira_worklogs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_worklogs (
        row_id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id               BIGINT          NOT NULL,
        issue_key                NVARCHAR(20)    NOT NULL,
        worklog_id                 NVARCHAR(50)    NOT NULL,   -- Jira worklog "id"
        author_account_id             NVARCHAR(100)   NULL,
        author_name                     NVARCHAR(255)   NULL,
        time_spent_seconds                INT             NULL,
        time_spent_display                  NVARCHAR(50)    NULL,   -- e.g. "1h 30m"
        started_at                             DATETIME2       NULL,
        worklog_created                          DATETIME2       NULL,
        worklog_updated                            DATETIME2       NULL,
        comment_text                                 NVARCHAR(MAX)   NULL,
        etl_loaded_at                                   DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_worklog_entry UNIQUE (issue_id, worklog_id)
    );
    CREATE INDEX IX_jira_worklogs_issue_key ON dbo.jira_worklogs(issue_key);
END

IF OBJECT_ID('dbo.jira_comments', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_comments (
        row_id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id               BIGINT          NOT NULL,
        issue_key                NVARCHAR(20)    NOT NULL,
        comment_id                 NVARCHAR(50)    NOT NULL,   -- Jira comment "id"
        author_account_id             NVARCHAR(100)   NULL,
        author_name                     NVARCHAR(255)   NULL,
        body_text                         NVARCHAR(MAX)   NULL,   -- ADF flattened to plain text
        comment_created                     DATETIME2       NULL,
        comment_updated                       DATETIME2       NULL,
        etl_loaded_at                           DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_comment_entry UNIQUE (issue_id, comment_id)
    );
    CREATE INDEX IX_jira_comments_issue_key ON dbo.jira_comments(issue_key);
END

IF OBJECT_ID('dbo.jira_issue_links', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_links (
        row_id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id               BIGINT          NOT NULL,   -- the issue this link was seen on
        issue_key                NVARCHAR(20)    NOT NULL,
        link_id                    NVARCHAR(50)    NOT NULL,   -- Jira issuelink "id"
        link_type                    NVARCHAR(100)   NULL,       -- e.g. "Blocks"
        direction                      NVARCHAR(10)    NOT NULL,   -- "inward" | "outward"
        linked_issue_id                   BIGINT          NULL,
        linked_issue_key                    NVARCHAR(20)    NULL,
        etl_loaded_at                          DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_issuelink_entry UNIQUE (issue_id, link_id)
    );
    CREATE INDEX IX_jira_issuelinks_issue_key ON dbo.jira_issue_links(issue_key);
END

IF OBJECT_ID('dbo.jira_custom_field_definitions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_custom_field_definitions (
        field_id      NVARCHAR(50)    NOT NULL PRIMARY KEY,   -- e.g. "customfield_10057"
        field_name    NVARCHAR(255)   NULL,
        field_type    NVARCHAR(100)   NULL,                   -- schema.type, e.g. "string", "option", "array"
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

IF OBJECT_ID('dbo.jira_custom_field_values', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_custom_field_values (
        issue_id      BIGINT          NOT NULL,
        issue_key     NVARCHAR(20)    NOT NULL,
        field_id      NVARCHAR(50)    NOT NULL,   -- e.g. "customfield_10057"
        value         NVARCHAR(MAX)   NULL,       -- JSON-serialized value (matches custom_fields_json's encoding)
        value_display NVARCHAR(MAX)   NULL,       -- best-effort human-readable text, NULL if not derivable -- see transform._flatten_display_value
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_jira_custom_field_values PRIMARY KEY (issue_id, field_id)
    );
    CREATE INDEX IX_jira_custom_field_values_field ON dbo.jira_custom_field_values(field_id);
END

-- Additive, same pattern as jira_issues.due_date etc. above. `value` stays exactly as-is
-- (full fidelity, e.g. the ~37KB Approvals blobs -- see README "Open questions"); this is a
-- second, best-effort column alongside it rather than a replacement, since collapsing a
-- structured value (Sprint's array of sprint objects being the motivating example) down to
-- one string is lossy and we don't want to lose the ability to get the rest back out.
IF COL_LENGTH('dbo.jira_custom_field_values', 'value_display') IS NULL
BEGIN
    ALTER TABLE dbo.jira_custom_field_values ADD value_display NVARCHAR(MAX) NULL;
END

IF OBJECT_ID('dbo.jira_fix_versions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_fix_versions (
        version_id     BIGINT          NOT NULL PRIMARY KEY,
        version_name   NVARCHAR(255)   NULL,
        project_id     BIGINT          NULL,
        release_date   DATE            NULL,
        released       BIT             NULL,
        etl_loaded_at  DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

IF OBJECT_ID('dbo.jira_issue_fix_versions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_fix_versions (
        issue_id      BIGINT          NOT NULL,
        issue_key     NVARCHAR(20)    NOT NULL,
        version_id    BIGINT          NOT NULL,
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_jira_issue_fix_versions PRIMARY KEY (issue_id, version_id)
    );
END

IF OBJECT_ID('dbo.jira_issue_attachments', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_attachments (
        row_id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        attachment_id        NVARCHAR(50)    NOT NULL,   -- Jira attachment "id"
        issue_id               BIGINT          NOT NULL,
        issue_key                NVARCHAR(20)    NOT NULL,
        filename                   NVARCHAR(500)   NULL,
        size_bytes                   BIGINT          NULL,
        mime_type                      NVARCHAR(100)   NULL,
        author_account_id                NVARCHAR(100)   NULL,
        author_name                        NVARCHAR(255)   NULL,
        attachment_created                   DATETIME2       NULL,
        content_url                            NVARCHAR(1000)  NULL,
        etl_loaded_at                            DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_attachment_entry UNIQUE (issue_id, attachment_id)
    );
    CREATE INDEX IX_jira_attachments_issue_key ON dbo.jira_issue_attachments(issue_key);
END

-- Parsed out of jira_issues.labels (which stays as-is, a JSON array, for the raw-payload
-- convenience) so labels are actually queryable/joinable rather than trapped in a blob.
-- Current-state, not an event log: replaced wholesale per issue on each sync, same as
-- custom field values / fix versions, since Jira reports only the current label set.
IF OBJECT_ID('dbo.jira_issue_labels', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_labels (
        issue_id      BIGINT          NOT NULL,
        issue_key     NVARCHAR(20)    NOT NULL,
        label         NVARCHAR(255)   NOT NULL,
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_jira_issue_labels PRIMARY KEY (issue_id, label)
    );
    CREATE INDEX IX_jira_issue_labels_label ON dbo.jira_issue_labels(label);
END

-- Remote links (Confluence pages, web URLs, "Approved"-style relationship links) are a
-- separate Jira concept from fields.issuelinks (issue-to-issue only) and come from a
-- separate endpoint: /issue/{key}/remotelink. jira_issue_links above never captured these.
IF OBJECT_ID('dbo.jira_issue_remote_links', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_remote_links (
        row_id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id               BIGINT          NOT NULL,
        issue_key                NVARCHAR(20)    NOT NULL,
        remote_link_id              NVARCHAR(50)    NOT NULL,   -- Jira remotelink "id"
        relationship                  NVARCHAR(255)   NULL,       -- e.g. "Approved", "mentioned in"
        title                           NVARCHAR(500)   NULL,       -- object.title
        url                               NVARCHAR(1000)  NULL,       -- object.url
        global_id                           NVARCHAR(255)   NULL,
        etl_loaded_at                          DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_jira_remote_link_entry UNIQUE (issue_id, remote_link_id)
    );
    CREATE INDEX IX_jira_remote_links_issue_key ON dbo.jira_issue_remote_links(issue_key);
END

-- child_issue_id is the PK (not an IDENTITY row_id like the append-only tables above):
-- an issue has at most one parent at a time, so this is a current-state row per child,
-- replaced wholesale on each sync rather than appended to. See db.replace_hierarchy().
IF OBJECT_ID('dbo.jira_issue_hierarchy', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_hierarchy (
        child_issue_id     BIGINT          NOT NULL PRIMARY KEY,
        child_issue_key    NVARCHAR(20)    NOT NULL,
        parent_issue_id    BIGINT          NOT NULL,
        parent_issue_key   NVARCHAR(20)    NOT NULL,
        relationship_type  NVARCHAR(20)    NOT NULL,   -- 'subtask' | 'epic_child'
        etl_loaded_at      DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

-- Components carry a stable id and are reusable per project (unlike labels, which are
-- free-text with no id), so this follows the jira_fix_versions dimension+junction shape
-- rather than the flat jira_issue_labels shape. Parsed out of fields.components, which
-- jira_issues.components (JSON array) still also carries -- see transform.flatten_issue's
-- caller in main.py for why that blob column is left populated as-is for now.
IF OBJECT_ID('dbo.jira_components', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_components (
        component_id     BIGINT          NOT NULL PRIMARY KEY,
        component_name   NVARCHAR(255)   NULL,
        project_id       BIGINT          NULL,   -- no FK, same convention as jira_fix_versions.project_id
        etl_loaded_at     DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

IF OBJECT_ID('dbo.jira_issue_components', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_components (
        issue_id      BIGINT          NOT NULL,
        issue_key     NVARCHAR(20)    NOT NULL,
        component_id  BIGINT          NOT NULL,
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_jira_issue_components PRIMARY KEY (issue_id, component_id)
    );
END

-- Sprint (resolved at runtime by field name -- see main.py -- since the customfield_*
-- id for "Sprint" is instance-specific). Not used by any current report, but captured now
-- as future-proofing per product request. An issue can pass through multiple sprints over
-- its life, so this follows the jira_fix_versions dimension+junction shape.
IF OBJECT_ID('dbo.jira_sprints', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_sprints (
        sprint_id      BIGINT          NOT NULL PRIMARY KEY,
        sprint_name    NVARCHAR(255)   NULL,
        state          NVARCHAR(50)    NULL,   -- "future" | "active" | "closed"
        board_id       BIGINT          NULL,
        start_date     DATETIME2       NULL,
        end_date       DATETIME2       NULL,
        goal           NVARCHAR(MAX)   NULL,
        etl_loaded_at  DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

IF OBJECT_ID('dbo.jira_issue_sprints', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_sprints (
        issue_id      BIGINT          NOT NULL,
        issue_key     NVARCHAR(20)    NOT NULL,
        sprint_id     BIGINT          NOT NULL,
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_jira_issue_sprints PRIMARY KEY (issue_id, sprint_id)
    );
END

-- Team (resolved at runtime by field name, same reasoning as Sprint above). Unlike a
-- normal custom field, Team references an Atlassian Team object (site-wide, not
-- project-scoped) -- its value on an issue is an id pointer, not a plain option value.
-- issue_id is the PK (not an IDENTITY row_id): an issue has at most one team at a time,
-- so this is current-state, replaced wholesale per sync -- see db.replace_team().
IF OBJECT_ID('dbo.jira_issue_team', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_team (
        issue_id      BIGINT          NOT NULL PRIMARY KEY,
        issue_key     NVARCHAR(20)    NOT NULL,
        team_id       NVARCHAR(50)    NOT NULL,   -- Atlassian Team GUID, not a Jira internal numeric id
        team_name     NVARCHAR(255)   NULL,       -- see transform.extract_team_row for why this is populated directly
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

-- Supply-chain PO tracking. po_number is NOT read from a dedicated Jira field -- there is
-- one ("Order Number"), but it's populated on only ~18% of issues that actually have a PO
-- (30 of 165 confirmed live). The real PO number almost always lives as free text in the
-- issue description instead, as a "POKRM_##########" token -- see
-- transform.extract_purchase_order_row for the regex extraction. A row is only written
-- when that token is found (no row at all otherwise, not a row with po_number = NULL),
-- since the vast majority of issues never reach the PO stage.
--
-- The other columns are resolved by field NAME, not a fixed customfield_* id, same
-- reasoning as Team/Sprint/Rank -- except several of these names (Category, Project) are
-- NOT unique on this site: Jira has multiple different customfield_* ids sharing the same
-- name across different projects. transform.extract_purchase_order_row handles this by
-- checking every id sharing a name and taking whichever one is actually non-null on that
-- specific issue, rather than pinning one id -- so this table isn't limited to whatever
-- project happened to be live when these ids were last checked.
IF OBJECT_ID('dbo.jira_issue_purchase_orders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_purchase_orders (
        issue_id        BIGINT          NOT NULL PRIMARY KEY,
        issue_key       NVARCHAR(20)    NOT NULL,
        po_number       NVARCHAR(50)    NOT NULL,   -- e.g. "POKRM_0100000222", regex-extracted from the description
        order_number    NVARCHAR(255)   NULL,       -- the dedicated "Order Number" field, kept for reconciliation -- usually NULL
        total_cost      FLOAT           NULL,       -- "Total Cost ($)"
        qty             FLOAT           NULL,       -- "Qty"
        need_date       DATE            NULL,       -- "Need Date"
        po_needed       NVARCHAR(50)    NULL,       -- "PO Needed?" -- Yes/No option value
        mrp_planned     NVARCHAR(50)    NULL,       -- "MRP Planned?" option value, e.g. "Non MRP"
        category        NVARCHAR(255)   NULL,       -- "Category" option value -- name not unique site-wide, see above
        vendor_project  NVARCHAR(255)   NULL,       -- "Project" option value, e.g. "Vector - Test Facility" -- a
                                                      -- picklist custom field, distinct from the real Jira project
                                                      -- (jira_issues.project_key); name not unique site-wide either
        etl_loaded_at   DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_jira_issue_po_number ON dbo.jira_issue_purchase_orders(po_number);
END

-- ============================================================================
-- Safety metrics dashboard (MFG-264), source project: SAFE (Environmental
-- Health & Safety). Everything below is built on top of tables that already
-- exist above -- no ETL/pipeline changes were needed, since
-- jira_custom_field_values already captures every customfield_* generically
-- for every issue (SAFE included), and jira_custom_field_definitions already
-- provides the field_id -> field_name mapping (refreshed every sync via
-- client.list_fields()). The two views below join through that mapping by
-- field NAME rather than hardcoding customfield_10297/10298/10299, matching
-- this codebase's existing Team/Sprint/PO precedent -- if EHS ever recreates
-- one of these fields under a new customfield_* id, the next pipeline sync
-- updates jira_custom_field_definitions and these views keep working
-- unmodified.
-- ============================================================================

-- Static business-rule classification, NOT derived from Jira data. Confirmed
-- against the live field's actual allowedValues (via issue SAFE-85 editmeta)
-- rather than the ticket's prose, which used different casing for two values
-- ("Property damage"/"near miss") than what the field option actually stores
-- ("Property Damage"/"Near Miss") -- seeded with the real values so the join
-- in dbo.safety_reports below actually matches.
IF OBJECT_ID('dbo.safety_report_type_lookup', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.safety_report_type_lookup (
        report_type   NVARCHAR(50)  NOT NULL PRIMARY KEY,
        category      NVARCHAR(10)  NOT NULL,   -- 'Leading' | 'Lagging'
        etl_loaded_at DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

-- Guarded per-row (MERGE ... WHEN NOT MATCHED) rather than a plain INSERT
-- inside the CREATE block above, so schema.sql stays safe to re-run --
-- consistent with every other object in this file being idempotent.
MERGE dbo.safety_report_type_lookup AS t
USING (VALUES
    (N'Injury',                   N'Lagging'),
    (N'Illness',                  N'Lagging'),
    (N'Property Damage',          N'Lagging'),
    (N'First Aid',                N'Lagging'),
    (N'Near Miss',                N'Leading'),
    (N'Proactive Identification', N'Leading')
) AS s(report_type, category)
ON t.report_type = s.report_type
WHEN NOT MATCHED THEN
    INSERT (report_type, category) VALUES (s.report_type, s.category);

-- One row per SAFE "Report" issue (individual safety incident reports).
-- OUTER APPLY per field resolves customfield_* by NAME via
-- jira_custom_field_definitions (see block comment above) -- TOP 1 with an
-- IS NOT NULL filter mirrors transform._find_field_value_by_name's
-- "whichever id sharing this name is actually non-null" logic, in case this
-- site ever has more than one field sharing the same name (already true for
-- some PO-related fields elsewhere on this site, so not assumed impossible
-- here even though not observed for these 3 fields).
--
-- occurrence_date parses value_display's raw ISO-8601-with-offset string
-- (e.g. "2026-08-24T09:00:00.000-0700") via DATETIMEOFFSET, then drops to
-- DATE only, per the ticket's "ignore timestamps, date only" instruction.
-- TRY_CONVERT so a malformed/unexpected value degrades to NULL rather than
-- breaking the whole view.
--
-- days_to_report is NULL whenever occurrence_date is NULL (confirmed live:
-- 1 of 20 current SAFE Report issues has no Date Time of Occurrence set) --
-- explicitly not fabricated as 0 or defaulted to created_date, per instruction.
-- db.apply_schema() runs this whole file as a single cur.execute(script) call
-- (no sqlcmd/SSMS involved, so "GO" is not a valid batch separator here), and
-- SQL Server requires CREATE VIEW to be the only statement in its batch --
-- so both views are created via EXEC(N'...') dynamic SQL instead, the
-- standard way to conditionally (re)create a view inside a larger script
-- that must stay a single batch.
IF OBJECT_ID('dbo.safety_reports', 'V') IS NOT NULL
    DROP VIEW dbo.safety_reports;

EXEC(N'
CREATE VIEW dbo.safety_reports AS
SELECT
    i.issue_key,
    i.summary                                                                AS description,
    CAST(i.created AS DATE)                                                  AS created_date,
    i.status,
    i.status_category,
    srt.value_display                                                        AS safety_report_type,
    lut.category                                                             AS leading_or_lagging,
    osha.value_display                                                       AS osha_recordable,
    occ.occurrence_date                                                      AS occurrence_date,
    CASE
        WHEN occ.occurrence_date IS NULL THEN NULL
        ELSE DATEDIFF(DAY, occ.occurrence_date, CAST(i.created AS DATE))
    END                                                                       AS days_to_report
FROM dbo.jira_issues i
OUTER APPLY (
    SELECT TOP 1 v.value_display
    FROM dbo.jira_custom_field_values v
    JOIN dbo.jira_custom_field_definitions d ON d.field_id = v.field_id
    WHERE v.issue_id = i.issue_id AND d.field_name = N''Safety Report Type'' AND v.value_display IS NOT NULL
) srt
OUTER APPLY (
    SELECT TOP 1 v.value_display
    FROM dbo.jira_custom_field_values v
    JOIN dbo.jira_custom_field_definitions d ON d.field_id = v.field_id
    WHERE v.issue_id = i.issue_id AND d.field_name = N''OSHA Recordable'' AND v.value_display IS NOT NULL
) osha
OUTER APPLY (
    -- Jira''s raw datetime string has no colon in its UTC offset (e.g.
    -- "...-0700"), which TRY_CONVERT(DATETIMEOFFSET, ...) refuses to parse
    -- as-is (confirmed live: it silently returned NULL for every row until
    -- this STUFF fix was added) -- STUFF inserts the colon 2 chars from the
    -- end ("-0700" -> "-07:00") before conversion.
    SELECT TOP 1
        CAST(
            TRY_CONVERT(DATETIMEOFFSET(3), STUFF(v.value_display, LEN(v.value_display) - 1, 0, N'':''))
            AS DATE
        ) AS occurrence_date
    FROM dbo.jira_custom_field_values v
    JOIN dbo.jira_custom_field_definitions d ON d.field_id = v.field_id
    WHERE v.issue_id = i.issue_id AND d.field_name = N''Date Time of Occurrence'' AND v.value_display IS NOT NULL
) occ
LEFT JOIN dbo.safety_report_type_lookup lut ON lut.report_type = srt.value_display
WHERE i.project_key = N''SAFE'' AND i.issue_type = N''Report'';
');

-- One row per SAFE "Further Action" issue (open corrective/follow-up tasks).
--
-- ASSUMPTION, NOT CONFIRMED WITH EHS: is_open is derived as
-- status_category <> 'Done'. This was explicitly flagged as unconfirmed --
-- if EHS's definition of "open" excludes some non-Done status (e.g. a
-- "Cancelled"/"Won't Do" status that isn't semantically open), this
-- predicate needs revisiting once that's confirmed.
IF OBJECT_ID('dbo.further_actions', 'V') IS NOT NULL
    DROP VIEW dbo.further_actions;

EXEC(N'
CREATE VIEW dbo.further_actions AS
SELECT
    i.issue_key,
    i.summary                                                          AS description,
    i.assignee_name                                                    AS assignee,
    CAST(i.created AS DATE)                                            AS created_date,
    i.status,
    i.status_category,
    DATEDIFF(DAY, CAST(i.created AS DATE), CAST(SYSUTCDATETIME() AS DATE)) AS age_days,
    CASE WHEN i.status_category <> N''Done'' THEN 1 ELSE 0 END           AS is_open
FROM dbo.jira_issues i
WHERE i.project_key = N''SAFE'' AND i.issue_type = N''Further Action'';
');

-- Scaffold only -- intentionally left EMPTY. Do not populate until it's
-- decided whether hours-worked data comes from Gusto, Microsoft 365 payroll
-- data, or manual entry (open question in MFG-264, explicitly not this
-- codebase's call to make). Monthly grain per the ticket; hours_worked
-- should exclude PTO/sick time per the ticket once a real source is wired up.
-- TODO(MFG-264): decide source + build the actual ingestion for this table.
IF OBJECT_ID('dbo.hours_worked', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.hours_worked (
        period        DATE            NOT NULL PRIMARY KEY,   -- first day of the month
        hours_worked  FLOAT           NULL,                   -- TODO(MFG-264): populate once source is decided
        source        NVARCHAR(50)    NULL,                   -- e.g. 'Gusto' | 'Microsoft365' | 'Manual'
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END

-- Scaffold only -- intentionally left EMPTY. Do not populate with invented
-- thresholds. Needs real published OSHA/BLS TRIR benchmark data for
-- Karman's specific industry/NAICS code (open question in MFG-264).
-- TODO(MFG-264): source real green/yellow/red cut points before the TRIR
-- chart (dashboard component 1) can be built.
IF OBJECT_ID('dbo.osha_trir_benchmarks', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.osha_trir_benchmarks (
        row_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        trir_min      FLOAT           NOT NULL,   -- TODO(MFG-264): real BLS/OSHA numbers, not invented
        trir_max      FLOAT           NULL,       -- NULL = open-ended upper bound (e.g. the "Red" band)
        color_band    NVARCHAR(20)    NOT NULL,   -- 'Green' | 'Yellow' | 'Red'
        etl_loaded_at DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
