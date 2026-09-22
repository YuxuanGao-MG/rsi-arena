"""Benzinga news items, as Alpaca serves them: timestamped to the second.

An item is a fact at ``created_at``. Its text may be edited afterwards -
``updated_at`` moves - and the API serves the edited text, so a headline
read at replay time can differ from the one a trader saw at the instant.
That is accepted and recorded: ``edited_after`` is set on the item, and the
question set carries it, so a reader of the results can weigh a story that
was rewritten after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ._bars import parse_instant, rfc3339
from ._client import AlpacaData

NEWS_PATH = "/v1beta1/news"
PAGE_MAX = 50


@dataclass(frozen=True)
class NewsItem:
    id: str
    created_at: datetime
    updated_at: datetime
    headline: str
    summary: str
    source: str
    symbols: tuple[str, ...]
    url: str = ""
    #: ``updated_at > created_at``: the text served now may not be the text
    #: that existed at the instant.
    edited_after: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "NewsItem":
        created = parse_instant(raw["created_at"])
        updated = parse_instant(raw.get("updated_at") or raw["created_at"])
        return cls(id=str(raw["id"]), created_at=created, updated_at=updated,
                   headline=str(raw.get("headline") or ""), summary=str(raw.get("summary") or ""),
                   source=str(raw.get("source") or ""),
                   symbols=tuple(str(s).upper() for s in (raw.get("symbols") or [])),
                   url=str(raw.get("url") or ""), edited_after=updated > created)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "created_at": self.created_at.isoformat(),
                "updated_at": self.updated_at.isoformat(), "headline": self.headline,
                "summary": self.summary, "source": self.source, "symbols": list(self.symbols),
                "url": self.url, "edited_after": self.edited_after}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NewsItem":
        created = parse_instant(d["created_at"])
        updated = parse_instant(d.get("updated_at") or d["created_at"])
        return cls(id=str(d["id"]), created_at=created, updated_at=updated,
                   headline=d.get("headline", ""), summary=d.get("summary", ""),
                   source=d.get("source", ""), symbols=tuple(d.get("symbols") or ()),
                   url=d.get("url", ""),
                   edited_after=bool(d.get("edited_after", updated > created)))


class AlpacaNews:
    """The news endpoint. ``items`` is bounded by the API's ``end``, which is
    what makes ``news_before`` honest at an instant."""

    def __init__(self, client: AlpacaData | None = None) -> None:
        self.client = client or AlpacaData()

    def items(self, symbols: list[str] | tuple[str, ...], start: datetime, end: datetime,
              limit: int = PAGE_MAX, sort: str = "asc", max_items: int | None = None
              ) -> list[NewsItem]:
        """Items on ``symbols`` created in ``[start, end]``, following pages
        until there are none or ``max_items`` is reached."""
        rows = self.client.collect(NEWS_PATH, "news", {
            "symbols": ",".join(s.upper() for s in symbols), "start": rfc3339(start),
            "end": rfc3339(end), "limit": max(1, min(int(limit), PAGE_MAX)), "sort": sort,
            "include_content": "false"}, max_items=max_items)
        return [NewsItem.from_api(r) for r in rows]


__all__ = ["NewsItem", "AlpacaNews", "NEWS_PATH", "PAGE_MAX"]
