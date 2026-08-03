"""Transform raw Jira API payloads into row dicts matching the MSSQL table columns."""

import json


def _adf_to_text(node):
    """Best-effort flatten of Atlassian Document Format (ADF) rich text to plain text."""
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(child) for child in node.get("content", [])]
        return "".join(p for p in parts if p)
    return None


def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def flatten_issue(issue):
    """Map one /search or /issue payload into a jira_issues row dict."""
    fields = issue.get("fields", {}) or {}

    custom_fields = {k: v for k, v in fields.items() if k.startswith("customfield_")}

    components = [c.get("name") for c in (fields.get("components") or [])]
    fix_versions = [v.get("name") for v in (fields.get("fixVersions") or [])]
    labels = fields.get("labels") or []

    return {
        "issue_id": int(issue["id"]),
        "issue_key": issue["key"],
        "project_key": _safe_get(fields, "project", "key"),
        "project_name": _safe_get(fields, "project", "name"),
        "issue_type": _safe_get(fields, "issuetype", "name"),
        "summary": fields.get("summary"),
        "status": _safe_get(fields, "status", "name"),
        "status_category": _safe_get(fields, "status", "statusCategory", "name"),
        "priority": _safe_get(fields, "priority", "name"),
        "assignee_account_id": _safe_get(fields, "assignee", "accountId"),
        "assignee_name": _safe_get(fields, "assignee", "displayName"),
        "reporter_account_id": _safe_get(fields, "reporter", "accountId"),
        "reporter_name": _safe_get(fields, "reporter", "displayName"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolutiondate": fields.get("resolutiondate"),
        "labels": json.dumps(labels),
        "components": json.dumps(components),
        "fix_versions": json.dumps(fix_versions),
        "custom_fields_json": json.dumps(custom_fields, default=str),
        "raw_json": json.dumps(issue, default=str),
    }


def extract_changelog_rows(issue_id, issue_key, history_entry):
    """One history entry (from /issue/{key}/changelog) -> list of per-field-change rows."""
    rows = []
    changelog_id = history_entry.get("id")
    created = history_entry.get("created")
    author_account_id = _safe_get(history_entry, "author", "accountId")
    author_name = _safe_get(history_entry, "author", "displayName")

    for item_index, item in enumerate(history_entry.get("items", [])):
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "changelog_id": str(changelog_id),
            "item_index": item_index,
            "field_name": item.get("field"),
            "field_type": item.get("fieldtype"),
            "from_value": item.get("from"),
            "from_string": item.get("fromString"),
            "to_value": item.get("to"),
            "to_string": item.get("toString"),
            "author_account_id": author_account_id,
            "author_name": author_name,
            "changed_at": created,
        })
    return rows


def extract_comment_row(issue_id, issue_key, comment_entry):
    """One entry (from /issue/{key}/comment) -> a jira_comments row dict."""
    body = comment_entry.get("body")
    body_text = _adf_to_text(body) if isinstance(body, dict) else body

    return {
        "issue_id": issue_id,
        "issue_key": issue_key,
        "comment_id": str(comment_entry.get("id")),
        "author_account_id": _safe_get(comment_entry, "author", "accountId"),
        "author_name": _safe_get(comment_entry, "author", "displayName"),
        "body_text": body_text,
        "comment_created": comment_entry.get("created"),
        "comment_updated": comment_entry.get("updated"),
    }


def extract_issuelink_rows(issue_id, issue_key, fields):
    """An issue's fields.issuelinks -> list of jira_issue_links row dicts."""
    rows = []
    for link in fields.get("issuelinks") or []:
        if "outwardIssue" in link:
            direction, other = "outward", link["outwardIssue"]
        elif "inwardIssue" in link:
            direction, other = "inward", link["inwardIssue"]
        else:
            continue
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "link_id": str(link.get("id")),
            "link_type": _safe_get(link, "type", "name"),
            "direction": direction,
            "linked_issue_id": int(other["id"]) if other.get("id") else None,
            "linked_issue_key": other.get("key"),
        })
    return rows


def extract_worklog_row(issue_id, issue_key, worklog_entry):
    """One worklog entry (from /issue/{key}/worklog) -> a jira_worklogs row dict."""
    comment = worklog_entry.get("comment")
    comment_text = _adf_to_text(comment) if isinstance(comment, dict) else comment

    return {
        "issue_id": issue_id,
        "issue_key": issue_key,
        "worklog_id": str(worklog_entry.get("id")),
        "author_account_id": _safe_get(worklog_entry, "author", "accountId"),
        "author_name": _safe_get(worklog_entry, "author", "displayName"),
        "time_spent_seconds": worklog_entry.get("timeSpentSeconds"),
        "time_spent_display": worklog_entry.get("timeSpent"),
        "started_at": worklog_entry.get("started"),
        "worklog_created": worklog_entry.get("created"),
        "worklog_updated": worklog_entry.get("updated"),
        "comment_text": comment_text,
    }
