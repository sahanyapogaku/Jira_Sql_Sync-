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
