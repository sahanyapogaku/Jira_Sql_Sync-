"""Transform raw Jira API payloads into row dicts matching the MSSQL table columns."""

import json
import re

_PO_NUMBER_RE = re.compile(r"POKRM_\d+")


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


def _flatten_environment(value):
    """fields.environment -- schema.type "string" like Description, which doesn't rule out
    ADF rich text (confirmed nowhere set on this site today, so this is defensive, not
    verified against a live example). Same generic ADF detection as
    extract_custom_field_value_rows.
    """
    if isinstance(value, dict) and value.get("type") == "doc":
        return _adf_to_text(value)
    return value


def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _extract_custom_fields(fields, exclude=None):
    exclude = exclude or set()
    return {k: v for k, v in fields.items() if k.startswith("customfield_") and k not in exclude}


def flatten_issue(issue, exclude_custom_field_ids=None):
    """Map one /search or /issue payload into a jira_issues row dict.

    exclude_custom_field_ids: field ids to leave out of custom_fields_json because they
    already have dedicated handling elsewhere (currently just Team -- see main.py). Sprint
    is deliberately NOT excluded here: nothing in the ticket asked for it to stop appearing
    in this catch-all blob, only for it to also gain a dedicated table.
    """
    fields = issue.get("fields", {}) or {}

    custom_fields = _extract_custom_fields(fields, exclude=exclude_custom_field_ids)

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
        "due_date": fields.get("duedate"),
        "environment": _flatten_environment(fields.get("environment")),
        "security_level": _safe_get(fields, "security", "name"),
        "original_estimate_seconds": fields.get("timeoriginalestimate"),
        "remaining_estimate_seconds": fields.get("timeestimate"),
        "time_spent_seconds": fields.get("timespent"),
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


_DISPLAY_KEYS = ("name", "value", "displayName")


def _extract_display_key(item):
    """Best-effort human-readable label for one dict-shaped custom field value entry."""
    if not isinstance(item, dict):
        return None
    for key in _DISPLAY_KEYS:
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _flatten_display_value(value):
    """Best-effort readable text for a custom field's (already ADF-flattened) value.

    Same generic-shape-detection philosophy as the ADF handling below -- no per-field-id
    lookup table, since which fields use which shape (single option object, list of option
    objects, list of full objects like Sprint's sprint dicts, etc.) is a per-site admin
    choice. Returns None when nothing recognizable is found (e.g. the Approvals field's
    blob, which doesn't have name/value/displayName at the top level) so callers can fall
    back to the raw `value` column instead of showing a misleadingly empty display value.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _extract_display_key(value)
    if isinstance(value, list):
        labels = [_extract_display_key(item) for item in value]
        labels = [label for label in labels if label]
        return ", ".join(labels) if labels else None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def extract_custom_field_value_rows(issue_id, issue_key, fields, exclude_custom_field_ids=None):
    """An issue's customfield_* entries -> list of jira_custom_field_values row dicts (skips nulls).

    Values stored as Atlassian Document Format (ADF) rich text -- confirmed present under
    15 different field ids on this site, e.g. "Considered Options"/"Decision" (not just
    those two) -- are flattened to plain text here, the same approach already used for
    comment/worklog bodies, rather than stored as raw ADF JSON. Detection is generic
    (any dict value with type == "doc"), not a hardcoded field-id list, since which fields
    use ADF is a per-site admin choice, same reasoning as the Sprint/Team field-id lookups.

    `value` always keeps the full-fidelity encoding (plain text, or JSON for anything
    structured). `value_display` is a second, best-effort readable column alongside it --
    see _flatten_display_value -- NULL where no readable label could be derived.

    exclude_custom_field_ids: field ids to leave out of this table entirely -- currently
    just "Rank" (see main.py), Jira's internal LexoRank board-ordering token. It's a plain
    string, not ADF or a structured value, so it isn't touched by either helper above and
    would otherwise land in both `value` and `value_display` verbatim -- but it's never a
    human-meaningful value (Jira doesn't even show it on the issue itself), so there's no
    "clean" version to derive, unlike every other field this table covers. Only removed
    from this table, not from jira_issues.custom_fields_json, which stays fully raw by
    design (see README "Decisions").
    """
    rows = []
    for field_id, value in _extract_custom_fields(fields, exclude=exclude_custom_field_ids).items():
        if value is None:
            continue
        if isinstance(value, dict) and value.get("type") == "doc":
            value = _adf_to_text(value)
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "field_id": field_id,
            "value": value if isinstance(value, str) else json.dumps(value, default=str),
            "value_display": _flatten_display_value(value),
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


def extract_component_rows(fields):
    """An issue's fields.components -> list of jira_components row dicts (the component
    dimension itself). Mirrors extract_fix_version_rows: project_id comes from the issue's
    own project, not from the component object (Jira's issue payload only returns
    {id, name, self} for each component, not full project detail).
    """
    rows = []
    project_id = _safe_get(fields, "project", "id")
    for c in fields.get("components") or []:
        if not c.get("id"):
            continue
        rows.append({
            "component_id": int(c["id"]),
            "component_name": c.get("name"),
            "project_id": int(project_id) if project_id else None,
        })
    return rows


def extract_issue_component_links(issue_id, issue_key, fields):
    """An issue's fields.components -> list of jira_issue_components junction row dicts."""
    rows = []
    for c in fields.get("components") or []:
        if not c.get("id"):
            continue
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "component_id": int(c["id"]),
        })
    return rows


def extract_sprint_rows(fields, sprint_field_id):
    """An issue's Sprint field -> list of jira_sprints row dicts (the sprint dimension
    itself). sprint_field_id is resolved at runtime by the caller (see
    main.resolve_field_id) since its customfield_* id is instance-specific -- confirmed as
    customfield_10020 on this site, but never hardcoded here.
    """
    if not sprint_field_id:
        return []
    rows = []
    for s in fields.get(sprint_field_id) or []:
        if not s.get("id"):
            continue
        rows.append({
            "sprint_id": int(s["id"]),
            "sprint_name": s.get("name"),
            "state": s.get("state"),
            "board_id": int(s["boardId"]) if s.get("boardId") is not None else None,
            "start_date": s.get("startDate"),
            "end_date": s.get("endDate"),
            "goal": s.get("goal"),
        })
    return rows


def extract_issue_sprint_links(issue_id, issue_key, fields, sprint_field_id):
    """An issue's Sprint field -> list of jira_issue_sprints junction row dicts."""
    if not sprint_field_id:
        return []
    rows = []
    for s in fields.get(sprint_field_id) or []:
        if not s.get("id"):
            continue
        rows.append({
            "issue_id": issue_id,
            "issue_key": issue_key,
            "sprint_id": int(s["id"]),
        })
    return rows


def extract_team_row(issue_id, issue_key, fields, team_field_id):
    """An issue's Team field -> a jira_issue_team row dict, or None if unset or the Team
    field isn't configured on this site. team_field_id is resolved at runtime by the
    caller (see main.resolve_field_id) -- confirmed as customfield_10001 on this site, but
    never hardcoded here, since it's an instance-specific id like Sprint's.

    NOTE ON team_name: a normal Atlassian Team reference would need a call to the separate
    Teams API (different base URL/auth surface than Jira issues) to resolve an id to a
    display name -- that call is deliberately NOT made here per the ticket. However,
    confirmed against live data on this site, the Team field's value already embeds "name"
    inline (e.g. {"id": "...", "name": "Product Engineering", ...}), so team_name below is
    populated directly from the same payload at zero extra API cost. Flagged explicitly in
    the accompanying report since it changes the "is this worth an extra call" calculus the
    ticket assumed -- no extra call is needed either way.
    """
    if not team_field_id:
        return None
    team = fields.get(team_field_id)
    if not team or not team.get("id"):
        return None
    return {
        "issue_id": issue_id,
        "issue_key": issue_key,
        "team_id": str(team["id"]),
        "team_name": team.get("name"),
    }


def _option_value(raw):
    """Jira 'option'-type custom fields return {"value": "...", ...}; plain string/number/
    date fields return the scalar directly. Normalizes either into the display string."""
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def _find_field_value_by_name(fields, name_to_ids, name):
    """Look up a custom field's raw value by display name, not a fixed customfield_* id.

    Several field names on this site (e.g. "Category", "Project") are NOT unique -- Jira
    has multiple different customfield_* ids sharing the same name across different
    projects, since each project's screen can carry its own copy. name_to_ids (built once
    per run by main.py from client.list_fields()) maps a name to every id that currently
    has it. Rather than pin one id and silently return nothing for issues in a different
    project, this checks every id sharing the name and returns whichever one is actually
    non-null on *this* issue -- only one project's copy of the field is ever populated for
    a given issue, so this resolves correctly regardless of which project the issue is in.
    """
    for field_id in name_to_ids.get(name, []):
        value = fields.get(field_id)
        if value is not None:
            return value
    return None


def extract_purchase_order_row(issue_id, issue_key, fields, name_to_ids):
    """An issue's description + PO-related custom fields -> a jira_issue_purchase_orders
    row dict, or None if no PO number is found.

    There IS a dedicated "Order Number" field for this, but confirmed against live data
    it's populated on only 30 of 165 issues that actually have a PO -- the real PO number
    almost always lives as free text in the description instead, typically as a bolded
    line near the top (e.g. "PO Placed: POKRM_0100000222 -- 8/23/2026"), not in that field.
    Detection is a generic regex for the "POKRM_<digits>" token Jira's procurement system
    assigns, not a hardcoded per-issue lookup, since the surrounding sentence wording
    varies (confirmed: "PO Placed: X", "original purchase order (X)", etc.) but the token
    itself is consistent. Only the first token is captured if a description mentions more
    than one (e.g. an amended PO referencing the original) -- confirmed as a rare case (6
    of 165) -- since the first one found is the one nearest the top of the description,
    matching how these are actually written.

    No row is returned when no token is found, rather than a row with po_number = NULL,
    since most issues never reach the PO stage at all.
    """
    description = fields.get("description")
    description_text = _adf_to_text(description) if isinstance(description, dict) else (description or "")
    match = _PO_NUMBER_RE.search(description_text or "")
    if not match:
        return None

    return {
        "issue_id": issue_id,
        "issue_key": issue_key,
        "po_number": match.group(0),
        "order_number": _option_value(_find_field_value_by_name(fields, name_to_ids, "Order Number")),
        "total_cost": _find_field_value_by_name(fields, name_to_ids, "Total Cost ($)"),
        "qty": _find_field_value_by_name(fields, name_to_ids, "Qty"),
        "need_date": _find_field_value_by_name(fields, name_to_ids, "Need Date"),
        "po_needed": _option_value(_find_field_value_by_name(fields, name_to_ids, "PO Needed?")),
        "mrp_planned": _option_value(_find_field_value_by_name(fields, name_to_ids, "MRP Planned?")),
        "category": _option_value(_find_field_value_by_name(fields, name_to_ids, "Category")),
        "vendor_project": _option_value(_find_field_value_by_name(fields, name_to_ids, "Project")),
    }
