# Jira Cloud -> MSSQL ETL

Extracts issues, projects, statuses, changelog (status/field history),
worklogs, comments, issue links, remote links, labels, custom fields, fix
versions, attachments, and parent/epic hierarchy from Jira Cloud across
**all** projects the API token can see, and loads them into SQL Server for
analytics.

## Setup

1. Install [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)
   on the machine running this pipeline.
2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in real values:
   ```
   JIRA_URL=https://karman-industries.atlassian.net
   JIRA_EMAIL=you@example.com
   JIRA_API_TOKEN=<create at https://id.atlassian.com/manage-profile/security/api-tokens>
   MSSQL_SERVER=...
   MSSQL_DATABASE=...
   MSSQL_USER=...
   MSSQL_PASSWORD=...
   ```
   `MSSQL_TRUST_SERVER_CERTIFICATE` defaults to `no`; set it to `yes` only for
   local/dev SQL Server instances using a self-signed certificate.

The pipeline creates its own tables on first run (see `schema.sql`) — no
manual DDL step required, though you can run `schema.sql` by hand if you'd
rather review it first.

## Usage

```
# Incremental sync: issues updated in the last 1 day (default)
python main.py --incremental

# Incremental sync with a wider window
python main.py --incremental --days 7

# Full sync : every issue in every project, no date filter
python main.py --full
```

Run `--incremental` on a schedule (e.g. hourly via cron/Task Scheduler) and
`--full` occasionally as a backfill/reconciliation pass. Both modes are safe
to re-run: issues are upserted by `issue_id`, and changelog/worklog rows are
only inserted if not already present.

Logs go to both the console and `jira_etl.log` (override with `--log-file`).

## Scope

Projects are discovered dynamically via `/rest/api/3/project/search` — there
is no hardcoded project key list. Every project the API token has access to
is included; new projects are picked up automatically on the next run.

## Data model

Issues are the hub, linked by `issue_key` (also carrying `issue_id`, Jira's
immutable internal ID, as primary key on `jira_issues`). Dimension/lookup
tables are upserted via `MERGE`; append-only event logs are idempotent
inserts keyed on their natural event key; a few "current state" tables are
wholesale-replaced per issue on every sync since Jira reports only the
current set, not a diff (see `db.replace_*`).

- **`jira_issues`** : one row per issue, current state. Standard fields are
  flattened into columns, including `due_date`, `environment`, and
  `security_level` (the last flattened to just its name, matching
  `priority`/`status_category`); every `customfield_*` key is preserved as-is
  (no name resolution) in `custom_fields_json`, since custom fields vary
  heavily by project/issue type. `raw_json` keeps the full original payload
  as a safety net. Upserted via `MERGE` keyed on `issue_id`. Note: as of this
  writing, no issue on this site actually sets `environment` or
  `security_level` (0 of 3,579) — the columns are schema-ready but unverified
  against real non-null data.
- **`jira_project`** — one row per project (`project_id`, `project_key`,
  `project_name`, `project_type_key`). `jira_issues.project_id` FKs into it.
- **`jira_status_categories`** / **`jira_statuses`** — Jira's global status
  list and the "To Do / In Progress / Done" category each status belongs to,
  discovered via `/rest/api/3/status`. Statuses since deleted from a workflow
  (absent from that endpoint but still referenced by old changelog entries)
  are seeded as placeholder rows with `category_id = NULL`.
- **`jira_issue_changelog`** — one row per non-status field-change event,
  sourced from `/issue/{key}/changelog`. Idempotent insert keyed on
  `(issue_id, changelog_id, item_index)`.
- **`jira_issue_status_history`** — status-change events split out from the
  general changelog (Atlassian's recommended 2-table pattern), with
  `from_status_id`/`to_status_id` as FKs into `jira_statuses` instead of
  plain text. Idempotent insert keyed on `(issue_id, changelog_id, item_index)`.
- **`jira_worklogs`** — one row per worklog entry, sourced from
  `/issue/{key}/worklog`. Idempotent insert keyed on `(issue_id, worklog_id)`.
- **`jira_comments`** — one row per comment, sourced from
  `/issue/{key}/comment`.
- **`jira_issue_links`** — one row per issue-link relationship (`fields.issuelinks`:
  issue-to-issue links only).
- **`jira_issue_remote_links`** — one row per remote link, sourced from
  `/issue/{key}/remotelink`: Confluence pages, web URLs, and "relationship" links
  (e.g. "Approved") that `fields.issuelinks` doesn't cover. Idempotent insert keyed
  on `(issue_id, remote_link_id)`.
- **`jira_issue_labels`** — one row per `(issue_id, label)`, parsed out of
  `fields.labels` so labels are queryable/joinable instead of trapped in
  `jira_issues.labels`'s JSON blob. Replaced wholesale per issue on each sync.
- **`jira_custom_field_definitions`** — the field ID -> name/type lookup for
  every `customfield_*` key (from `/rest/api/3/field`), so `field_id` values
  elsewhere are resolvable to a human name.
- **`jira_custom_field_values`** — one row per `(issue_id, field_id)`, current
  value only. Replaced wholesale per issue on each sync. `value` is the
  full-fidelity encoding (plain text, or JSON for anything structured);
  `value_display` is a best-effort readable column alongside it (e.g. just
  the sprint name(s) for the Sprint field, instead of the full array of
  sprint objects) — NULL where no readable label could be derived, in which
  case fall back to parsing `value` directly. Excludes the "Rank" field
  entirely (resolved by name at runtime). See "Decisions" below.
- **`jira_fix_versions`** / **`jira_issue_fix_versions`** — the version
  dimension and its issue junction table. The junction is replaced wholesale
  per issue on each sync.
- **`jira_issue_attachments`** — one row per attachment, sourced from
  `fields.attachment`. Idempotent insert keyed on `(issue_id, attachment_id)`.
- **`jira_issue_hierarchy`** — one row per issue with a parent (subtask ->
  parent task, or story/task -> epic), keyed on `child_issue_id`. Replaced
  wholesale per issue on each sync since an issue has at most one parent.
- **`jira_components`** / **`jira_issue_components`** — the component
  dimension and its issue junction table, same dimension+junction shape as
  fix versions (components carry a stable id, unlike labels). The junction
  is replaced wholesale per issue on each sync. `jira_issues.components`
  (JSON array) is left populated as-is alongside this for now — see
  "Open questions" below.
- **`jira_sprints`** / **`jira_issue_sprints`** — the sprint dimension and
  its issue junction table (an issue can pass through multiple sprints over
  its life), same dimension+junction shape as fix versions. Not used by any
  report today; captured as future-proofing. The Sprint field's
  `customfield_*` id is resolved at runtime by name (`main.resolve_field_id`)
  rather than hardcoded, since that id is specific to this Jira site.
- **`jira_issue_team`** — one row per issue with a Team set, keyed on
  `issue_id` (an issue has at most one team). Team is an Atlassian Team
  reference (site-wide, not project-scoped), not a normal custom field — its
  `customfield_*` id is likewise resolved at runtime by name. `team_name` is
  populated directly from the field's own payload (confirmed to already
  include a display name on this site), so no separate call to the
  Atlassian Teams API is made.
- **`jira_issue_purchase_orders`** — one row per issue that has a PO number,
  detected as a `POKRM_##########` token regex-extracted from the issue
  description (confirmed live: the dedicated "Order Number" field is only
  populated on ~18% of issues that actually have one — the real PO number
  is almost always typed as free text instead). No row at all for issues
  without one, not a row with `po_number = NULL`. The other columns
  (`total_cost`, `qty`, `need_date`, `po_needed`, `mrp_planned`,
  `category`, `vendor_project`) are resolved by field *name*, checking
  every `customfield_*` id sharing that name and using whichever is
  non-null on that issue — several of these names are reused across
  different projects with different ids, so this isn't limited to one
  project. Replaced wholesale per issue on each sync, same as custom field
  values.

Full DDL: see `schema.sql`.

## Decisions

- **`jira_issues.custom_fields_json` / `.components` blob columns** — kept
  permanently, alongside `jira_custom_field_values` / `jira_issue_components`
  (and `jira_issue_labels`, which duplicates `jira_issues.labels` the same
  way). This matches the pattern already established for labels by original
  design ("stays as-is... for raw-payload convenience" — see `schema.sql`).
  Nothing inside this codebase reads the blobs back out, but an external
  consumer (BI tool, ad-hoc query) might, and there's no cost to keeping
  them — so removing them for normalization-purity alone isn't worth the
  risk. Not up for revisiting without a concrete reason.
- **`jira_custom_field_values.value_display`** — added because `value`
  alone was landing raw-JSON blobs for any structured (non-string, non-ADF)
  custom field, e.g. the Sprint field's array of full sprint objects
  (id/name/state/board/goal/dates), which isn't usable directly by an
  ad-hoc query or BI tool. `value_display` is a generic best-effort label
  (`transform._flatten_display_value`): a plain string as-is, or for a
  dict/list of dicts, the first of `name` / `value` / `displayName` found
  per item (joined with `, ` for lists). No per-field-id lookup table, same
  generic-shape-detection approach already used for ADF. `value` is kept
  unchanged alongside it (full fidelity, nothing lossy) rather than
  replaced, matching the raw-blob-plus-normalized precedent above.
- **`jira_custom_field_values` excludes the "Rank" field** (resolved by
  name at runtime, same as Team/Sprint) entirely — it's Jira's internal
  LexoRank board-ordering token, a plain string with no human-meaningful
  content, never shown on the issue itself in Jira's own UI. Unlike every
  other field this table covers, there's no "clean" version to derive
  (`value_display` would just duplicate the same opaque token as `value`),
  so it's dropped rather than kept as noise. Still present, unfiltered, in
  `jira_issues.custom_fields_json` — the raw blob stays fully raw by design
  (see above).

## Open questions

- **`jira_custom_field_values` for the "Approvals" field**
  (`customfield_10046`) — currently stored as one large JSON blob per issue
  (up to ~37KB), and `value_display` will be NULL for it too, since the
  blob has no top-level `name`/`value`/`displayName` key. Extracting
  `approval_status` / `final_decision` / `approver_id` (etc.) into real
  columns has been proposed but not built — see the custom-field-formatting
  investigation notes.

## Files

- `jira_client.py` — Jira REST API calls: project/status/field discovery,
  issue search (`/search/jql` with `nextPageToken` pagination), changelog,
  worklogs, comments. Retries with exponential backoff on 429s and transient
  network/5xx errors; raises immediately on 401/403.
- `transform.py` — raw Jira JSON -> row dicts for every table.
- `db.py` — MSSQL connection, schema bootstrap, batched MERGE/insert/replace logic.
  Also retries transient connection drops with backoff (see "Error handling").
- `main.py` — CLI entrypoint (`--full` / `--incremental --days N`), logging,
  and the run-level retry loop around a transient MSSQL failure.
- `schema.sql` — CREATE TABLE DDL (idempotent, guarded by `IF OBJECT_ID(...) IS NULL`).
- `pipeline_logs.sql` — DDL for `dbo.pipeline_logs`, a run-level log table
  (start/end time, status, rows processed, error message) for monitoring in
  Grafana. Auto-applied at startup via `db.open_logging_connection`.
- `grafana_queries.sql` — ready-to-paste panel queries for a Grafana
  dashboard against `pipeline_logs` (last-run status, success rate, run
  duration, recent failures, stuck-`Running` detection, etc).
- `TROUBLESHOOTING.md` — catalog of failure modes actually seen in this
  pipeline (MSSQL connection drops, Jira auth/rate-limit errors, etc.), what
  handles each automatically, and what to check manually when it doesn't.
- `API_LOGIC.md` — every Jira endpoint called, its pagination style, and the
  full rate-limit/backoff behavior, plus the API-deprecation change
  management plan.
- `PROCESS.md` — the operational runbook: schedule, success criteria,
  log/error alerting, and rollback plan.

## Error handling

- **401/403** from Jira aborts the run immediately (logged as critical) —
  these indicate a bad token/email, not a transient issue worth retrying.
- **429** and 5xx/network errors are retried with exponential backoff (up to
  6 attempts), honoring `Retry-After` when Jira sends it.
- A single issue's changelog or worklog fetch failing (after retries) is
  logged as a warning and skipped, rather than aborting the entire run — the
  issue row itself still loads.
- **Transient MSSQL connection drops** (network blip, VPN drop, machine
  sleep) are retried with backoff at two levels: `db.connect()` itself
  retries up to 3 times, and `main.py` retries the *entire run* up to 3
  times 30s apart if the failure is transient — safe because every write in
  this pipeline is idempotent. See `TROUBLESHOOTING.md` for the full catalog
  of failure modes and what to do when retries aren't enough.
