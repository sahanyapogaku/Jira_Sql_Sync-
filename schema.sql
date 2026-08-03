-- Jira -> MSSQL analytics schema
-- Idempotent: safe to run every startup, only creates objects that don't exist yet.

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

IF OBJECT_ID('dbo.jira_issue_changelog', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.jira_issue_changelog (
        row_id               BIGINT IDENTITY(1,1) PRIMARY KEY,
        issue_id              BIGINT          NOT NULL,
        issue_key               NVARCHAR(20)    NOT NULL,
        changelog_id              NVARCHAR(50)    NOT NULL,   -- Jira history "id"
        item_index                  INT             NOT NULL,   -- position within the history entry's items[]
        field_name                  NVARCHAR(100)   NOT NULL,   -- e.g. "status"
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
