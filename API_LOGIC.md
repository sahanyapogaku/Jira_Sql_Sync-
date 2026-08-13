# API Logic

Every Jira Cloud REST API call this pipeline makes, its pagination style, and
its rate-limit/backoff behavior. All of this lives in `jira_client.py`; this
document explains the *why*, not just the *what*.

## Endpoints called

| Endpoint | Method | Called from | Paginated? |
|---|---|---|---|
| `/rest/api/3/project/search` | GET | `list_projects()` | Yes — offset (`isLast`) |
| `/rest/api/3/status` | GET | `list_statuses()` | No — full list in one response |
| `/rest/api/3/field` | GET | `list_fields()` | No — full list in one response |
| `/rest/api/3/search/jql` | POST | `search_issues()` | Yes — token (`nextPageToken`) |
| `/rest/api/3/issue/{key}/changelog` | GET | `get_changelog()` | Yes — offset (`isLast`) |
| `/rest/api/3/issue/{key}/comment` | GET | `get_comments()` | Yes — offset (`total`) |
| `/rest/api/3/issue/{key}/remotelink` | GET | `get_remote_links()` | No — full list in one response |
| `/rest/api/3/issue/{key}/worklog` | GET | `get_worklogs()` | Yes — offset (`total`) |

`search_issues()` is called once per run with a JQL filter built by
`main.build_jql()` (`updated >= -{days}d` for `--incremental`, or an
open-ended `updated >= "1900-01-01 00:00"` for `--full`). Every other
endpoint above is then called once **per issue** returned by that search —
this is why full syncs take tens of minutes: it's not one bulk call, it's
`1 + 4*N` requests for `N` issues (changelog + comment + remotelink +
worklog per issue, on top of the search pages themselves).

## Pagination handling

Three distinct styles are in play, deliberately not unified into one helper
since Jira Cloud itself doesn't unify them:

**1. Token-based (`/search/jql` only)** — Jira Cloud's current recommended
search pagination. The response includes `nextPageToken`; each subsequent
request echoes it back in the request body. Loop condition:
```python
is_last = data.get("isLast", next_page_token is None)
if is_last or not next_page_token or not issues:
    break
```
Page size: `SEARCH_PAGE_SIZE = 100`.

**2. Offset-based via `isLast`** (`project/search`, `changelog`) — classic
`startAt`/`maxResults` request params; the response's `isLast` flag (or its
absence, inferred from `startAt >= total`) signals the end. `startAt`
increments by the number of values actually returned each page, not a fixed
step, so a short final page still terminates correctly.

**3. Offset-based via `total`** (`comment`, `worklog`) — same `startAt`/
`maxResults` params, but the loop instead compares accumulated `startAt`
against the response's `total` count, with no `isLast` field to lean on.
Page size for all of these: `SUB_RESOURCE_PAGE_SIZE = 100`.

**Not paginated at all** (`status`, `field`, `remotelink`): Jira returns the
complete result in a single response, so there's nothing to loop over.

## Rate-limit / backoff behavior

All of this lives in `JiraClient._request()`, the single choke point every
call above goes through:

| Response | Behavior |
|---|---|
| **401 / 403** | Raises `JiraAuthError` immediately — never retried. A bad token/email won't fix itself by waiting, so retrying would just delay an obvious failure. |
| **429** | Retried. Honors the `Retry-After` header if Jira sends one; otherwise exponential backoff (`BASE_BACKOFF_SECONDS * 2^(attempt-1)`). |
| **5xx** | Retried with the same exponential backoff as 429. |
| **Network/connection error** (`requests.exceptions.RequestException`) | Retried with the same exponential backoff — this is what catches a mid-run network blip or DNS failure (see `TROUBLESHOOTING.md`). |
| **Other 4xx** (400, 404, etc.) | Raises `JiraAPIError` immediately — a client-side request problem, not transient. |

Backoff constants: `MAX_RETRIES = 6`, `BASE_BACKOFF_SECONDS = 2`, giving a
backoff sequence of roughly 2s, 4s, 8s, 16s, 32s across attempts 1–5 before
the 6th and final attempt is made. Exhausting all 6 raises `JiraAPIError`,
which `main.py` treats as unrecoverable for that run (see `PROCESS.md` and
`TROUBLESHOOTING.md`).

A single issue's changelog/comment/remotelink/worklog fetch exhausting its
retries does **not** abort the whole run — `main.run()` catches
`JiraAPIError` per sub-resource call, logs a warning, and continues to the
next issue. Only a failure in `search_issues()` itself (the primary issue
list) is treated as fatal for the run.

## Change management for API deprecations

Atlassian publishes Jira Cloud REST API deprecation notices via the
[developer changelog](https://developer.atlassian.com/changelog/) and the
Jira Cloud platform release notes — check periodically, or subscribe if the
changelog offers an RSS/email option.

This pipeline is already ahead of one known deprecation: it uses
`/rest/api/3/search/jql` (Jira's current recommended search endpoint), not
the older `/rest/api/3/search` that Atlassian has deprecated in favor of it.

When a new deprecation notice lands that touches an endpoint in the table
above:
1. Check the deprecation's sunset date against how urgent the change is.
2. Test the affected call against a non-production Jira site if available,
   or cautiously against production with `--incremental --days 1` (small
   blast radius) before a `--full` run.
3. Update the call in `jira_client.py` and any shape assumptions in
   `transform.py` that depend on its response format.
4. Update this document to match.

**Owner:** _fill in who's responsible for watching this and acting on
notices — not something this document can decide on its own._
