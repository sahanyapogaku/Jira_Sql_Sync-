"""MSSQL connection, schema bootstrap, and batched upsert/insert logic.

jira_issues is upserted via MERGE keyed on issue_id.
jira_issue_changelog / jira_worklogs are append-only event logs loaded via an
idempotent "insert where not already present" pattern keyed on their natural
event keys, so re-running an incremental sync never duplicates rows.
"""

import logging
import os
from datetime import timezone

import pyodbc
from dateutil import parser as dateparser

logger = logging.getLogger("jira_etl.db")

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

ISSUE_DATETIME_COLS = ("created", "updated", "resolutiondate")
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
    conn = pyodbc.connect(build_connection_string(), autocommit=False)
    return conn


def apply_schema(conn):
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        script = f.read()
    cur = conn.cursor()
    cur.execute(script)
    conn.commit()
    logger.info("Schema verified/applied from %s", SCHEMA_PATH)


ISSUE_COLUMNS = [
    "issue_id", "issue_key", "project_key", "project_name", "issue_type", "summary",
    "status", "status_category", "priority", "assignee_account_id", "assignee_name",
    "reporter_account_id", "reporter_name", "created", "updated", "resolutiondate",
    "labels", "components", "fix_versions", "custom_fields_json", "raw_json",
]

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


def _rows_to_tuples(rows, columns):
    return [tuple(row.get(c) for c in columns) for row in rows]


def upsert_issues(conn, rows):
    if not rows:
        return 0
    _coerce_datetimes(rows, ISSUE_DATETIME_COLS)

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
