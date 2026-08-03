# Jira Cloud -> MSSQL ETL

Extracts issues, changelog (status/field history), and worklogs from Jira Cloud
across **all** projects the API token can see, and loads them into SQL Server
for analytics.

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
# Incremental sync — issues updated in the last 1 day (default)
python main.py --incremental

# Incremental sync with a wider window
python main.py --incremental --days 7

# Full sync — every issue in every project, no date filter
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

Three tables, linked by `issue_key` (also carrying `issue_id`, Jira's
immutable internal ID, as primary key on `jira_issues`):

- **`jira_issues`** — one row per issue, current state. Standard fields are
  flattened into columns; every `customfield_*` key is preserved as-is (no
  name resolution) in `custom_fields_json`, since custom fields vary heavily
  by project/issue type. `raw_json` keeps the full original payload as a
  safety net. Upserted via `MERGE` keyed on `issue_id`.
- **`jira_issue_changelog`** — one row per field-change event, sourced from
  `/issue/{key}/changelog`. This is what lets you reconstruct status history
  (or any field's history) over time. Idempotent insert keyed on
  `(issue_id, changelog_id, field_name)`.
- **`jira_worklogs`** — one row per worklog entry, sourced from
  `/issue/{key}/worklog`. Idempotent insert keyed on `(issue_id, worklog_id)`.

Full DDL: see `schema.sql`.

## Files

- `jira_client.py` — Jira REST API calls: project discovery, issue search
  (`/search/jql` with `nextPageToken` pagination), changelog, worklogs.
  Retries with exponential backoff on 429s and transient network/5xx errors;
  raises immediately on 401/403.
- `transform.py` — raw Jira JSON -> row dicts for all three tables.
- `db.py` — MSSQL connection, schema bootstrap, batched MERGE/insert logic.
- `main.py` — CLI entrypoint (`--full` / `--incremental --days N`), logging.
- `schema.sql` — CREATE TABLE DDL (idempotent, guarded by `IF OBJECT_ID(...) IS NULL`).

## Error handling

- **401/403** from Jira aborts the run immediately (logged as critical) —
  these indicate a bad token/email, not a transient issue worth retrying.
- **429** and 5xx/network errors are retried with exponential backoff (up to
  6 attempts), honoring `Retry-After` when Jira sends it.
- A single issue's changelog or worklog fetch failing (after retries) is
  logged as a warning and skipped, rather than aborting the entire run — the
  issue row itself still loads.
