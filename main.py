"""CLI entrypoint for the Jira -> MSSQL ETL pipeline.

Usage:
    python main.py --incremental              # default: issues updated in last 1 day
    python main.py --incremental --days 7      # issues updated in last 7 days
    python main.py --full                      # every issue across every project
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

import db
import transform
from jira_client import JiraAuthError, JiraAPIError, JiraClient

logger = logging.getLogger("jira_etl")

BATCH_SIZE = 200


def setup_logging(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def build_jql(full, days):
    if full:
        return 'updated >= "1900-01-01 00:00" ORDER BY updated ASC'
    return f'updated >= -{days}d ORDER BY updated ASC'


def run(jql, conn, client):
    issue_batch = []
    changelog_batch = []
    worklog_batch = []
    comment_batch = []
    issuelink_batch = []

    total_issues = 0
    total_changelog = 0
    total_worklogs = 0
    total_comments = 0
    total_issuelinks = 0

    def flush():
        nonlocal issue_batch, changelog_batch, worklog_batch, comment_batch, issuelink_batch
        nonlocal total_changelog, total_worklogs, total_comments, total_issuelinks
        if issue_batch:
            db.upsert_issues(conn, issue_batch)
            issue_batch = []
        if changelog_batch:
            total_changelog += db.insert_changelog(conn, changelog_batch)
            changelog_batch = []
        if worklog_batch:
            total_worklogs += db.insert_worklogs(conn, worklog_batch)
            worklog_batch = []
        if comment_batch:
            total_comments += db.insert_comments(conn, comment_batch)
            comment_batch = []
        if issuelink_batch:
            total_issuelinks += db.insert_issuelinks(conn, issuelink_batch)
            issuelink_batch = []

    for issue in client.search_issues(jql, fields="*all"):
        issue_id = int(issue["id"])
        issue_key = issue["key"]

        issue_batch.append(transform.flatten_issue(issue))
        issuelink_batch.extend(transform.extract_issuelink_rows(issue_id, issue_key, issue.get("fields", {})))
        total_issues += 1

        try:
            for history_entry in client.get_changelog(issue_key):
                changelog_batch.extend(transform.extract_changelog_rows(issue_id, issue_key, history_entry))
        except JiraAPIError as exc:
            logger.warning("Skipping changelog for %s after repeated failures: %s", issue_key, exc)

        try:
            for worklog_entry in client.get_worklogs(issue_key):
                worklog_batch.append(transform.extract_worklog_row(issue_id, issue_key, worklog_entry))
        except JiraAPIError as exc:
            logger.warning("Skipping worklogs for %s after repeated failures: %s", issue_key, exc)

        try:
            for comment_entry in client.get_comments(issue_key):
                comment_batch.append(transform.extract_comment_row(issue_id, issue_key, comment_entry))
        except JiraAPIError as exc:
            logger.warning("Skipping comments for %s after repeated failures: %s", issue_key, exc)

        if len(issue_batch) >= BATCH_SIZE:
            flush()
            logger.info("Progress: %d issues processed so far", total_issues)

    flush()
    logger.info(
        "Run complete: %d issues, %d changelog events, %d worklog entries, "
        "%d comments, %d issue links loaded",
        total_issues, total_changelog, total_worklogs, total_comments, total_issuelinks,
    )


def main():
    parser = argparse.ArgumentParser(description="Jira Cloud -> MSSQL ETL pipeline")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="Full sync: every issue across every project")
    mode.add_argument("--incremental", action="store_true", help="Incremental sync (default mode)")
    parser.add_argument("--days", type=int, default=1, help="Incremental window in days (default: 1)")
    parser.add_argument("--log-file", default="jira_etl.log", help="Path to log file")
    args = parser.parse_args()

    setup_logging(args.log_file)
    load_dotenv()

    required_env = ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
                     "MSSQL_SERVER", "MSSQL_DATABASE", "MSSQL_USER", "MSSQL_PASSWORD"]
    missing = [v for v in required_env if not os.environ.get(v)]
    if missing:
        logger.critical("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    full = args.full
    jql = build_jql(full=full, days=args.days)
    logger.info("Mode: %s | JQL: %s", "FULL" if full else f"INCREMENTAL ({args.days}d)", jql)

    client = JiraClient(os.environ["JIRA_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

    try:
        conn = db.connect()
    except Exception:
        logger.critical("Failed to connect to MSSQL", exc_info=True)
        sys.exit(1)

    try:
        db.apply_schema(conn)

        projects = client.list_projects()
        logger.info("Scope: ALL %d discovered projects (no project filter applied to JQL)", len(projects))

        run(jql, conn, client)

    except JiraAuthError as exc:
        logger.critical("Jira authentication failed: %s", exc)
        sys.exit(1)
    except JiraAPIError as exc:
        logger.critical("Unrecoverable Jira API error: %s", exc)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
