"""A deliberately slow, well-identified HTTP client.

One session, one delay per host, a descriptive User-Agent, and robots.txt
enforcement. Nothing here should ever be bypassed for convenience.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from .config import Config

log = logging.getLogger(__name__)


class RobotsDenied(RuntimeError):
    """Raised when robots.txt forbids a URL we were asked to fetch."""


class PoliteSession:
    def __init__(self, cfg: Config, honor_robots: bool | None = None):
        self.cfg = cfg
        self.honor_robots = (
            cfg.sources["flags"]["honor_robots"] if honor_robots is None else honor_robots
        )
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.user_agent
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # -- robots ---------------------------------------------------------------
    def _robots_for(self, url: str):
        host = urlparse(url).netloc
        scheme = urlparse(url).scheme
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=self.cfg.timeout)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                # No robots.txt is an allow-all, per the standard.
                rp.parse([])
        except requests.RequestException:
            log.warning("could not fetch %s; assuming allow-all", robots_url)
            rp.parse([])
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.honor_robots:
            return True
        rp = self._robots_for(url)
        return rp.can_fetch(self.cfg.user_agent, url) or rp.can_fetch("*", url)

    # -- fetching -------------------------------------------------------------
    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.cfg.delay_seconds:
                time.sleep(self.cfg.delay_seconds - gap)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, *, params: dict | None = None, **kw) -> requests.Response:
        if not self.allowed(url):
            raise RobotsDenied(f"robots.txt disallows {url}")
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            self._wait(url)
            try:
                resp = self.session.get(
                    url, params=params, timeout=self.cfg.timeout, **kw
                )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("attempt %d for %s failed: %s", attempt, url, exc)
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 500:
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                log.warning("attempt %d for %s: HTTP %d", attempt, url, resp.status_code)
                time.sleep(2 * attempt)
                continue
            return resp
        raise RuntimeError(f"giving up on {url}") from last_exc

    def get_json(self, url: str, *, params: dict | None = None):
        resp = self.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
