# Troubleshooting

Known failure modes for the `jira_etl` pipeline, what causes each one, how the
pipeline already handles it (if it does), and what to check manually. Written
against real errors this pipeline has actually hit, not hypothetical ones —
each entry below happened at least once during development/backfill.

Check `dbo.pipeline_logs` (Grafana → "Recent failures with error detail",
see `grafana_queries.sql`) first for `error_message`, then cross-reference the
matching entry below. For anything not covered here, check `jira_etl.log` /
`jira_etl_full.log` directly — every error also gets a full traceback there.

## MSSQL connection dropped mid-run

**Looks like:** `pipeline_logs.error_message` containing "Process crashed:
MSSQL connection..." (this is the error in the screenshot that prompted this
doc), or in the log file:
```
pyodbc.OperationalError: ('08S01', '[08S01] [Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: An existing connection was forcibly closed by the remote host...')
```
or SQLSTATE `08001` ("Named Pipes Provider: Could not open a connection").

**Cause:** a network blip, VPN drop, or the machine going to sleep during a
long-running sync (confirmed live: one incident showed a 1.5-hour gap between
retry attempts, consistent with the machine sleeping mid-run, not just a
momentary blip).

**Handled automatically, as of commit `40dd643`:**
- `db.connect()` retries transient connection failures up to `CONNECT_MAX_RETRIES`
  (3) times with backoff before giving up.
- `main.py` retries the **entire run** up to `DB_RETRY_MAX_ATTEMPTS` (3) times,
  30s apart, if the failure is transient (`db.is_transient_connection_error`).
  Safe to restart the whole run from scratch because every write in this
  pipeline is idempotent (`MERGE` / insert-where-not-exists — see README's
  "Data model" section).
- Not retried: `JiraAuthError`, `JiraAPIError`, or any non-transient exception
  — those still fail fast, same as before.

**The two rows in the screenshot predate this fix** — they're from crashes on
2026-08-10 that happened before the retry logic existed, manually corrected
from a stuck `'Running'` state to `'Failed'` afterward (see "Stuck Running
rows" below for why they were stuck in the first place).

**If it still happens after 3 retries:** the outage outlasted ~90 seconds of
backoff. Check whether the machine running the pipeline actually stayed
awake and network-connected for the full run — full syncs can take 30-40+
minutes. If this becomes frequent, consider: running full syncs on a machine
with sleep disabled, or raising `DB_RETRY_MAX_ATTEMPTS` /
`DB_RETRY_BACKOFF_SECONDS` in `main.py`.

## Jira authentication failure (401/403)

**Looks like:** `pipeline_logs.status = 'Failed'`, `error_message` mentioning
"Jira authentication failed", or in the log: `JiraAuthError`.

**Cause:** `JIRA_API_TOKEN` is wrong, expired, or revoked, or `JIRA_EMAIL`
doesn't match the account that owns the token.

**Handled automatically:** not retried, by design — `jira_client.py` raises
immediately on 401/403 rather than burning retries on a credential problem
that won't fix itself.

**Resolution:** generate a fresh token at
https://id.atlassian.com/manage-profile/security/api-tokens, update
`JIRA_API_TOKEN` in `.env`, and confirm `JIRA_EMAIL` matches that account.

## Jira API errors after exhausting retries (429 / 5xx / network)

**Looks like:** `error_message` mentioning "Unrecoverable Jira API error", or
in the log: `JiraAPIError` after several `WARNING` lines like `429 rate
limited on ... — retrying in Xs (attempt N/6)`.

**Cause:** sustained rate limiting or a Jira Cloud outage that outlasts
`jira_client.py`'s retry budget (`MAX_RETRIES = 6`, exponential backoff,
honoring `Retry-After` on 429s).

**Handled automatically:** up to the retry budget above; not retried further
by `main.py` (a full-run retry wouldn't help if Jira itself is down).

**Resolution:** check https://status.atlassian.com for an active incident;
otherwise re-run once the rate-limit window passes. If this happens
regularly (not just once), something else may also be hitting the same
Jira API token concurrently — check for other integrations sharing it.

## A single issue's changelog/worklog/comment/remote-link fetch fails

**Looks like:** `WARNING` lines like `Skipping changelog for ISSUE-123 after
repeated failures: ...` — no `CRITICAL`, no `pipeline_logs` failure. The run
continues and completes normally.

**Cause:** a transient error specific to that one sub-resource call, already
retried and exhausted per `jira_client.py`'s normal retry budget.

**Handled automatically:** by design — the issue row itself and its other
data still load; only that one sub-resource is skipped for that issue this
run. Since changelog/worklog/comment loading is idempotent
(insert-where-not-exists), the next sync (even the next incremental run)
will pick it up if it was truly transient.

**Resolution:** usually none needed. If the *same* issue keeps failing across
multiple runs, check Jira permissions on that specific issue/project for the
API token's account.

## `pipeline_logs` itself fails to write

**Looks like:** `WARNING jira_etl.db: Failed to write pipeline_logs start row`
or `...end row` in the log, but the main sync's own log lines (issue counts,
"Run complete") look otherwise normal.

**Cause:** almost always the same root cause as "MSSQL connection dropped"
above, hitting the separate logging connection instead of (or in addition
to) the main one. By design, this **cannot** crash the actual data pipeline
— `db.start_pipeline_log`/`db.finish_pipeline_log` catch everything
internally and only log a warning.

**Resolution:** if the main sync is completing successfully but this warning
keeps appearing on its own, confirm the SQL login has permission to create
tables (for `pipeline_logs.sql`'s auto-apply) and write to
`dbo.pipeline_logs` specifically — a permissions issue here wouldn't
necessarily block permissions on the other Jira tables.

## Missing required environment variables

**Looks like:** `CRITICAL jira_etl: Missing required environment variables:
...` immediately on startup, process exits before doing anything.

**Cause:** one or more of `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
`MSSQL_SERVER`, `MSSQL_DATABASE`, `MSSQL_USER`, `MSSQL_PASSWORD` isn't set.

**Resolution:** check `.env` against `.env.example` for the missing key(s)
named in the error.

## Stuck `'Running'` rows in `pipeline_logs`

**Looks like:** a `pipeline_logs` row with `status = 'Running'` and no
`end_time`, from a run that's clearly not still going (Grafana panel #8 in
`grafana_queries.sql` is built specifically to surface these).

**Cause:** the process died hard enough to skip *every* exception handler in
`main.py` — a hard kill (`taskkill`, OOM, host reboot), or a case where both
the main connection and the logging connection failed in the same instant
before `finish_pipeline_log` could even attempt its write. This is
mechanically what happened in the incident behind the screenshot above,
before the retry logic existed.

**Not fully eliminated by the retry logic** — it reduces how often this can
happen (more chances to succeed and write a clean outcome), but a true hard
kill of the process still bypasses everything.

**Resolution:** manually correct the row once you know the run actually
failed:
```sql
UPDATE dbo.pipeline_logs
SET end_time = SYSUTCDATETIME(), status = 'Failed',
    error_message = '<what actually happened>'
WHERE log_id = <id>;
```
