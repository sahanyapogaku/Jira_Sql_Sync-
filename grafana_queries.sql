-- Grafana panel queries for monitoring the jira_etl pipeline via dbo.pipeline_logs.
-- Each block below is one panel's query -- paste the query, not the whole file, into a
-- panel's query editor (MSSQL / Azure SQL data source). $__timeFilter(start_time) is a
-- Grafana macro that expands to a BETWEEN clause using the dashboard's time range; omit it
-- on panels that should always show the latest state regardless of the selected range.


-- 1. Last run status (Stat panel, color-coded: green=Success, red=Failed, yellow=Running)
-- Not time-filtered on purpose -- always show the latest run regardless of dashboard range.
SELECT TOP 1 status
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl'
ORDER BY log_id DESC;


-- 2. Minutes since last successful run (Stat panel)
-- Catches a scheduler (cron / Task Scheduler) that silently stopped firing -- a per-run
-- success/fail view won't show that, since there are no new rows to look "failed" at all.
-- Wire this to a Grafana alert, not just a colored stat (e.g. threshold 90 for an hourly job).
SELECT DATEDIFF(MINUTE, MAX(end_time), GETUTCDATE()) AS minutes_since_success
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND status = 'Success';


-- 3. Success rate, last 7 days (Stat panel, as %)
SELECT
    100.0 * SUM(CASE WHEN status = 'Success' THEN 1 ELSE 0 END) / COUNT(*) AS success_rate_pct
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND start_time >= DATEADD(DAY, -7, GETUTCDATE());


-- 4. Run duration over time (Time series)
-- Catches performance regressions (e.g. Jira API slowing down, batch size no longer fitting)
-- before they turn into timeouts.
SELECT
    start_time AS time,
    DATEDIFF(SECOND, start_time, end_time) AS duration_seconds
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND $__timeFilter(start_time)
ORDER BY start_time;


-- 5. Rows processed over time (Time series)
-- A sudden drop to near-zero on a "Success" run usually means the JQL window or a field
-- resolution (e.g. Sprint/Team lookup) silently broke, not that Jira ran out of issues.
SELECT
    start_time AS time,
    rows_processed
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND status = 'Success' AND $__timeFilter(start_time)
ORDER BY start_time;


-- 6. Runs per day, success vs failed (stacked Bar chart)
SELECT
    CAST(start_time AS DATE) AS day,
    SUM(CASE WHEN status = 'Success' THEN 1 ELSE 0 END) AS successes,
    SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) AS failures
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND $__timeFilter(start_time)
GROUP BY CAST(start_time AS DATE)
ORDER BY day;


-- 7. Recent failures with error detail (Table)
-- The panel you actually read during an incident.
SELECT TOP 20
    start_time, end_time, rows_processed, error_message
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND status = 'Failed'
ORDER BY log_id DESC;


-- 8. Stuck "Running" rows (Table)
-- Every code path in main.py (success, both specific excepts, and the catch-all) updates
-- the row -- but a hard process kill (OOM, host reboot, taskkill) or a bug in
-- db.finish_pipeline_log itself would leave a row stuck at 'Running' forever. This surfaces
-- that. Worth an alert too: e.g. any row Running for over 60 minutes.
SELECT log_id, start_time, DATEDIFF(MINUTE, start_time, GETUTCDATE()) AS running_minutes
FROM dbo.pipeline_logs
WHERE pipeline_name = 'jira_etl' AND status = 'Running'
  AND start_time < DATEADD(MINUTE, -60, GETUTCDATE());
