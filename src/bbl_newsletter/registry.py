"""Load and validate the source registry (newsletter_sources.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

SourceType = Literal[
    "league", "club", "regional_public", "regional_commercial", "aggregator"
]
SourceStatus = Literal["ready", "conditional", "pending_check", "disabled"]
Method = Literal["rss", "sitemap", "html_listing", "unknown"]


class Defaults(BaseModel):
    user_agent: str
    obey_robots: bool = True
    conditional_get: bool = True
    request_timeout_s: int = 15
    max_articles_per_source_per_run: int = 20


class Source(BaseModel):
    id: str
    publisher: str
    type: SourceType
    status: SourceStatus
    method: Method = "unknown"
    url: Optional[str] = None
    feed: Optional[str] = None
    sitemap: Optional[str] = None
    article_host: Optional[str] = None
    article_url_pattern: Optional[str] = None  # regex; html_listing link filter
    exclude_url_pattern: Optional[str] = None  # regex; discovered URLs matching it are never fetched
    language: str = "de"
    covers: list[str] = Field(default_factory=list)
    robots: Optional[str] = None
    reason: Optional[str] = None

    @property
    def entry_point(self) -> Optional[str]:
        """The URL the collector polls for this source, by method."""
        if self.method == "rss":
            return self.feed
        if self.method == "sitemap":
            return self.sitemap
        return self.url


class Registry(BaseModel):
    version: int
    defaults: Defaults
    sources: list[Source]

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        reg = cls.model_validate(data)
        ids = [s.id for s in reg.sources]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate source ids in registry: {sorted(dupes)}")
        return reg

    def usable(self) -> list[Source]:
        """Sources the collector may poll now. `pending_check` sources join
        only after their runtime robots check passes; `disabled` never."""
        return [s for s in self.sources if s.status in ("ready", "conditional")]

    def pending(self) -> list[Source]:
        return [s for s in self.sources if s.status == "pending_check"]

    def by_id(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)
