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


if __name__ == "__main__":
    unittest.main()
