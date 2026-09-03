/* ============================================================================
   Jira ETL incremental sync — SQL Server Agent job

   Run this in SSMS, connected to the target MSSQL instance, as sysadmin (or
   a login with SQLAgentOperatorRole/SQLAgentUserRole + rights to create jobs).

   This creates the job ON THIS SQL SERVER INSTANCE ONLY. Cloning the repo
   does not create the job anywhere -- anyone setting this pipeline up
   against a different SQL Server (their own dev instance, a different
   environment, etc.) needs to run this script themselves, after editing the
   ">>> EDIT THESE <<<" values below to match their own machine/setup.
   ============================================================================
   Before running, also confirm:
   1. The SQL Server Agent SERVICE ACCOUNT (Services -> SQL Server Agent
      (<instance>) -> Log On As, or:
        SELECT servicename, service_account FROM sys.dm_server_services
        WHERE servicename LIKE 'SQL Server Agent%';
      ) has:
        - Read access to the project folder below (incl. .env)
        - Permission to execute the python command below
        - Outbound HTTPS (443) to your Jira Cloud site
        - Whatever MSSQL login .env's MSSQL_USER/MSSQL_PASSWORD points to
          (the job step runs as the OS account above, but the pipeline's own
          DB connection still authenticates with the SQL login in .env, so
          that login must exist and have write access to the target DB).
   2. `python` (or whatever @PythonExe is set to) must resolve on that
      service account's PATH -- SQL Agent service accounts often have a
      minimal PATH. Test the exact command manually in a cmd prompt as that
      account if unsure.
   ========================================================================= */

USE msdb;
GO

IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = N'Jira ETL - Incremental Sync')
BEGIN
    EXEC msdb.dbo.sp_delete_job @job_name = N'Jira ETL - Incremental Sync';
END
GO

-- ============================================================================
-- >>> EDIT THESE <<< to match your machine before running
-- ============================================================================
DECLARE @ProjectPath   NVARCHAR(400) = N'C:\Users\SahanyaPogaku\jira-mssql-etl'; -- full path to the cloned repo
DECLARE @PythonExe     NVARCHAR(200) = N'python';   -- or a full path, e.g. C:\Python312\python.exe, if `python` isn't on the Agent service account's PATH
DECLARE @IncrementalDays INT         = 5;           -- lookback window per run (see note below)
DECLARE @ScheduleEveryNDays INT      = 3;           -- how often the job runs
DECLARE @StartTime     INT           = 020000;      -- HHMMSS, 24hr -- pick a low-traffic window
-- ============================================================================

DECLARE @jobId BINARY(16);
DECLARE @Command NVARCHAR(1000) = N'cmd /c "cd /d ' + @ProjectPath + N' && ' + @PythonExe
    + N' main.py --incremental --days ' + CAST(@IncrementalDays AS NVARCHAR(10))
    + N' --log-file jira_etl.log"';

EXEC msdb.dbo.sp_add_job
    @job_name = N'Jira ETL - Incremental Sync',
    @enabled = 1,
    @description = N'Runs jira-mssql-etl incremental sync on a recurring schedule. The --days window should exceed the schedule interval (see @IncrementalDays/@ScheduleEveryNDays above) so one missed/failed run still gets picked up by the next -- safe because every write in the pipeline is idempotent.',
    @category_name = N'[Uncategorized (Local)]',
    @owner_login_name = N'sa',
    @job_id = @jobId OUTPUT;

EXEC msdb.dbo.sp_add_jobstep
    @job_id = @jobId,
    @step_name = N'Run incremental sync',
    @step_id = 1,
    @subsystem = N'CmdExec',
    @command = @Command,
    @on_success_action = 1,   -- quit reporting success
    @on_fail_action = 2,      -- quit reporting failure
    @retry_attempts = 1,
    @retry_interval = 10;     -- minutes before a single retry on failure

EXEC msdb.dbo.sp_update_job
    @job_id = @jobId,
    @start_step_id = 1;

EXEC msdb.dbo.sp_add_schedule
    @schedule_name = N'Jira ETL schedule',
    @freq_type = 4,                          -- daily
    @freq_interval = @ScheduleEveryNDays,    -- every N days
    @active_start_time = @StartTime;

EXEC msdb.dbo.sp_attach_schedule
    @job_id = @jobId,
    @schedule_name = N'Jira ETL schedule';

EXEC msdb.dbo.sp_add_jobserver
    @job_id = @jobId,
    @server_name = N'(local)';  -- change if targeting a named instance/remote server
GO
