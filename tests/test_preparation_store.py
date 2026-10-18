import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.comments import Comment, ThreadComments
from app.news import JST, SELECTION_POLICY, Article, NewsBriefing, NewsItem, Source
from app.preparation_store import PreparationStore, StoredPreparation


PREPARATION_ID = "preparation_test_12345678"


class PreparationStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "preparations"
        self.store = PreparationStore(self.directory)
        self.now = datetime(2026, 10, 18, 4, tzinfo=JST)
        source = Source("AIの話題", "https://slashdot.org/story/test", "Slashdot", self.now.isoformat(), "discussion")
        self.preparation = StoredPreparation(
            preparation_id=PREPARATION_ID,
            prepared_at=self.now,
            provider="openai",
            news=NewsBriefing(
                "2026-10-17T04:00:00+09:00", self.now.isoformat(), "日本語の要約です。",
                [NewsItem("日本語の要約です。", source, "質問に答えるための記事の詳細です。")],
            ),
            threads=[ThreadComments(
                source.title, source.url, self.now.isoformat(), "available",
                [Comment("123", "意見", "取得したコメントです。", source.url + "#comment_123", 5, None, True)],
            )],
        )

    @property
    def path(self):
        return self.directory / f"{PREPARATION_ID}.json"

    def test_json_round_trip_keeps_topics_sources_and_structured_comments(self):
        self.store.save(self.preparation)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("日本語の要約です。", text)
        self.assertIn("取得したコメントです。", text)
        data = json.loads(text)
        self.assertEqual(data["version"], 1)
        self.assertFalse(data["used"])
        self.assertFalse({"session_token", "api_key", "system_prompt", "turns"} & data.keys())
        restored = self.store.load(PREPARATION_ID)
        self.assertEqual(restored.news.items[0].source.publisher, "Slashdot")
        self.assertEqual(restored.news.items[0].article_summary, "質問に答えるための記事の詳細です。")
        self.assertEqual(restored.threads[0].comments[0].score, 5)
        self.assertTrue(restored.threads[0].comments[0].truncated)
        self.assertEqual(restored.prepared_at, self.now)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_previously_saved_topics_without_article_excerpt_still_load(self):
        self.store.save(self.preparation)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        del data["news"]["items"][0]["article_summary"]
        del data["news"]["items"][0]["advisory"]
        del data["news"]["articles"]
        del data["news"]["selection_policy"]
        del data["threads"][0]["kind"]
        del data["threads"][0]["partial"]
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        restored = self.store.load(PREPARATION_ID)
        self.assertEqual(restored.news.items[0].article_summary, "")
        self.assertIsNone(restored.news.items[0].advisory)
        self.assertEqual(restored.news.text, self.preparation.news.text)
        self.assertEqual(restored.news.articles, [])
        self.assertEqual(restored.news.selection_policy, "last_24_hours")
        self.assertEqual(restored.threads[0].kind, "comments")
        self.assertFalse(restored.threads[0].partial)

    def test_fallback_policy_and_old_publication_dates_survive_restoration(self):
        old = self.now - timedelta(days=7)
        source = Source("Report", "https://seclists.org/fulldisclosure/2026/Oct/1", "Full Disclosure", old.isoformat(), "mailing_list")
        self.preparation.news = NewsBriefing(
            old.isoformat(), self.now.isoformat(), "脆弱性が報告されました。", [NewsItem("脆弱性が報告されました。", source)],
            [Article(source.title, "説明", source.url, source.publisher, old, source.kind)], SELECTION_POLICY,
        )
        self.store.save(self.preparation)
        restored = self.store.load(PREPARATION_ID)
        self.assertEqual(restored.news.selection_policy, SELECTION_POLICY)
        self.assertEqual(restored.news.since, old.isoformat())
        self.assertEqual(restored.news.items[0].source.published_at, old.isoformat())
        self.assertEqual(restored.news.articles[0].published_at, old)
        self.assertIn("古い投稿を含む", restored.news.conversation_context())

    def test_advisory_identifiers_and_fix_versions_survive_json_and_conversation_context(self):
        source = Source("Package vulnerability", "https://github.com/advisories/GHSA-abcd-1234-5678", "GitHub Advisory Database", self.now.isoformat(), "advisory")
        details = {"ghsa_id": "GHSA-abcd-1234-5678", "cve_id": "CVE-2026-12345", "severity": "high",
                   "vulnerabilities": [{"package": {"ecosystem": "pip", "name": "sample"},
                                        "vulnerable_version_range": "<1.2.3", "first_patched_version": "1.2.3"}]}
        self.preparation.news.items = [NewsItem("パッケージに脆弱性が報告されました。", source, "説明", details)]
        self.preparation.news.articles = [Article(source.title, "説明", source.url, source.publisher, self.now, "advisory", details)]
        self.store.save(self.preparation)
        restored = self.store.load(PREPARATION_ID)
        self.assertEqual(restored.news.items[0].advisory, details)
        self.assertEqual(restored.news.articles[0].advisory, details)
        self.assertIn('"first_patched_version": "1.2.3"', restored.news.conversation_context())

    def test_failed_replacement_preserves_previous_json_and_removes_temporary_file(self):
        self.store.save(self.preparation)
        with patch("app.preparation_store.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.save(self.preparation.model_copy(update={"warnings": ["変更"]}))
        self.assertEqual(self.store.load(PREPARATION_ID).warnings, [])
        self.assertEqual([path.name for path in self.directory.iterdir()], [self.path.name])

    def test_invalid_id_cannot_read_arbitrary_paths(self):
        with patch("pathlib.Path.open", side_effect=AssertionError("must not read")):
            for identifier in ("../persona.yaml", "/etc/passwd", "..", "id/child", "a" * 129):
                with self.subTest(identifier=identifier):
                    self.assertIsNone(self.store.load(identifier))

    def test_corrupt_or_unsupported_json_is_preserved(self):
        self.directory.mkdir()
        for text in ("{broken", '{"version": 99}', self.preparation.model_dump_json().replace(PREPARATION_ID, "a_different_preparation_id")):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertLogs("app.preparation_store", level="WARNING"):
                    self.assertIsNone(self.store.load(PREPARATION_ID))
                self.assertEqual(self.path.read_text(), text)

    def test_used_record_keeps_the_topic_archive(self):
        self.store.save(self.preparation.model_copy(update={"used": True}))
        restored = self.store.load(PREPARATION_ID)
        self.assertTrue(restored.used)
        self.assertEqual(restored.news.text, self.preparation.news.text)
        self.assertEqual(restored.threads[0].comments[0].text, "取得したコメントです。")

    def test_oversized_file_is_rejected_without_modifying_it(self):
        self.directory.mkdir()
        self.path.write_bytes(b" " * 1025)
        with patch("app.preparation_store.MAX_PREPARATION_BYTES", 1024), self.assertLogs("app.preparation_store", level="WARNING"):
            self.assertIsNone(self.store.load(PREPARATION_ID))
        self.assertEqual(self.path.stat().st_size, 1025)

    def test_archives_larger_than_the_previous_two_megabyte_limit_are_restorable(self):
        self.preparation.news.articles = [Article("Archive", "x" * 2_100_000, "https://example.com/archive", "test", self.now)]
        self.store.save(self.preparation)
        restored = self.store.load(PREPARATION_ID)
        self.assertEqual(restored.news.articles, self.preparation.news.articles)

    def test_unrestorable_size_is_rejected_before_replacing_the_previous_file(self):
        self.store.save(self.preparation)
        previous = self.path.read_bytes()
        with patch("app.preparation_store.MAX_PREPARATION_BYTES", len(previous)):
            with self.assertRaises(OSError):
                self.store.save(self.preparation.model_copy(update={"warnings": ["x" * 1000]}))
        self.assertEqual(self.path.read_bytes(), previous)
