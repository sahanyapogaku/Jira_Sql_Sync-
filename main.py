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
import time

from dotenv import load_dotenv

import db
import transform
from jira_client import JiraAuthError, JiraAPIError, JiraClient

logger = logging.getLogger("jira_etl")

BATCH_SIZE = 200
PIPELINE_NAME = "jira_etl"

# A transient MSSQL connection drop (network blip, VPN drop, machine sleep -- confirmed
# live: this pipeline has hit both) kills whatever connection was open mid-write with no
# way to resume that exact write. Retrying the whole run from scratch is safe (every write
# here is idempotent -- see README's "Data model" section) even if wasteful for a --full
# sync that was most of the way through. Not retried for JiraAuthError/JiraAPIError or any
# non-transient error -- those fail fast, same as before.
DB_RETRY_MAX_ATTEMPTS = 3
DB_RETRY_BACKOFF_SECONDS = 30


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


def resolve_field_id(field_defs, field_name):
    """Look up a customfield_* id by its display name (from client.list_fields()).

    Used for Team/Sprint instead of hardcoding e.g. customfield_10020, since these ids
    are assigned per Jira site and would silently break the mapping if this pipeline is
    ever pointed at a different site. Returns None (with capture disabled for that field)
    if no field with this name exists here.
    """
    return next((f["id"] for f in field_defs if f.get("name") == field_name), None)


def run(jql, conn, client, known_status_ids, team_field_id, sprint_field_id):
    issue_batch = []
    project_batch = {}
    changelog_batch = []
    status_history_batch = []
    ghost_status_batch = []
    worklog_batch = []
    comment_batch = []
    issuelink_batch = []
    remotelink_batch = []
    customfieldvalue_batch = []
    fixversion_batch = {}
    issue_fixversion_batch = []
    attachment_batch = []
    hierarchy_batch = []
    label_batch = []
    component_batch = {}
    issue_component_batch = []
    sprint_batch = {}
    issue_sprint_batch = []
    team_batch = []
    batch_issue_ids = []
    exclude_custom_field_ids = {team_field_id} if team_field_id else None

    total_issues = 0
    total_changelog = 0
    total_status_history = 0
    total_worklogs = 0
    total_comments = 0
    total_issuelinks = 0
    total_remotelinks = 0
    total_attachments = 0

    def flush():
        nonlocal issue_batch, project_batch, changelog_batch, status_history_batch, ghost_status_batch
        nonlocal worklog_batch, comment_batch, issuelink_batch, remotelink_batch
        nonlocal customfieldvalue_batch, fixversion_batch, issue_fixversion_batch, attachment_batch, hierarchy_batch
        nonlocal label_batch, batch_issue_ids
        nonlocal component_batch, issue_component_batch, sprint_batch, issue_sprint_batch, team_batch
        nonlocal total_changelog, total_status_history, total_worklogs, total_comments, total_issuelinks
        nonlocal total_remotelinks, total_attachments
        if project_batch:
            # Must upsert before issue_batch: jira_issues.project_id has an FK to jira_project.
            db.upsert_projects(conn, list(project_batch.values()))
            project_batch = {}
        if issue_batch:
            db.upsert_issues(conn, issue_batch)
            issue_batch = []
        if changelog_batch:
            total_changelog += db.insert_changelog(conn, changelog_batch)
            changelog_batch = []
        if ghost_status_batch:
            # Statuses that have since been deleted from the workflow: gone from Jira's
            # live /status list (and even a direct by-id lookup 404s), but changelog
            # history still references them by id. Must upsert before status_history_batch:
            # from/to_status_id are FKs into jira_statuses.
            db.upsert_statuses(conn, ghost_status_batch)
            ghost_status_batch = []
        if status_history_batch:
            total_status_history += db.insert_status_history(conn, status_history_batch)
            status_history_batch = []
        if worklog_batch:
            total_worklogs += db.insert_worklogs(conn, worklog_batch)
            worklog_batch = []
        if comment_batch:
            total_comments += db.insert_comments(conn, comment_batch)
            comment_batch = []
        if issuelink_batch:
            total_issuelinks += db.insert_issuelinks(conn, issuelink_batch)
            issuelink_batch = []
        if remotelink_batch:
            total_remotelinks += db.insert_remote_links(conn, remotelink_batch)
            remotelink_batch = []
        if fixversion_batch:
            db.upsert_fix_versions(conn, list(fixversion_batch.values()))
            fixversion_batch = {}
        if component_batch:
            # Must upsert before batch_issue_ids block below: jira_issue_components.component_id
            # has no FK, but follows the same dimension-before-junction ordering as fix versions.
            db.upsert_components(conn, list(component_batch.values()))
            component_batch = {}
        if sprint_batch:
            db.upsert_sprints(conn, list(sprint_batch.values()))
            sprint_batch = {}
        if attachment_batch:
            total_attachments += db.insert_attachments(conn, attachment_batch)
            attachment_batch = []
        if batch_issue_ids:
            db.replace_custom_field_values(conn, batch_issue_ids, customfieldvalue_batch)
            db.replace_issue_fix_versions(conn, batch_issue_ids, issue_fixversion_batch)
            db.replace_hierarchy(conn, batch_issue_ids, hierarchy_batch)
            db.replace_labels(conn, batch_issue_ids, label_batch)
            db.replace_issue_components(conn, batch_issue_ids, issue_component_batch)
            db.replace_issue_sprints(conn, batch_issue_ids, issue_sprint_batch)
            db.replace_team(conn, batch_issue_ids, team_batch)
            customfieldvalue_batch = []
            issue_fixversion_batch = []
            hierarchy_batch = []
            label_batch = []
            issue_component_batch = []
            issue_sprint_batch = []
            team_batch = []
            batch_issue_ids = []

    for issue in client.search_issues(jql, fields="*all"):
        issue_id = int(issue["id"])
        issue_key = issue["key"]
        fields = issue.get("fields", {})

        issue_batch.append(transform.flatten_issue(issue, exclude_custom_field_ids=exclude_custom_field_ids))
        batch_issue_ids.append(issue_id)

        project_row = transform.extract_project_row(fields)
        if project_row:
            project_batch[project_row["project_id"]] = project_row

        issuelink_batch.extend(transform.extract_issuelink_rows(issue_id, issue_key, fields))
        customfieldvalue_batch.extend(transform.extract_custom_field_value_rows(issue_id, issue_key, fields))
        attachment_batch.extend(transform.extract_attachment_rows(issue_id, issue_key, fields))
        issue_fixversion_batch.extend(transform.extract_issue_fix_version_links(issue_id, issue_key, fields))
        label_batch.extend(transform.extract_label_rows(issue_id, issue_key, fields))
        for fv_row in transform.extract_fix_version_rows(fields):
            fixversion_batch[fv_row["version_id"]] = fv_row
        hierarchy_row = transform.extract_hierarchy_row(issue_id, issue_key, fields)
        if hierarchy_row:
            hierarchy_batch.append(hierarchy_row)

        for comp_row in transform.extract_component_rows(fields):
            component_batch[comp_row["component_id"]] = comp_row
        issue_component_batch.extend(transform.extract_issue_component_links(issue_id, issue_key, fields))

        for sprint_row in transform.extract_sprint_rows(fields, sprint_field_id):
            sprint_batch[sprint_row["sprint_id"]] = sprint_row
        issue_sprint_batch.extend(transform.extract_issue_sprint_links(issue_id, issue_key, fields, sprint_field_id))

        team_row = transform.extract_team_row(issue_id, issue_key, fields, team_field_id)
        if team_row:
            team_batch.append(team_row)

        total_issues += 1

        try:
            for history_entry in client.get_changelog(issue_key):
                changelog_batch.extend(transform.extract_changelog_rows(issue_id, issue_key, history_entry))
                for row in transform.extract_status_history_rows(issue_id, issue_key, history_entry):
                    for sid, sname in (
                        (row["from_status_id"], row.pop("from_status_name")),
                        (row["to_status_id"], row.pop("to_status_name")),
                    ):
                        if sid and sid not in known_status_ids:
                            known_status_ids.add(sid)
                            ghost_status_batch.append({
                                "status_id": sid, "status_name": sname, "category_id": None,
                            })
                    status_history_batch.append(row)
        except JiraAPIError as exc:
            logger.warning("Skipping changelog for %s after repeated failures: %s", issue_key, exc)

        try:
            remotelink_batch.extend(
                transform.extract_remote_link_rows(issue_id, issue_key, client.get_remote_links(issue_key))
            )
        except JiraAPIError as exc:
            logger.warning("Skipping remote links for %s after repeated failures: %s", issue_key, exc)

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
        "Run complete: %d issues, %d changelog events, %d status transitions, %d worklog entries, "
        "%d comments, %d issue links, %d remote links, %d attachments loaded",
        total_issues, total_changelog, total_status_history, total_worklogs,
        total_comments, total_issuelinks, total_remotelinks, total_attachments,
    )
    return total_issues


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

    for attempt in range(1, DB_RETRY_MAX_ATTEMPTS + 1):
        if attempt > 1:
            logger.info("Retry attempt %d/%d: restarting the run from scratch", attempt, DB_RETRY_MAX_ATTEMPTS)

        try:
            conn = db.connect()
        except Exception:
            logger.critical("Failed to connect to MSSQL", exc_info=True)
            sys.exit(1)

        # Separate connection/table from the main sync -- see db.open_logging_connection
        # for why. Never raises: log_conn/log_id are None if this fails, and every call
        # below is a safe no-op in that case, so a logging failure can't take down the
        # actual pipeline.
        log_conn = db.open_logging_connection()
        log_id = db.start_pipeline_log(log_conn, PIPELINE_NAME)

        total_issues = 0
        try:
            db.apply_schema(conn)

            projects = client.list_projects()
            logger.info("Scope: ALL %d discovered projects (no project filter applied to JQL)", len(projects))

            field_defs = client.list_fields()
            db.upsert_field_definitions(conn, [
                {"field_id": f["id"], "field_name": f["name"], "field_type": f["type"]} for f in field_defs
            ])

            # Resolved by name, not hardcoded, since these customfield_* ids are specific
            # to this Jira site and would break if the pipeline is ever pointed at another.
            team_field_id = resolve_field_id(field_defs, "Team")
            sprint_field_id = resolve_field_id(field_defs, "Sprint")
            if not team_field_id:
                logger.warning("No custom field named 'Team' found on this site — Team capture disabled for this run")
            if not sprint_field_id:
                logger.warning("No custom field named 'Sprint' found on this site — Sprint capture disabled for this run")

            statuses = client.list_statuses()
            known_status_ids = {s["status_id"] for s in statuses}
            categories_by_id = {}
            for s in statuses:
                if s["category_id"] is not None:
                    categories_by_id[s["category_id"]] = {
                        "category_id": s["category_id"],
                        "category_key": s["category_key"],
                        "category_name": s["category_name"],
                        "color_name": s["color_name"],
                    }
            db.upsert_status_categories(conn, list(categories_by_id.values()))
            db.upsert_statuses(conn, [
                {"status_id": s["status_id"], "status_name": s["status_name"], "category_id": s["category_id"]}
                for s in statuses
            ])

            total_issues = run(jql, conn, client, known_status_ids, team_field_id, sprint_field_id)
            db.finish_pipeline_log(log_conn, log_id, status="Success", rows_processed=total_issues)
            return

        except JiraAuthError as exc:
            logger.critical("Jira authentication failed: %s", exc)
            db.finish_pipeline_log(log_conn, log_id, status="Failed", rows_processed=total_issues, error_message=str(exc))
            sys.exit(1)
        except JiraAPIError as exc:
            logger.critical("Unrecoverable Jira API error: %s", exc)
            db.finish_pipeline_log(log_conn, log_id, status="Failed", rows_processed=total_issues, error_message=str(exc))
            sys.exit(1)
        except Exception as exc:
            db.finish_pipeline_log(log_conn, log_id, status="Failed", rows_processed=total_issues, error_message=str(exc))
            if db.is_transient_connection_error(exc) and attempt < DB_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "Transient MSSQL connection error (attempt %d/%d): %s — retrying the whole run in %ds "
                    "(safe: every write here is idempotent, see README)",
                    attempt, DB_RETRY_MAX_ATTEMPTS, exc, DB_RETRY_BACKOFF_SECONDS,
                )
                time.sleep(DB_RETRY_BACKOFF_SECONDS)
                continue
            logger.critical("Unhandled pipeline error", exc_info=True)
            raise
        finally:
            conn.close()
            if log_conn:
                log_conn.close()


if __name__ == "__main__":
    main()
