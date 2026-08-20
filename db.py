"""MSSQL connection, schema bootstrap, and batched upsert/insert logic.

jira_issues is upserted via MERGE keyed on issue_id.
jira_issue_changelog / jira_worklogs are append-only event logs loaded via an
idempotent "insert where not already present" pattern keyed on their natural
event keys, so re-running an incremental sync never duplicates rows.
"""

import logging
import os
import time
from datetime import datetime, timezone

import pyodbc
from dateutil import parser as dateparser

logger = logging.getLogger("jira_etl.db")

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
PIPELINE_LOGS_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "pipeline_logs.sql")

# SQLSTATEs indicating a dropped/unreachable connection (network blip, VPN drop, machine
# sleep) rather than a real SQL problem (bad syntax, constraint violation) -- the same
# fail-fast-vs-retry split jira_client.py already makes for 401/403 vs 429/5xx. Seen live:
# '08S01' (TCP Provider: connection forcibly closed) and '08001' (Named Pipes Provider:
# could not open a connection) after this machine's network dropped mid-sync.
TRANSIENT_SQLSTATES = {"08S01", "08001", "08S02", "HYT00", "HYT01"}
CONNECT_MAX_RETRIES = 3
CONNECT_BASE_BACKOFF_SECONDS = 5


def is_transient_connection_error(exc):
    sqlstate = exc.args[0] if isinstance(exc, pyodbc.Error) and exc.args else None
    return sqlstate in TRANSIENT_SQLSTATES

ISSUE_DATETIME_COLS = ("created", "updated", "resolutiondate")
ISSUE_DATE_COLS = ("due_date",)
CHANGELOG_DATETIME_COLS = ("changed_at",)
WORKLOG_DATETIME_COLS = ("started_at", "worklog_created", "worklog_updated")
COMMENT_DATETIME_COLS = ("comment_created", "comment_updated")


def _parse_dt(value):
    if not value:
        return None
    dt = dateparser.isoparse(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _coerce_datetimes(rows, columns):
    for row in rows:
        for col in columns:
            if col in row:
                row[col] = _parse_dt(row[col])


def _parse_date(value):
    if not value:
        return None
    return dateparser.isoparse(value).date()


def _coerce_dates(rows, columns):
    for row in rows:
        for col in columns:
            if col in row:
                row[col] = _parse_date(row[col])


def build_connection_string():
    server = os.environ["MSSQL_SERVER"]
    database = os.environ["MSSQL_DATABASE"]
    user = os.environ["MSSQL_USER"]
    password = os.environ["MSSQL_PASSWORD"]
    trust_cert = os.environ.get("MSSQL_TRUST_SERVER_CERTIFICATE", "no")
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};DATABASE={database};UID={user};PWD={password};"
        f"Encrypt=yes;TrustServerCertificate={trust_cert};"
    )


def connect():
    """Open an MSSQL connection, retrying transient failures with backoff -- mirrors
    jira_client.py's retry philosophy: a network blip right after an outage (confirmed
    live: reconnect attempts can themselves fail for a few seconds after connectivity
    nominally returns) is worth retrying briefly; anything else (bad credentials, unknown
    host) should still fail immediately rather than retry into a wall.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return pyodbc.connect(build_connection_string(), autocommit=False)
        except pyodbc.Error as exc:
            if attempt >= CONNECT_MAX_RETRIES or not is_transient_connection_error(exc):
                raise
            backoff = CONNECT_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning("Transient MSSQL connection error on connect (attempt %d/%d): %s — retrying in %ds",
                           attempt, CONNECT_MAX_RETRIES, exc, backoff)
            time.sleep(backoff)


def apply_schema(conn):
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        script = f.read()
    cur = conn.cursor()
    cur.execute(script)
    conn.commit()
    logger.info("Schema verified/applied from %s", SCHEMA_PATH)


def apply_pipeline_logs_schema(conn):
    with open(PIPELINE_LOGS_SCHEMA_PATH, "r", encoding="utf-8") as f:
        script = f.read()
    cur = conn.cursor()
    cur.execute(script)
    conn.commit()
    logger.info("pipeline_logs schema verified/applied from %s", PIPELINE_LOGS_SCHEMA_PATH)


def open_logging_connection():
    """A dedicated connection for pipeline_logs, separate from the main data connection
    used for the Jira sync itself. Kept separate so that if the main connection ends up
    mid-transaction after a failure, writing the pipeline_logs failure row here isn't
    blocked by that state. Uses the same connection details/credentials as connect() --
    see build_connection_string() -- nothing new to configure.

    Never raises: returns None (with run-level DB logging disabled for this run) and logs
    a warning if a connection can't be established or the pipeline_logs table can't be
    verified, per the "logging failure must not crash the pipeline" requirement.
    """
    try:
        conn = connect()
        apply_pipeline_logs_schema(conn)
        return conn
    except Exception:
        logger.warning("Could not establish pipeline_logs connection; run-level DB logging disabled for this run",
                        exc_info=True)
        return None


def start_pipeline_log(conn, pipeline_name):
    """Insert a 'Running' row into pipeline_logs and return its log_id, or None if this
    fails (or conn is None because open_logging_connection() already failed) -- never
    raises, per the "logging failure must not crash the pipeline" requirement.
    """
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dbo.pipeline_logs (pipeline_name, start_time, status) "
            "OUTPUT INSERTED.log_id VALUES (?, ?, ?)",
            pipeline_name, datetime.utcnow(), "Running",
        )
        log_id = cur.fetchone()[0]
        conn.commit()
        return log_id
    except Exception:
        logger.warning("Failed to write pipeline_logs start row", exc_info=True)
        return None


def finish_pipeline_log(conn, log_id, status, rows_processed=None, error_message=None):
    """Update the pipeline_logs row for this run with its outcome. No-op if conn/log_id
    are None (start already failed). Never raises -- logs a warning instead, per the
    "logging failure must not crash the pipeline" requirement.
    """
    if conn is None or log_id is None:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE dbo.pipeline_logs SET end_time = ?, status = ?, rows_processed = ?, error_message = ? "
            "WHERE log_id = ?",
            datetime.utcnow(), status, rows_processed, error_message, log_id,
        )
        conn.commit()
    except Exception:
        logger.warning("Failed to write pipeline_logs end row", exc_info=True)


ISSUE_COLUMNS = [
    "issue_id", "issue_key", "project_id", "project_key", "project_name", "issue_type", "summary",
    "status", "status_category", "priority", "assignee_account_id", "assignee_name",
    "reporter_account_id", "reporter_name", "created", "updated", "resolutiondate",
    "due_date", "environment", "security_level",
    "original_estimate_seconds", "remaining_estimate_seconds", "time_spent_seconds",
    "labels", "components", "fix_versions", "custom_fields_json", "raw_json",
]

PROJECT_COLUMNS = ["project_id", "project_key", "project_name", "project_type_key"]

FIELD_DEF_COLUMNS = ["field_id", "field_name", "field_type"]

CUSTOM_FIELD_VALUE_COLUMNS = ["issue_id", "issue_key", "field_id", "value", "value_display"]

FIX_VERSION_COLUMNS = ["version_id", "version_name", "project_id", "release_date", "released"]

ISSUE_FIX_VERSION_COLUMNS = ["issue_id", "issue_key", "version_id"]

ATTACHMENT_COLUMNS = [
    "attachment_id", "issue_id", "issue_key", "filename", "size_bytes", "mime_type",
    "author_account_id", "author_name", "attachment_created", "content_url",
]

ATTACHMENT_DATETIME_COLS = ("attachment_created",)

HIERARCHY_COLUMNS = [
    "child_issue_id", "child_issue_key", "parent_issue_id", "parent_issue_key", "relationship_type",
]

COMPONENT_COLUMNS = ["component_id", "component_name", "project_id"]

ISSUE_COMPONENT_COLUMNS = ["issue_id", "issue_key", "component_id"]

SPRINT_COLUMNS = ["sprint_id", "sprint_name", "state", "board_id", "start_date", "end_date", "goal"]

SPRINT_DATETIME_COLS = ("start_date", "end_date")

ISSUE_SPRINT_COLUMNS = ["issue_id", "issue_key", "sprint_id"]

TEAM_COLUMNS = ["issue_id", "issue_key", "team_id", "team_name"]

STATUS_CATEGORY_COLUMNS = ["category_id", "category_key", "category_name", "color_name"]

STATUS_COLUMNS = ["status_id", "status_name", "category_id"]

STATUS_HISTORY_COLUMNS = [
    "issue_id", "issue_key", "changelog_id", "item_index",
    "from_status_id", "to_status_id", "author_account_id", "author_name", "changed_at",
]

STATUS_HISTORY_DATETIME_COLS = ("changed_at",)

CHANGELOG_COLUMNS = [
    "issue_id", "issue_key", "changelog_id", "item_index", "field_name", "field_type",
    "from_value", "from_string", "to_value", "to_string",
    "author_account_id", "author_name", "changed_at",
]

WORKLOG_COLUMNS = [
    "issue_id", "issue_key", "worklog_id", "author_account_id", "author_name",
    "time_spent_seconds", "time_spent_display", "started_at",
    "worklog_created", "worklog_updated", "comment_text",
]

COMMENT_COLUMNS = [
    "issue_id", "issue_key", "comment_id", "author_account_id", "author_name",
    "body_text", "comment_created", "comment_updated",
]

ISSUELINK_COLUMNS = [
    "issue_id", "issue_key", "link_id", "link_type", "direction",
    "linked_issue_id", "linked_issue_key",
]

LABEL_COLUMNS = ["issue_id", "issue_key", "label"]

REMOTE_LINK_COLUMNS = [
    "issue_id", "issue_key", "remote_link_id", "relationship", "title", "url", "global_id",
]


def _rows_to_tuples(rows, columns):
    return [tuple(row.get(c) for c in columns) for row in rows]


def upsert_issues(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, ISSUE_DATETIME_COLS)
    _coerce_dates(rows, ISSUE_DATE_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_issues') IS NOT NULL DROP TABLE #stg_issues;
        SELECT TOP 0 * INTO #stg_issues FROM dbo.jira_issues;
        ALTER TABLE #stg_issues ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in ISSUE_COLUMNS)
    insert_sql = f"INSERT INTO #stg_issues ({', '.join(ISSUE_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, ISSUE_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in ISSUE_COLUMNS if c != "issue_id")
    insert_cols = ", ".join(ISSUE_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in ISSUE_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_issues AS t
        USING #stg_issues AS s
        ON t.issue_id = s.issue_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d issue rows", len(rows))
    return len(rows)


def insert_changelog(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, CHANGELOG_DATETIME_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_changelog') IS NOT NULL DROP TABLE #stg_changelog;
        SELECT TOP 0 * INTO #stg_changelog FROM dbo.jira_issue_changelog;
        ALTER TABLE #stg_changelog ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in CHANGELOG_COLUMNS)
    insert_sql = f"INSERT INTO #stg_changelog ({', '.join(CHANGELOG_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, CHANGELOG_COLUMNS))

    cols = ", ".join(CHANGELOG_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_issue_changelog ({cols})
        SELECT {cols} FROM #stg_changelog s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_issue_changelog t
            WHERE t.issue_id = s.issue_id
              AND t.changelog_id = s.changelog_id
              AND t.item_index = s.item_index
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new changelog rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def insert_worklogs(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, WORKLOG_DATETIME_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_worklogs') IS NOT NULL DROP TABLE #stg_worklogs;
        SELECT TOP 0 * INTO #stg_worklogs FROM dbo.jira_worklogs;
        ALTER TABLE #stg_worklogs ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in WORKLOG_COLUMNS)
    insert_sql = f"INSERT INTO #stg_worklogs ({', '.join(WORKLOG_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, WORKLOG_COLUMNS))

    cols = ", ".join(WORKLOG_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_worklogs ({cols})
        SELECT {cols} FROM #stg_worklogs s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_worklogs t
            WHERE t.issue_id = s.issue_id
              AND t.worklog_id = s.worklog_id
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new worklog rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def insert_comments(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, COMMENT_DATETIME_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_comments') IS NOT NULL DROP TABLE #stg_comments;
        SELECT TOP 0 * INTO #stg_comments FROM dbo.jira_comments;
        ALTER TABLE #stg_comments ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in COMMENT_COLUMNS)
    insert_sql = f"INSERT INTO #stg_comments ({', '.join(COMMENT_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, COMMENT_COLUMNS))

    cols = ", ".join(COMMENT_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_comments ({cols})
        SELECT {cols} FROM #stg_comments s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_comments t
            WHERE t.issue_id = s.issue_id
              AND t.comment_id = s.comment_id
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new comment rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def insert_issuelinks(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_issuelinks') IS NOT NULL DROP TABLE #stg_issuelinks;
        SELECT TOP 0 * INTO #stg_issuelinks FROM dbo.jira_issue_links;
        ALTER TABLE #stg_issuelinks ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in ISSUELINK_COLUMNS)
    insert_sql = f"INSERT INTO #stg_issuelinks ({', '.join(ISSUELINK_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, ISSUELINK_COLUMNS))

    cols = ", ".join(ISSUELINK_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_issue_links ({cols})
        SELECT {cols} FROM #stg_issuelinks s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_issue_links t
            WHERE t.issue_id = s.issue_id
              AND t.link_id = s.link_id
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new issue link rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def insert_remote_links(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_remote_links') IS NOT NULL DROP TABLE #stg_remote_links;
        SELECT TOP 0 * INTO #stg_remote_links FROM dbo.jira_issue_remote_links;
        ALTER TABLE #stg_remote_links ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in REMOTE_LINK_COLUMNS)
    insert_sql = f"INSERT INTO #stg_remote_links ({', '.join(REMOTE_LINK_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, REMOTE_LINK_COLUMNS))

    cols = ", ".join(REMOTE_LINK_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_issue_remote_links ({cols})
        SELECT {cols} FROM #stg_remote_links s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_issue_remote_links t
            WHERE t.issue_id = s.issue_id
              AND t.remote_link_id = s.remote_link_id
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new remote link rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def upsert_status_categories(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_status_categories') IS NOT NULL DROP TABLE #stg_status_categories;
        SELECT TOP 0 * INTO #stg_status_categories FROM dbo.jira_status_categories;
        ALTER TABLE #stg_status_categories ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in STATUS_CATEGORY_COLUMNS)
    insert_sql = f"INSERT INTO #stg_status_categories ({', '.join(STATUS_CATEGORY_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, STATUS_CATEGORY_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in STATUS_CATEGORY_COLUMNS if c != "category_id")
    insert_cols = ", ".join(STATUS_CATEGORY_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in STATUS_CATEGORY_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_status_categories AS t
        USING #stg_status_categories AS s
        ON t.category_id = s.category_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d status category rows", len(rows))
    return len(rows)


def upsert_statuses(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_statuses') IS NOT NULL DROP TABLE #stg_statuses;
        SELECT TOP 0 * INTO #stg_statuses FROM dbo.jira_statuses;
        ALTER TABLE #stg_statuses ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in STATUS_COLUMNS)
    insert_sql = f"INSERT INTO #stg_statuses ({', '.join(STATUS_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, STATUS_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in STATUS_COLUMNS if c != "status_id")
    insert_cols = ", ".join(STATUS_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in STATUS_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_statuses AS t
        USING #stg_statuses AS s
        ON t.status_id = s.status_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d status rows", len(rows))
    return len(rows)


def insert_status_history(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, STATUS_HISTORY_DATETIME_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_status_history') IS NOT NULL DROP TABLE #stg_status_history;
        SELECT TOP 0 * INTO #stg_status_history FROM dbo.jira_issue_status_history;
        ALTER TABLE #stg_status_history ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in STATUS_HISTORY_COLUMNS)
    insert_sql = f"INSERT INTO #stg_status_history ({', '.join(STATUS_HISTORY_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, STATUS_HISTORY_COLUMNS))

    cols = ", ".join(STATUS_HISTORY_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_issue_status_history ({cols})
        SELECT {cols} FROM #stg_status_history s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_issue_status_history t
            WHERE t.issue_id = s.issue_id
              AND t.changelog_id = s.changelog_id
              AND t.item_index = s.item_index
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new status history rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def upsert_projects(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_projects') IS NOT NULL DROP TABLE #stg_projects;
        SELECT TOP 0 * INTO #stg_projects FROM dbo.jira_project;
        ALTER TABLE #stg_projects ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in PROJECT_COLUMNS)
    insert_sql = f"INSERT INTO #stg_projects ({', '.join(PROJECT_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, PROJECT_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in PROJECT_COLUMNS if c != "project_id")
    insert_cols = ", ".join(PROJECT_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in PROJECT_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_project AS t
        USING #stg_projects AS s
        ON t.project_id = s.project_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d project rows", len(rows))
    return len(rows)


def upsert_field_definitions(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_field_defs') IS NOT NULL DROP TABLE #stg_field_defs;
        SELECT TOP 0 * INTO #stg_field_defs FROM dbo.jira_custom_field_definitions;
        ALTER TABLE #stg_field_defs ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in FIELD_DEF_COLUMNS)
    insert_sql = f"INSERT INTO #stg_field_defs ({', '.join(FIELD_DEF_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, FIELD_DEF_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in FIELD_DEF_COLUMNS if c != "field_id")
    insert_cols = ", ".join(FIELD_DEF_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in FIELD_DEF_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_custom_field_definitions AS t
        USING #stg_field_defs AS s
        ON t.field_id = s.field_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d custom field definition rows", len(rows))
    return len(rows)


def upsert_fix_versions(conn, rows):
    if not rows:
        return 0
    _coerce_dates(rows, ("release_date",))

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_fix_versions') IS NOT NULL DROP TABLE #stg_fix_versions;
        SELECT TOP 0 * INTO #stg_fix_versions FROM dbo.jira_fix_versions;
        ALTER TABLE #stg_fix_versions ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in FIX_VERSION_COLUMNS)
    insert_sql = f"INSERT INTO #stg_fix_versions ({', '.join(FIX_VERSION_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, FIX_VERSION_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in FIX_VERSION_COLUMNS if c != "version_id")
    insert_cols = ", ".join(FIX_VERSION_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in FIX_VERSION_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_fix_versions AS t
        USING #stg_fix_versions AS s
        ON t.version_id = s.version_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d fix version rows", len(rows))
    return len(rows)


def upsert_components(conn, rows):
    if not rows:
        return 0

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_components') IS NOT NULL DROP TABLE #stg_components;
        SELECT TOP 0 * INTO #stg_components FROM dbo.jira_components;
        ALTER TABLE #stg_components ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in COMPONENT_COLUMNS)
    insert_sql = f"INSERT INTO #stg_components ({', '.join(COMPONENT_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, COMPONENT_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in COMPONENT_COLUMNS if c != "component_id")
    insert_cols = ", ".join(COMPONENT_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in COMPONENT_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_components AS t
        USING #stg_components AS s
        ON t.component_id = s.component_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d component rows", len(rows))
    return len(rows)


def upsert_sprints(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, SPRINT_DATETIME_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_sprints') IS NOT NULL DROP TABLE #stg_sprints;
        SELECT TOP 0 * INTO #stg_sprints FROM dbo.jira_sprints;
        ALTER TABLE #stg_sprints ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in SPRINT_COLUMNS)
    insert_sql = f"INSERT INTO #stg_sprints ({', '.join(SPRINT_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, SPRINT_COLUMNS))

    set_clause = ", ".join(f"t.{c} = s.{c}" for c in SPRINT_COLUMNS if c != "sprint_id")
    insert_cols = ", ".join(SPRINT_COLUMNS)
    insert_vals = ", ".join(f"s.{c}" for c in SPRINT_COLUMNS)
    cur.execute(f"""
        MERGE dbo.jira_sprints AS t
        USING #stg_sprints AS s
        ON t.sprint_id = s.sprint_id
        WHEN MATCHED THEN
            UPDATE SET {set_clause}, t.etl_loaded_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """)
    conn.commit()
    logger.info("Upserted %d sprint rows", len(rows))
    return len(rows)


def insert_attachments(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, ATTACHMENT_DATETIME_COLS)

    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('tempdb..#stg_attachments') IS NOT NULL DROP TABLE #stg_attachments;
        SELECT TOP 0 * INTO #stg_attachments FROM dbo.jira_issue_attachments;
        ALTER TABLE #stg_attachments ALTER COLUMN etl_loaded_at DATETIME2 NULL;
    """)
    placeholders = ", ".join("?" for _ in ATTACHMENT_COLUMNS)
    insert_sql = f"INSERT INTO #stg_attachments ({', '.join(ATTACHMENT_COLUMNS)}) VALUES ({placeholders})"
    cur.executemany(insert_sql, _rows_to_tuples(rows, ATTACHMENT_COLUMNS))

    cols = ", ".join(ATTACHMENT_COLUMNS)
    cur.execute(f"""
        INSERT INTO dbo.jira_issue_attachments ({cols})
        SELECT {cols} FROM #stg_attachments s
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.jira_issue_attachments t
            WHERE t.issue_id = s.issue_id
              AND t.attachment_id = s.attachment_id
        );
    """)
    inserted = cur.rowcount
    conn.commit()
    logger.info("Inserted %d new attachment rows (%d in batch, rest already present)", inserted, len(rows))
    return inserted


def replace_custom_field_values(conn, issue_ids, rows):
    """Replace custom-field values for the given issue_ids with the freshly extracted set.

    Deviates from the append-only insert-where-not-exists pattern used for changelog /
    worklogs / comments / issuelinks above: a custom field's value is current-state data
    (it can change or be cleared on re-sync), not an immutable historical event, so stale
    rows for a re-synced issue must be removed here, not just added to.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_custom_field_values WHERE issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_cfv') IS NOT NULL DROP TABLE #stg_cfv;
            SELECT TOP 0 * INTO #stg_cfv FROM dbo.jira_custom_field_values;
            ALTER TABLE #stg_cfv ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in CUSTOM_FIELD_VALUE_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_cfv ({', '.join(CUSTOM_FIELD_VALUE_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, CUSTOM_FIELD_VALUE_COLUMNS),
        )
        cols = ", ".join(CUSTOM_FIELD_VALUE_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_custom_field_values ({cols}) SELECT {cols} FROM #stg_cfv")

    conn.commit()
    logger.info("Replaced custom field values for %d issues (%d values loaded)", len(issue_ids), len(rows))
    return len(rows)


def replace_labels(conn, issue_ids, rows):
    """Replace labels for the given issue_ids with the freshly extracted set. Same
    rationale as replace_custom_field_values: labels are current-state, not an
    immutable historical event, so stale rows for a re-synced issue must be cleared.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_issue_labels WHERE issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_labels') IS NOT NULL DROP TABLE #stg_labels;
            SELECT TOP 0 * INTO #stg_labels FROM dbo.jira_issue_labels;
            ALTER TABLE #stg_labels ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in LABEL_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_labels ({', '.join(LABEL_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, LABEL_COLUMNS),
        )
        cols = ", ".join(LABEL_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_issue_labels ({cols}) SELECT {cols} FROM #stg_labels")

    conn.commit()
    logger.info("Replaced labels for %d issues (%d labels loaded)", len(issue_ids), len(rows))
    return len(rows)


def replace_issue_fix_versions(conn, issue_ids, rows):
    """Replace fix-version links for the given issue_ids. Same rationale as
    replace_custom_field_values: fix versions can be added or removed from an issue on
    re-sync, so this clears stale links rather than only appending new ones.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_issue_fix_versions WHERE issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_ifv') IS NOT NULL DROP TABLE #stg_ifv;
            SELECT TOP 0 * INTO #stg_ifv FROM dbo.jira_issue_fix_versions;
            ALTER TABLE #stg_ifv ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in ISSUE_FIX_VERSION_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_ifv ({', '.join(ISSUE_FIX_VERSION_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, ISSUE_FIX_VERSION_COLUMNS),
        )
        cols = ", ".join(ISSUE_FIX_VERSION_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_issue_fix_versions ({cols}) SELECT {cols} FROM #stg_ifv")

    conn.commit()
    logger.info("Replaced fix-version links for %d issues (%d links loaded)", len(issue_ids), len(rows))
    return len(rows)


def replace_issue_components(conn, issue_ids, rows):
    """Replace component links for the given issue_ids. Same rationale as
    replace_issue_fix_versions: an issue's component set can change on re-sync, so this
    clears stale links rather than only appending new ones.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_issue_components WHERE issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_ic') IS NOT NULL DROP TABLE #stg_ic;
            SELECT TOP 0 * INTO #stg_ic FROM dbo.jira_issue_components;
            ALTER TABLE #stg_ic ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in ISSUE_COMPONENT_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_ic ({', '.join(ISSUE_COMPONENT_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, ISSUE_COMPONENT_COLUMNS),
        )
        cols = ", ".join(ISSUE_COMPONENT_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_issue_components ({cols}) SELECT {cols} FROM #stg_ic")

    conn.commit()
    logger.info("Replaced component links for %d issues (%d links loaded)", len(issue_ids), len(rows))
    return len(rows)


def replace_issue_sprints(conn, issue_ids, rows):
    """Replace sprint links for the given issue_ids. Same rationale as
    replace_issue_fix_versions: an issue's sprint history can grow (or be corrected) on
    re-sync, so this clears stale links rather than only appending new ones.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_issue_sprints WHERE issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_is') IS NOT NULL DROP TABLE #stg_is;
            SELECT TOP 0 * INTO #stg_is FROM dbo.jira_issue_sprints;
            ALTER TABLE #stg_is ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in ISSUE_SPRINT_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_is ({', '.join(ISSUE_SPRINT_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, ISSUE_SPRINT_COLUMNS),
        )
        cols = ", ".join(ISSUE_SPRINT_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_issue_sprints ({cols}) SELECT {cols} FROM #stg_is")

    conn.commit()
    logger.info("Replaced sprint links for %d issues (%d links loaded)", len(issue_ids), len(rows))
    return len(rows)


def replace_team(conn, issue_ids, rows):
    """Replace the team link for the given issue_ids. Same rationale as replace_hierarchy:
    an issue's team can change or be cleared on re-sync, and an issue has at most one team
    at a time, so stale rows must be cleared rather than only appended to.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_issue_team WHERE issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_team') IS NOT NULL DROP TABLE #stg_team;
            SELECT TOP 0 * INTO #stg_team FROM dbo.jira_issue_team;
            ALTER TABLE #stg_team ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in TEAM_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_team ({', '.join(TEAM_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, TEAM_COLUMNS),
        )
        cols = ", ".join(TEAM_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_issue_team ({cols}) SELECT {cols} FROM #stg_team")

    conn.commit()
    logger.info("Replaced team links for %d issues (%d teams loaded)", len(issue_ids), len(rows))
    return len(rows)


def replace_hierarchy(conn, issue_ids, rows):
    """Replace parent/child hierarchy rows for the given issue_ids (as children). Same
    rationale: an issue's parent can change or be removed on re-sync, so stale rows must
    be cleared rather than only appended to.
    """
    if not issue_ids:
        return 0
    cur = conn.cursor()
    placeholders = ", ".join("?" for _ in issue_ids)
    cur.execute(f"DELETE FROM dbo.jira_issue_hierarchy WHERE child_issue_id IN ({placeholders})", issue_ids)

    if rows:
        cur.execute("""
            IF OBJECT_ID('tempdb..#stg_hierarchy') IS NOT NULL DROP TABLE #stg_hierarchy;
            SELECT TOP 0 * INTO #stg_hierarchy FROM dbo.jira_issue_hierarchy;
            ALTER TABLE #stg_hierarchy ALTER COLUMN etl_loaded_at DATETIME2 NULL;
        """)
        ph = ", ".join("?" for _ in HIERARCHY_COLUMNS)
        cur.executemany(
            f"INSERT INTO #stg_hierarchy ({', '.join(HIERARCHY_COLUMNS)}) VALUES ({ph})",
            _rows_to_tuples(rows, HIERARCHY_COLUMNS),
        )
        cols = ", ".join(HIERARCHY_COLUMNS)
        cur.execute(f"INSERT INTO dbo.jira_issue_hierarchy ({cols}) SELECT {cols} FROM #stg_hierarchy")

    conn.commit()
    logger.info("Replaced hierarchy rows for %d issues (%d parent links loaded)", len(issue_ids), len(rows))
    return len(rows)
