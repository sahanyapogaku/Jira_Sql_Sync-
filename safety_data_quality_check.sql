-- Data-quality check for MFG-264's safety dashboard: field population rates
-- across SAFE "Report" issues, and anything that would silently break the
-- leading/lagging classification in dbo.safety_reports.
--
-- Run against the same MSSQL database this pipeline loads into. No schema
-- changes; read-only against dbo.safety_reports (see schema.sql) plus the
-- raw dbo.jira_custom_field_values table it's built from.

-- 1. Overall population rate for the 3 fields the dashboard depends on.
SELECT
    COUNT(*)                                                          AS total_report_issues,
    SUM(CASE WHEN safety_report_type IS NULL THEN 1 ELSE 0 END)       AS missing_safety_report_type,
    SUM(CASE WHEN osha_recordable IS NULL THEN 1 ELSE 0 END)          AS missing_osha_recordable,
    SUM(CASE WHEN occurrence_date IS NULL THEN 1 ELSE 0 END)          AS missing_occurrence_date,
    CAST(100.0 * SUM(CASE WHEN occurrence_date IS NULL THEN 1 ELSE 0 END) / COUNT(*) AS DECIMAL(5,1))
                                                                       AS pct_missing_occurrence_date
FROM dbo.safety_reports;

-- 2. Which specific issues are missing which field -- for a spot check /
--    following up with whoever logged them, not just a raw count.
SELECT
    issue_key,
    status,
    created_date,
    safety_report_type,
    osha_recordable,
    occurrence_date,
    CASE WHEN safety_report_type IS NULL THEN 'MISSING: Safety Report Type' END AS gap_1,
    CASE WHEN osha_recordable IS NULL THEN 'MISSING: OSHA Recordable' END       AS gap_2,
    CASE WHEN occurrence_date IS NULL THEN 'MISSING: Date Time of Occurrence' END AS gap_3
FROM dbo.safety_reports
WHERE safety_report_type IS NULL OR osha_recordable IS NULL OR occurrence_date IS NULL
ORDER BY created_date DESC;

-- 3. Values actually seen in Safety Report Type that DON'T map to a category
--    in dbo.safety_report_type_lookup -- if this returns any rows, either a
--    new option value was added in Jira (EHS added a 7th report type) or
--    there's a casing/typo mismatch, and every issue with that value would
--    silently show leading_or_lagging = NULL on the dashboard until the
--    lookup table is updated.
SELECT DISTINCT safety_report_type, COUNT(*) AS issue_count
FROM dbo.safety_reports
WHERE safety_report_type IS NOT NULL AND leading_or_lagging IS NULL
GROUP BY safety_report_type;

-- 4. Distribution of report types actually in use, for context (e.g. is the
--    leading:lagging ratio chart going to have any "Lagging" data at all
--    right now, or is every issue so far a leading-indicator report?).
SELECT safety_report_type, leading_or_lagging, COUNT(*) AS issue_count
FROM dbo.safety_reports
GROUP BY safety_report_type, leading_or_lagging
ORDER BY leading_or_lagging, issue_count DESC;
