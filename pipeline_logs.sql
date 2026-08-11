-- Pipeline-run metadata (start/end time, status, rows processed) for Grafana visualization.
-- Idempotent, same convention as schema.sql: safe to run every startup, only creates the
-- table if it doesn't already exist. Kept as a separate file from schema.sql since this is
-- operational pipeline-run logging, not part of the Jira data model.

IF OBJECT_ID('dbo.pipeline_logs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.pipeline_logs (
        log_id          INT IDENTITY PRIMARY KEY,
        pipeline_name   VARCHAR(100),
        start_time      DATETIME,
        end_time        DATETIME,
        status          VARCHAR(20),
        rows_processed  INT,
        error_message   NVARCHAR(MAX)
    );
END
