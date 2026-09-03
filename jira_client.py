"""Jira Cloud REST API client: project discovery, issue search, changelog, worklogs.

Handles Jira Cloud's token-based /search/jql pagination, offset-based pagination
for changelog/worklog sub-resources, and retry-with-backoff on 429s / transient
network errors. Auth failures (401/403) are raised immediately, never retried.
"""

import logging
import time

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger("jira_etl.jira_client")

SEARCH_PAGE_SIZE = 100
SUB_RESOURCE_PAGE_SIZE = 100
MAX_RETRIES = 6
BASE_BACKOFF_SECONDS = 2


class JiraAuthError(Exception):
    """Raised on 401/403 — never retried, should abort the run."""


class JiraAPIError(Exception):
    """Raised when a request exhausts all retries."""


class JiraClient:
    def __init__(self, base_url, email, api_token, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(email, api_token)
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise JiraAPIError(f"{method} {path} failed after {attempt} attempts: {exc}") from exc
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("Network error on %s %s (attempt %d/%d): %s — retrying in %ds",
                               method, path, attempt, MAX_RETRIES, exc, backoff)
                time.sleep(backoff)
                continue

            if resp.status_code in (401, 403):
                raise JiraAuthError(
                    f"{method} {path} returned {resp.status_code} — check JIRA_EMAIL/JIRA_API_TOKEN "
                    f"and that the token has access to this site."
                )

            if resp.status_code == 429:
                if attempt >= MAX_RETRIES:
                    raise JiraAPIError(f"{method} {path} rate-limited after {attempt} attempts")
                retry_after = resp.headers.get("Retry-After")
                backoff = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("429 rate limited on %s %s — retrying in %.1fs (attempt %d/%d)",
                               method, path, backoff, attempt, MAX_RETRIES)
                time.sleep(backoff)
                continue

            if resp.status_code >= 500:
                if attempt >= MAX_RETRIES:
                    raise JiraAPIError(f"{method} {path} returned {resp.status_code} after {attempt} attempts")
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("Server error %d on %s %s — retrying in %ds (attempt %d/%d)",
                               resp.status_code, method, path, backoff, attempt, MAX_RETRIES)
                time.sleep(backoff)
                continue

            if resp.status_code >= 400:
                raise JiraAPIError(f"{method} {path} returned {resp.status_code}: {resp.text[:500]}")

            return resp

    def list_projects(self):
        """Discover all projects visible to this API token. Returns list of {key, name, id}."""
        projects = []
        start_at = 0
        page_size = 50
        while True:
            resp = self._request(
                "GET",
                "/rest/api/3/project/search",
                params={"startAt": start_at, "maxResults": page_size},
            )
            data = resp.json()
            for proj in data.get("values", []):
                projects.append({"key": proj["key"], "name": proj.get("name"), "id": proj.get("id")})
            if data.get("isLast", True) or not data.get("values"):
                break
            start_at += page_size
        logger.info("Discovered %d projects: %s", len(projects), ", ".join(p["key"] for p in projects))
        return projects

    def list_statuses(self):
        """Discover all statuses and their categories visible to this API token."""
        resp = self._request("GET", "/rest/api/3/status")
        data = resp.json()
        statuses = []
        for s in data:
            cat = s.get("statusCategory") or {}
            statuses.append({
                "status_id": str(s["id"]),
                "status_name": s.get("name"),
                "category_id": cat.get("id"),
                "category_key": cat.get("key"),
                "category_name": cat.get("name"),
                "color_name": cat.get("colorName"),
            })
        logger.info("Discovered %d statuses", len(statuses))
        return statuses

    def list_fields(self):
        """Discover all fields (system + custom) visible to this API token. Returns list of {id, name, type}."""
        resp = self._request("GET", "/rest/api/3/field")
        data = resp.json()
        fields = [{"id": f["id"], "name": f.get("name"), "type": (f.get("schema") or {}).get("type")} for f in data]
        logger.info("Discovered %d fields (%d custom)", len(fields), sum(1 for f in data if f.get("custom")))
        return fields

    def search_issues(self, jql, fields="*all"):
        """Generator yielding issue dicts, paginated via nextPageToken (Jira Cloud /search/jql)."""
        next_page_token = None
        total_fetched = 0
        while True:
            body = {
                "jql": jql,
                "maxResults": SEARCH_PAGE_SIZE,
                "fields": [fields] if isinstance(fields, str) else fields,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token

            resp = self._request("POST", "/rest/api/3/search/jql", json=body)
            data = resp.json()
            issues = data.get("issues", [])
            for issue in issues:
                yield issue
            total_fetched += len(issues)

            next_page_token = data.get("nextPageToken")
            is_last = data.get("isLast", next_page_token is None)
            logger.info("Fetched page of %d issues (total so far: %d)", len(issues), total_fetched)
            if is_last or not next_page_token or not issues:
                break

    def get_changelog(self, issue_key):
        """Generator yielding changelog history entries: {id, author, created, items: [...]}."""
        start_at = 0
        while True:
            resp = self._request(
                "GET",
                f"/rest/api/3/issue/{issue_key}/changelog",
                params={"startAt": start_at, "maxResults": SUB_RESOURCE_PAGE_SIZE},
            )
            data = resp.json()
            values = data.get("values", [])
            for entry in values:
                yield entry
            start_at += len(values)
            total = data.get("total", start_at)
            if data.get("isLast", start_at >= total) or not values:
                break
    def get_comments(self, issue_key):
        """Generator yielding comment entries: {id, author, body, created, updated}."""
        start_at = 0
        while True:
            resp = self._request(
                "GET",
                f"/rest/api/3/issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": SUB_RESOURCE_PAGE_SIZE},
            )
            data = resp.json()
            values = data.get("comments", [])
            for entry in values:
                yield entry
            start_at += len(values)
            total = data.get("total", start_at)
            if start_at >= total or not values:
                break

    def get_remote_links(self, issue_key):
        """Remote links (Confluence pages, web URLs, relationship links like "Approved") for
        an issue: {id, globalId, relationship, object: {url, title, ...}}. Not paginated —
        Jira returns the full list in one response.
        """
        resp = self._request("GET", f"/rest/api/3/issue/{issue_key}/remotelink")
        return resp.json()

    def get_worklogs(self, issue_key):
        """Generator yielding worklog entries: {id, author, timeSpentSeconds, started, comment, ...}."""
        start_at = 0
        while True:
            resp = self._request(
                "GET",
                f"/rest/api/3/issue/{issue_key}/worklog",
                params={"startAt": start_at, "maxResults": SUB_RESOURCE_PAGE_SIZE},
            )
            data = resp.json()
            values = data.get("worklogs", [])
            for entry in values:
                yield entry
            start_at += len(values)
            total = data.get("total", start_at)
            if start_at >= total or not values:
                break
