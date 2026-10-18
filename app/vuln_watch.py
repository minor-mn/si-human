"""vuln-watchの最新情報APIを読む。外部ソース取得・要約・ファイル保存はしない。"""

import os
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.comments import ThreadComments
from app.news import NewsBriefing

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class BriefingUnavailable(Exception):
    pass


class BriefingResponse(BaseModel):
    version: Literal[1]
    news: NewsBriefing
    threads: list[ThreadComments] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self):
        for date in (self.news.as_of, self.news.since):
            if datetime.fromisoformat(date).tzinfo is None:
                raise ValueError("基準日時にタイムゾーンがありません")
        if len(self.threads) != len(self.news.items):
            raise ValueError("話題とコメント資料の件数が一致しません")
        for item, thread in zip(self.news.items, self.threads):
            url = urlsplit(item.source.url)
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
                raise ValueError("出典URLが不正です")
            if thread.url != item.source.url or item.source.kind == "mailing_list_reply":
                raise ValueError("話題とコメント資料の対応が不正です")
            published = datetime.fromisoformat(item.source.published_at)
            if published.tzinfo is None:
                raise ValueError("公開日時にタイムゾーンがありません")
        return self


async def fetch_briefing() -> BriefingResponse:
    url = os.environ.get("VULN_WATCH_API_URL", "http://127.0.0.1:3000/api/briefing")
    sources = os.environ.get("VULN_WATCH_SOURCES")
    params = {"sources": sources} if sources is not None else {}
    for parameter in ("hours", "limit"):
        value = os.environ.get(f"VULN_WATCH_{parameter.upper()}")
        if value is not None:
            params[parameter] = value
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", url, params=params or None) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise BriefingUnavailable("APIの応答が大きすぎます")
        return BriefingResponse.model_validate_json(data)
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        # URLや応答本文には認証情報が含まれ得るので、そのままログに出さない。
        raise BriefingUnavailable(f"APIの取得または応答検証に失敗しました（{type(exc).__name__}）") from exc
