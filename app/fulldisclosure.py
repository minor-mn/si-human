"""Full Disclosureの公開投稿と、その投稿への返信関係を読む。"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


def message_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "seclists.org"
                or parsed.username or parsed.password or parsed.port not in {None, 443}
                or not re.fullmatch(r"/fulldisclosure/\d{4}/(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)/\d+", parsed.path)):
            return None
        return urlunsplit(("https", "seclists.org", parsed.path, "", ""))
    except ValueError:
        return None


@dataclass
class Message:
    title: str
    text: str
    parents: dict[str, str | None]

    def replies_to(self, url: str) -> list[str]:
        replies = []
        for candidate in self.parents:
            current, seen = candidate, set()
            while current in self.parents and current not in seen:
                seen.add(current)
                current = self.parents[current]
                if current == url:
                    replies.append(candidate)
                    break
        return replies


class _MessageParser(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.in_body = False
        self.body_seen = False
        self.body_ended = False
        self.in_title = False
        self.ignored = 0
        self.body: list[str] = []
        self.title: list[str] = []
        self.thread_depth = 0
        self.stack: list[dict] = []
        self.rows: list[dict] = []

    def handle_comment(self, data: str) -> None:
        if data.strip() == "X-Body-of-Message":
            self.in_body = self.body_seen = True
        elif data.strip() == "X-Body-of-Message-End":
            self.in_body = False
            self.body_ended = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "h1" and "m-title" in classes and not self.body_seen:
            self.in_title = True
        if self.in_body:
            if tag in {"script", "style", "blockquote"}:
                self.ignored += 1
            if tag in {"br", "p", "div", "li"}:
                self.body.append("\n")
        if not self.body_ended:
            return
        if tag == "ul" and (self.thread_depth or "thread" in classes):
            self.thread_depth += 1
        elif self.thread_depth and tag == "li":
            row = {"url": None, "parent": self.stack[-1] if self.stack else None, "linked": False}
            self.stack.append(row)
            self.rows.append(row)
        elif self.thread_depth and self.stack:
            if tag == "a":
                self.stack[-1]["linked"] = True
                target = message_url(urljoin(self.url, attributes.get("href") or ""))
                if target:
                    self.stack[-1]["url"] = target
            elif tag == "strong" and not self.stack[-1]["linked"]:
                # MHonArcは表示中の投稿をリンクの代わりにstrongで示す。
                self.stack[-1]["url"] = self.url

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.in_title = False
        if self.in_body:
            if tag in {"script", "style", "blockquote"} and self.ignored:
                self.ignored -= 1
            if tag in {"p", "div", "li"}:
                self.body.append("\n")
        if self.thread_depth:
            if tag == "li" and self.stack:
                self.stack.pop()
            elif tag == "ul":
                self.thread_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        if self.in_body and not self.ignored:
            self.body.append(data)


def parse_message(html: str, url: str) -> Message:
    canonical = message_url(url)
    if canonical is None:
        raise ValueError("Full Disclosureの投稿URLが不正です")
    parser = _MessageParser(canonical)
    parser.feed(html)
    parents = {
        row["url"]: row["parent"]["url"] if row["parent"] else None
        for row in parser.rows if row["url"]
    }
    title = " ".join("".join(parser.title).split())
    text = "".join(parser.body).strip()
    if not parser.body_seen or not parser.body_ended or not title or not text or canonical not in parents:
        raise ValueError("Full Disclosureの投稿本文または返信一覧を確認できませんでした")
    return Message(title, text, parents)
