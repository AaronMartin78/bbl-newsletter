"""Polite HTTP fetch layer.

Every request goes through one Fetcher: robots.txt is checked per host
(cached), conditional GET headers are sent when the caller has ETag or
Last-Modified state, responses are size-capped, and requests to the same
host are spaced by a minimum interval. The httpx transport is injectable
so tests run against a mock, never the network.
"""
from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import httpx

DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB per response


@dataclass
class FetchResult:
    url: str
    status: int
    content: Optional[bytes]  # None on not_modified, robots_denied or error
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False
    robots_denied: bool = False
    error: Optional[str] = None


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout_s: float = 15.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        min_host_interval_s: float = 2.0,
        obey_robots: bool = True,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.min_host_interval_s = min_host_interval_s
        self.obey_robots = obey_robots
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request: dict[str, float] = {}

    # -- robots ------------------------------------------------------------
    def _robots_for(self, host: str) -> urllib.robotparser.RobotFileParser:
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self._client.get(f"https://{host}/robots.txt")
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                elif 400 <= resp.status_code < 500:
                    # RFC 9309 2.3.1.3: an unavailable robots.txt (any 4xx,
                    # 401/403 included) means crawling is unrestricted. The
                    # games API host 401s every path, robots.txt too.
                    rp.allow_all = True
                else:  # 5xx: robots exists but is unreachable, deny this run
                    rp.disallow_all = True
            except httpx.HTTPError:
                # Unreachable robots: be conservative, deny this run.
                rp.disallow_all = True
            self._robots[host] = rp
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        host = urlsplit(url).netloc
        return self._robots_for(host).can_fetch(self.user_agent, url)

    # -- fetch -------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_host_interval_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> FetchResult:
        if not self.allowed(url):
            return FetchResult(url=url, status=0, content=None, robots_denied=True)

        headers = dict(headers or {})
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        host = urlsplit(url).netloc
        self._throttle(host)
        try:
            with self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 304:
                    return FetchResult(
                        url=url, status=304, content=None, not_modified=True,
                        etag=etag, last_modified=last_modified,
                    )
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        return FetchResult(
                            url=url, status=resp.status_code, content=None,
                            error=f"response exceeded {self.max_bytes} bytes",
                        )
                    chunks.append(chunk)
                return FetchResult(
                    url=url,
                    status=resp.status_code,
                    content=b"".join(chunks) if resp.status_code == 200 else None,
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                    error=None if resp.status_code == 200 else f"http_{resp.status_code}",
                )
        except httpx.HTTPError as exc:
            return FetchResult(url=url, status=0, content=None, error=str(exc))

    def close(self) -> None:
        self._client.close()
