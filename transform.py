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


def _extract_custom_fields(fields):
    return {k: v for k, v in fields.items() if k.startswith("customfield_")}


def flatten_issue(issue):
    """Map one /search or /issue payload into a jira_issues row dict."""
    fields = issue.get("fields", {}) or {}

    custom_fields = _extract_custom_fields(fields)

    components = [c.get("name") for c in (fields.get("components") or [])]
    fix_versions = [v.get("name") for v in (fields.get("fixVersions") or [])]
    labels = fields.get("labels") or []
    project_id = _safe_get(fields, "project", "id")

    return {
        "issue_id": int(issue["id"]),
        "issue_key": issue["key"],
        "project_id": int(project_id) if project_id else None,
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
    """One history entry (from /issue/{key}/changelog) -> list of per-field-change rows.

    Excludes "status" items -- those go to extract_status_history_rows() instead, per
    Atlassian's recommended 2-table history split.
    """
    rows = []
    changelog_id = history_entry.get("id")
    created = history_entry.get("created")
    author_account_id = _safe_get(history_entry, "author", "accountId")
    author_name = _safe_get(history_entry, "author", "displayName")

    for item_index, item in enumerate(history_entry.get("items", [])):
        if item.get("field") == "status":
            continue
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


def extract_status_history_rows(issue_id, issue_key, history_entry):
    """One history entry (from /issue/{key}/changelog) -> list of jira_issue_status_history
    row dicts: "status" items only, with from/to as status IDs (FKs into jira_statuses)
    rather than plain display text.
    """
    rows = []
    changelog_id = history_entry.get("id")
    created = history_entry.get("created")
    author_account_id = _safe_get(history_entry, "author", "accountId")
    author_name = _safe_get(history_entry, "author", "displayName")

    for item_index, item in enumerate(history_entry.get("items", [])):
        if item.get("field") != "status":
            continue
        to_value = item.get("to")
        if to_value is None:
            continue
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "changelog_id": str(changelog_id),
            "item_index": item_index,
            "from_status_id": str(item["from"]) if item.get("from") is not None else None,
            "to_status_id": str(to_value),
            "author_account_id": author_account_id,
            "author_name": author_name,
            "changed_at": created,
            # Not columns on jira_issue_status_history (that table is FK-only, no plain
            # text) -- carried here so a caller can name a status that's since been
            # deleted from the workflow (Jira's global /status list won't have it, but
            # the changelog still does) when seeding a placeholder jira_statuses row.
            "from_status_name": item.get("fromString"),
            "to_status_name": item.get("toString"),
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


def extract_project_row(fields):
    """An issue's fields.project -> a jira_project row dict, or None if absent."""
    project = fields.get("project") or {}
    if not project.get("id"):
        return None
    return {
        "project_id": int(project["id"]),
        "project_key": project.get("key"),
        "project_name": project.get("name"),
        "project_type_key": project.get("projectTypeKey"),
    }


def extract_custom_field_value_rows(issue_id, issue_key, fields):
    """An issue's customfield_* entries -> list of jira_custom_field_values row dicts (skips nulls)."""
    rows = []
    for field_id, value in _extract_custom_fields(fields).items():
        if value is None:
            continue
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "field_id": field_id,
            "value": value if isinstance(value, str) else json.dumps(value, default=str),
        })
    return rows


def extract_fix_version_rows(fields):
    """An issue's fields.fixVersions -> list of jira_fix_versions row dicts (the version dimension itself)."""
    rows = []
    project_id = _safe_get(fields, "project", "id")
    for v in fields.get("fixVersions") or []:
        if not v.get("id"):
            continue
        rows.append({
            "version_id": int(v["id"]),
            "version_name": v.get("name"),
            "project_id": int(project_id) if project_id else None,
            "release_date": v.get("releaseDate"),
            "released": bool(v.get("released")) if v.get("released") is not None else None,
        })
    return rows


def extract_issue_fix_version_links(issue_id, issue_key, fields):
    """An issue's fields.fixVersions -> list of jira_issue_fix_versions junction row dicts."""
    rows = []
    for v in fields.get("fixVersions") or []:
        if not v.get("id"):
            continue
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "version_id": int(v["id"]),
        })
    return rows


def extract_attachment_rows(issue_id, issue_key, fields):
    """An issue's fields.attachment -> list of jira_issue_attachments row dicts."""
    rows = []
    for att in fields.get("attachment") or []:
        rows.append({
            "attachment_id": str(att.get("id")),
            "issue_id": issue_id,
            "issue_key": issue_key,
            "filename": att.get("filename"),
            "size_bytes": att.get("size"),
            "mime_type": att.get("mimeType"),
            "author_account_id": _safe_get(att, "author", "accountId"),
            "author_name": _safe_get(att, "author", "displayName"),
            "attachment_created": att.get("created"),
            "content_url": att.get("content"),
        })
    return rows


def extract_label_rows(issue_id, issue_key, fields):
    """An issue's fields.labels -> list of jira_issue_labels row dicts."""
    return [
        {"issue_id": issue_id, "issue_key": issue_key, "label": label}
        for label in (fields.get("labels") or [])
    ]


def extract_remote_link_rows(issue_id, issue_key, remote_link_entries):
    """A list of entries (from /issue/{key}/remotelink) -> jira_issue_remote_links row dicts."""
    rows = []
    for entry in remote_link_entries:
        obj = entry.get("object") or {}
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "remote_link_id": str(entry.get("id")),
            "relationship": entry.get("relationship"),
            "title": obj.get("title"),
            "url": obj.get("url"),
            "global_id": entry.get("globalId"),
        })
    return rows


def extract_hierarchy_row(issue_id, issue_key, fields):
    """An issue's fields.parent -> a jira_issue_hierarchy row dict, or None if the issue has no parent.

    Covers both subtask -> parent-task and story/task -> epic: this Jira site's projects
    expose both relationships through the same "parent" field (team-managed project model)
    rather than a separate classic "Epic Link" custom field (confirmed against live data).
    """
    parent = fields.get("parent")
    if not parent or not parent.get("id"):
        return None
    is_subtask = _safe_get(fields, "issuetype", "subtask") is True
    return {
        "child_issue_id": issue_id,
        "child_issue_key": issue_key,
        "parent_issue_id": int(parent["id"]),
        "parent_issue_key": parent.get("key"),
        "relationship_type": "subtask" if is_subtask else "epic_child",
    }
