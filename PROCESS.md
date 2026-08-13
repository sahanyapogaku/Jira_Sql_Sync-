# Process

The operational runbook for `jira_etl`: schedule, what "success" means,
how failures surface, and how to roll back. The pipeline has been run
enough times informally (a zero-error full-sync log, several real crashes
and recoveries — see `TROUBLESHOOTING.md`) that this document is
formalizing an already-proven pattern, not proposing an untested one.

## Schedule

- **Incremental sync** (`python main.py --incremental`, default `--days 1`):
  intended to run on a recurring schedule — hourly via cron or Windows Task
  Scheduler. The 1-day default window (rather than exactly matching the
  run interval) is a deliberate overlap: if one scheduled run is missed or
  delayed, the next one's wider window still catches whatever it missed.
- **Full sync** (`python main.py --full`): a periodic reconciliation/backfill
  pass across every issue in every project, with no date filter. Recommended
  cadence: **weekly, during off-hours** (a full sync currently takes
  30–40+ minutes and issues one Jira API call per sub-resource per issue —
  see `API_LOGIC.md`). Also run it once, out of band, any time a schema or
  transform change needs to backfill historical issues that an incremental
  window wouldn't reach (e.g. adding a new column — see the Sprint/Team/
  Components/due-date rollout in this repo's history for a worked example).

## Success criteria

A run is successful when:
- Its `pipeline_logs` row shows `status = 'Success'` (see `pipeline_logs.sql`
  / `grafana_queries.sql` panel 1 — "Last run status").
- No `CRITICAL` lines appear in `jira_etl.log` for that run.
- Process exit code is 0.

**`rows_processed = 0` is not automatically a failure** — for an incremental
run, it correctly means no issues were updated in that window. Treat it as
a signal worth investigating only if it drops to zero for an extended
stretch during otherwise-active hours (Grafana panel 5, "rows processed
over time," is built for spotting exactly that pattern) or if it happens
on a `--full` run, where zero would be a red flag.

## Log / error alerting

Already built, not hypothetical:
- **`jira_etl.log`** (and `jira_etl_full.log` for full-sync runs) — every
  run's full detail, including tracebacks for anything unhandled.
- **`dbo.pipeline_logs`** — structured per-run outcome (start/end time,
  status, rows processed, error message), written by `db.start_pipeline_log`/
  `db.finish_pipeline_log`, designed to never crash the actual sync even if
  the logging write itself fails (see `TROUBLESHOOTING.md`).
- **Grafana dashboard** (`grafana_queries.sql`) — 8 panels against
  `pipeline_logs`: last run status, minutes since last success, 7-day
  success rate, run duration over time, rows processed over time, runs/day
  success-vs-failed, recent failures with error detail, and stuck-`Running`
  detection.
- **`TROUBLESHOOTING.md`** — the catalog of what each failure mode actually
  looks like and how to resolve it.

**What should actually page someone**, not just sit on a dashboard: wire
Grafana alerts on panel 2 (minutes since last successful run — catches a
scheduler that silently died) and panel 8 (stuck `Running` rows — catches a
hard process kill). Everything else on the dashboard is for
investigation *after* one of those two fires, not a first-line alert on its
own.

## Rollback plan

**For a bad run** (crashed, incomplete, or wrong data from a transient
cause): just re-run `--incremental` or `--full`. This works because almost
every write in this pipeline is idempotent by design:
- `jira_issues`, `jira_project`, `jira_statuses`, `jira_status_categories`,
  `jira_custom_field_definitions`, `jira_fix_versions`, `jira_components`,
  `jira_sprints` — upserted via `MERGE`, safe to re-run.
- `jira_custom_field_values`, `jira_issue_labels`, `jira_issue_fix_versions`,
  `jira_issue_components`, `jira_issue_sprints`, `jira_issue_team`,
  `jira_issue_hierarchy` — wholesale-replaced per issue on every sync, so a
  re-run always reflects the current true state, not a stale partial one.

**One real caveat** — this does *not* apply to the append-only event
tables: `jira_issue_changelog`, `jira_issue_status_history`,
`jira_worklogs`, `jira_comments`, `jira_issue_links`,
`jira_issue_remote_links`, `jira_issue_attachments`. These use
insert-where-not-exists keyed on a natural event key, so a re-run adds
*new* events but will not remove or correct a *bad* row already inserted
under a correct-looking key (e.g. a transform bug that wrote a wrong
`field_name` before being fixed). If a code bug — not a crash — wrote bad
rows into one of these tables, rollback requires a manual, targeted
`DELETE` for the affected rows, not just a re-run.

**For a bad code deploy specifically:** revert the code change first, then
re-run — re-running with the same buggy code just reproduces the same bad
output, whether or not the write pattern is idempotent.
