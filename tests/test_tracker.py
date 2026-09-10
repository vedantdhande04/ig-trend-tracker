"""Unit tests for the 14-day pool retention filter (prune + matches) and the
reel URL enrichment step (enrichment_urls).

The retention tests use a throwaway temp DB per test — real state.db and
state_mock.db are never touched.
Run:  python -m unittest discover -s tests -v
"""
import datetime
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import tracker


def _posted_on(days_back):
    """UTC date `days_back` calendar days before today, at midnight (the code
    only reads the date portion of `posted`, so ages are whole-day granular)."""
    d = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=days_back)
    return d.strftime("%Y-%m-%dT00:00:00.000Z")


def insert(con, shortcode, likes, posted, status="tracked"):
    con.execute(
        "INSERT INTO reels(shortcode,url,author,title,likes,likes_raw,posted,lang,desc,first_seen,last_checked,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (shortcode, f"https://www.instagram.com/reel/{shortcode}/", "test_author", "test title",
         likes, str(likes), posted, "en", "test desc", "2026-01-01 00:00", time.strftime("%Y-%m-%d %H:%M"), status))
    con.commit()


class FourteenDayFilterTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._prev_db = tracker.DB
        tracker.DB = os.path.join(self._tmp, "test.db")
        self.con = tracker.db()

    def tearDown(self):
        self.con.close()
        tracker.DB = self._prev_db
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fresh_reel_with_enough_likes_kept(self):
        insert(self.con, "AAA111", 12000, _posted_on(5))
        kept, dropped, new_q = tracker.prune(min_likes=5000, window=14)
        self.assertEqual((kept, dropped), (1, 0))
        self.assertEqual(new_q, ["AAA111"])
        status = self.con.execute("SELECT status FROM reels WHERE shortcode='AAA111'").fetchone()[0]
        self.assertEqual(status, "active")

    def test_old_reel_dropped(self):
        insert(self.con, "BBB222", 12000, _posted_on(20))
        kept, dropped, _ = tracker.prune(min_likes=5000, window=14)
        self.assertEqual((kept, dropped), (0, 1))

    def test_low_likes_dropped_even_if_fresh(self):
        insert(self.con, "CCC333", 900, _posted_on(3))
        kept, dropped, _ = tracker.prune(min_likes=5000, window=14)
        self.assertEqual((kept, dropped), (0, 1))

    def test_missing_posted_date_dropped(self):
        insert(self.con, "DDD444", 12000, None)
        kept, dropped, _ = tracker.prune(min_likes=5000, window=14)
        self.assertEqual((kept, dropped), (0, 1))

    def test_boundary_just_inside_window_kept(self):
        # 13 calendar days back: midnight age is 13.x days, inside the 14-day window
        insert(self.con, "EEE555", 12000, _posted_on(13))
        kept, dropped, _ = tracker.prune(min_likes=5000, window=14)
        self.assertEqual((kept, dropped), (1, 0))

    def test_boundary_just_outside_window_dropped(self):
        # 14 calendar days back: midnight age is 14.x days, outside the window
        # (age is measured from midnight of the posted date, so day-14 drops)
        insert(self.con, "FFF666", 12000, _posted_on(14))
        kept, dropped, _ = tracker.prune(min_likes=5000, window=14)
        self.assertEqual((kept, dropped), (0, 1))

    def test_matches_honors_window(self):
        insert(self.con, "GGG777", 12000, _posted_on(5))
        insert(self.con, "HHH888", 12000, _posted_on(20))
        tracker.prune(min_likes=5000, window=14)
        self.assertEqual(len(tracker.matches(window_days=7, min_likes=10000)), 1)
        self.assertEqual(len(tracker.matches(window_days=3, min_likes=10000)), 0)

    def test_matches_respects_likes_floor(self):
        insert(self.con, "III999", 6000, _posted_on(2))
        insert(self.con, "JJJ000", 30000, _posted_on(2))
        tracker.prune(min_likes=5000, window=14)
        self.assertEqual(len(tracker.matches(window_days=7, min_likes=20000)), 1)


class EnrichmentUrlTests(unittest.TestCase):
    """Reel URL enrichment step: shortcode extraction, type filter, dedupe, cap."""

    def test_extracts_shortcode_from_item_field(self):
        items = [{"type": "Video", "shortCode": "ABC123xyz"}]
        self.assertEqual(tracker.enrichment_urls(items),
                         ["https://www.instagram.com/reel/ABC123xyz/"])

    def test_falls_back_to_url_when_shortcode_missing(self):
        items = [{"type": "Video", "url": "https://www.instagram.com/reel/DEF456qrs/?utm=x"}]
        self.assertEqual(tracker.enrichment_urls(items),
                         ["https://www.instagram.com/reel/DEF456qrs/"])

    def test_skips_non_video_items(self):
        items = [
            {"type": "Image", "shortCode": "SKIP1"},
            {"type": "Video", "shortCode": "KEEP1"},
            {"type": "Sidecar", "shortCode": "SKIP2"},
        ]
        self.assertEqual(tracker.enrichment_urls(items),
                         ["https://www.instagram.com/reel/KEEP1/"])

    def test_dedupes_repeated_shortcodes(self):
        items = [{"type": "Video", "shortCode": sc} for sc in ("DUP1", "DUP2", "DUP1", "DUP3")]
        self.assertEqual(len(tracker.enrichment_urls(items)), 3)

    def test_respects_cap(self):
        items = [{"type": "Video", "shortCode": f"CAP{i:04d}"} for i in range(10)]
        self.assertEqual(len(tracker.enrichment_urls(items, cap=4)), 4)

    def test_non_instagram_urls_skipped(self):
        items = [{"type": "Video", "url": "https://example.com/not-instagram"}]
        self.assertEqual(tracker.enrichment_urls(items), [])


class RetryTests(unittest.TestCase):
    """Transient Apify failures get retried with backoff; fatal ones don't."""

    def setUp(self):
        self._sleep = tracker.time.sleep
        self._slept = []
        tracker.time.sleep = lambda s: self._slept.append(s)

    def tearDown(self):
        tracker.time.sleep = self._sleep

    def test_succeeds_on_third_try(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise tracker.ApifyError("Apify run FAILED: actor crashed")
            return ["ok"]

        self.assertEqual(tracker.with_retries(flaky, attempts=3, base_delay=2), ["ok"])
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self._slept, [2, 4])          # backoff doubles

    def test_gives_up_after_attempts(self):
        def always_fail():
            raise tracker.ApifyError("Apify run TIMED_OUT: no output")

        with self.assertRaises(tracker.ApifyError):
            tracker.with_retries(always_fail, attempts=3, base_delay=1)
        self.assertEqual(self._slept, [1, 2])

    def test_fatal_error_not_retried(self):
        calls = {"n": 0}

        def out_of_credits():
            calls["n"] += 1
            raise tracker.ApifyError("Apify: out of credits (free tier gives $5/mo).")

        with self.assertRaises(tracker.ApifyError):
            tracker.with_retries(out_of_credits, attempts=3, base_delay=1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self._slept, [])

    def test_bad_token_is_fatal(self):
        self.assertTrue(tracker.is_fatal(tracker.ApifyError("Apify API HTTP 401: Invalid token")))
        self.assertTrue(tracker.is_fatal(tracker.ApifyError("Apify API HTTP 403: forbidden")))
        self.assertFalse(tracker.is_fatal(tracker.ApifyError("Apify API unreachable: timed out")))


class ApiTimeoutTests(unittest.TestCase):
    """Timeouts: GETs get one retry, POSTs never do (a retried POST starts a second actor run)."""

    def setUp(self):
        self._sleep = tracker.time.sleep
        tracker.time.sleep = lambda s: None

    def tearDown(self):
        tracker.time.sleep = self._sleep

    def test_get_retries_after_timeout(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise tracker.ApifyError(tracker.timeout_msg("GET", "https://api.apify.com/v2/actor-runs/1", 120))
            return {"data": {"status": "RUNNING"}}

        with mock.patch.object(tracker, "api_call", flaky):
            self.assertEqual(tracker.api("https://api.apify.com/v2/actor-runs/1"), {"data": {"status": "RUNNING"}})
        self.assertEqual(calls["n"], 2)

    def test_post_timeout_raised_immediately(self):
        calls = {"n": 0}

        def dead(*a, **k):
            calls["n"] += 1
            raise tracker.ApifyError(tracker.timeout_msg("POST", "https://api.apify.com/v2/acts/x/runs", 120))

        with mock.patch.object(tracker, "api_call", dead):
            with self.assertRaises(tracker.ApifyError):
                tracker.api("https://api.apify.com/v2/acts/x/runs", method="POST", body={})
        self.assertEqual(calls["n"], 1)

    def test_non_timeout_error_not_retried(self):
        calls = {"n": 0}

        def bad(*a, **k):
            calls["n"] += 1
            raise tracker.ApifyError("Apify API HTTP 500: boom")

        with mock.patch.object(tracker, "api_call", bad):
            with self.assertRaises(tracker.ApifyError):
                tracker.api("https://api.apify.com/v2/actor-runs/1")
        self.assertEqual(calls["n"], 1)

    def test_timeout_message_names_the_endpoint(self):
        msg = tracker.timeout_msg("GET", "https://api.apify.com/v2/datasets/abc123/items?clean=true", 180)
        self.assertIn("180s", msg)
        self.assertIn("GET datasets/abc123/items", msg)


class MergeItemsTests(unittest.TestCase):
    """Stage 1 (profiles) and stage 3 (enrichment) return the same reels — merge to one row each."""

    def test_same_shortcode_across_stages_merged(self):
        stage1 = [{"type": "Video", "shortCode": "SAME1", "likesCount": 12000}]
        stage3 = [{"type": "Video", "shortCode": "SAME1", "likesCount": 12000}]
        self.assertEqual(len(tracker.merge_items(stage1, stage3)), 1)

    def test_keeps_copy_with_more_stats(self):
        thin = {"type": "Video", "shortCode": "BEST1", "likesCount": -1}
        full = {"type": "Video", "shortCode": "BEST1", "likesCount": 8000, "videoViewCount": 300000}
        self.assertEqual(tracker.merge_items([thin], [full])[0]["likesCount"], 8000)
        # order of the stages shouldn't matter
        self.assertEqual(tracker.merge_items([full], [thin])[0]["likesCount"], 8000)

    def test_equal_stats_keeps_first_seen_copy(self):
        a = {"type": "Video", "shortCode": "EQ1", "likesCount": 9000, "videoPlayCount": 100}
        b = {"type": "Video", "shortCode": "EQ1", "likesCount": 9000, "videoPlayCount": 100}
        self.assertIs(tracker.merge_items([a], [b])[0], a)

    def test_url_only_items_merge_too(self):
        a = {"type": "Video", "url": "https://www.instagram.com/reel/URLONLY1/"}
        b = {"type": "Video", "url": "https://www.instagram.com/reel/URLONLY1/?x=1", "likesCount": 7000}
        merged = tracker.merge_items([a], [b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["likesCount"], 7000)

    def test_items_without_shortcode_dropped(self):
        items = [{"type": "Video"}, {"type": "Video", "url": "https://example.com/nope"},
                 {"type": "Video", "shortCode": "KEEP9"}]
        merged = tracker.merge_items(items)
        self.assertEqual([i["shortCode"] for i in merged], ["KEEP9"])

    def test_first_seen_order_preserved(self):
        merged = tracker.merge_items(
            [{"type": "Video", "shortCode": sc} for sc in ("A1", "B2")],
            [{"type": "Video", "shortCode": sc} for sc in ("C3", "A1")])
        self.assertEqual([i["shortCode"] for i in merged], ["A1", "B2", "C3"])

    def test_handles_empty_stage_lists(self):
        one = [{"type": "Video", "shortCode": "ONLY1"}]
        self.assertEqual(len(tracker.merge_items(one, [], None)), 1)


class MockSyncTests(unittest.TestCase):
    """End-to-end mock run against a temp DB: no token, no network, real DB untouched."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._prev_db, self._prev_mock = tracker.DB, tracker.MOCK
        tracker.DB = os.path.join(self._tmp, "sync.db")
        tracker.MOCK = False

    def tearDown(self):
        tracker.DB, tracker.MOCK = self._prev_db, self._prev_mock
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mock_run_reports_unique_reels(self):
        s = tracker.sync(mock=True)
        self.assertNotIn("error", s)
        self.assertGreater(s["found"], 0)
        self.assertGreaterEqual(s["skipped"], 1)               # mock injects an Image item
        self.assertIn("dupes_merged", s["stages"])
        self.assertEqual(s["found"], s["stages"]["mock"])
        con = tracker.db()
        rows = con.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
        self.assertGreaterEqual(rows, s["new"])


if __name__ == "__main__":
    unittest.main()
