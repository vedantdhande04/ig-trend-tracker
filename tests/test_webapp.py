"""Dashboard ordering tests: the hits table sorts by views by default, other
orders are opt-in via ?sort=.

The dashboard tests render the real Flask app against a throwaway temp DB —
state.db / state_mock.db are never touched.
Run:  python -m unittest discover -s tests
"""
import os
import shutil
import tempfile
import time
import unittest

import tracker

try:
    import webapp
except ImportError:          # flask missing: tracker tests still run
    webapp = None


def row(sc, likes, views, author="creator", posted="2026-09-05T00:00:00.000Z"):
    return {"shortcode": sc, "url": f"https://www.instagram.com/reel/{sc}/", "author": author,
            "title": sc, "likes": likes, "likes_raw": str(likes), "views": views,
            "views_raw": tracker.format_likes(views), "posted": posted[:10], "lang": "en"}


@unittest.skipIf(webapp is None, "flask not installed")
class SortHitsTests(unittest.TestCase):

    def setUp(self):
        self.rows = [row("LOWVIEWS", 40000, 100000), row("HIGHVIEWS", 12000, 900000),
                     row("MOSTLIKES", 90000, None)]

    def code_order(self, sort="views"):
        return [r["shortcode"] for r in webapp.sort_hits(self.rows, sort)]

    def test_default_sort_is_views_desc(self):
        self.assertEqual(self.code_order(), ["HIGHVIEWS", "LOWVIEWS", "MOSTLIKES"])

    def test_likes_sort(self):
        self.assertEqual(self.code_order("likes"), ["MOSTLIKES", "LOWVIEWS", "HIGHVIEWS"])

    def test_newest_sort(self):
        rows = [row("OLD1", 10000, 1000, posted="2026-08-30T00:00:00.000Z"),
                row("NEW1", 10000, 1000, posted="2026-09-08T00:00:00.000Z")]
        self.assertEqual([r["shortcode"] for r in webapp.sort_hits(rows, "posted")], ["NEW1", "OLD1"])

    def test_creator_sort_is_alphabetical(self):
        rows = [row("B1", 10000, 1000, author="zeta"), row("A1", 10000, 1000, author="alpha")]
        self.assertEqual([r["shortcode"] for r in webapp.sort_hits(rows, "creator")], ["A1", "B1"])

    def test_unknown_sort_falls_back_to_views(self):
        self.assertEqual(self.code_order("nonsense"), ["HIGHVIEWS", "LOWVIEWS", "MOSTLIKES"])

    def test_missing_views_sort_last_not_first(self):
        self.assertEqual(self.code_order()[-1], "MOSTLIKES")


@unittest.skipIf(webapp is None, "flask not installed")
class DashboardSortTests(unittest.TestCase):
    """Rendered page: view-count leader first by default, ?sort= switches the order."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._prev_db = tracker.DB
        tracker.DB = os.path.join(self._tmp, "web.db")
        con = tracker.db()
        now = time.strftime("%Y-%m-%d %H:%M")
        for sc, likes, views in (("LOWV", 30000, 150000), ("HIGHV", 20000, 800000)):
            con.execute(
                "INSERT INTO reels(shortcode,url,author,title,likes,likes_raw,views,posted,lang,desc,"
                "first_seen,last_checked,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active')",
                (sc, f"https://www.instagram.com/reel/{sc}/", "tester", sc, likes, str(likes), views,
                 "2026-09-05T00:00:00.000Z", "en", "desc", now, now))
        con.commit()
        con.close()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def tearDown(self):
        tracker.DB = self._prev_db
        shutil.rmtree(self._tmp, ignore_errors=True)

    def hits_table(self, query):
        html = self.client.get(query).get_data(as_text=True)
        return html.split("🔥 Hits:")[1].split("➕ Add a reel")[0]

    def test_view_leader_first_by_default(self):
        table = self.hits_table("/?window=14&likes=5000")
        self.assertLess(table.index("reel/HIGHV/"), table.index("reel/LOWV/"))

    def test_sort_likes_puts_most_liked_first(self):
        table = self.hits_table("/?window=14&likes=5000&sort=likes")
        self.assertLess(table.index("reel/LOWV/"), table.index("reel/HIGHV/"))

    def test_sort_control_is_rendered_with_views_selected(self):
        html = self.client.get("/?window=14&likes=5000").get_data(as_text=True)
        self.assertIn('name="sort"', html)
        self.assertIn('<option value="views" selected>', html)

    def test_bad_sort_value_still_renders(self):
        resp = self.client.get("/?window=14&likes=5000&sort=%%%")
        self.assertEqual(resp.status_code, 200)


@unittest.skipIf(webapp is None, "flask not installed")
class ReloadAccountsTests(unittest.TestCase):
    """The dashboard re-reads accounts.txt on demand — no app restart needed."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._prev_db, self._prev_accounts = tracker.DB, tracker.ACCOUNTS
        tracker.DB = os.path.join(self._tmp, "web.db")
        tracker.ACCOUNTS = os.path.join(self._tmp, "accounts.txt")
        tracker.db().close()                      # create the schema in the temp DB
        self._write("# mine\nnewcreator\n")
        tracker._accounts_cache.update(mtime=None, names=[])
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def tearDown(self):
        tracker.DB, tracker.ACCOUNTS = self._prev_db, self._prev_accounts
        tracker._accounts_cache.update(mtime=None, names=[])
        webapp._reload["msg"] = ""
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, text):
        with open(tracker.ACCOUNTS, "w") as f:
            f.write(text)
        stamp = time.time() + 10
        os.utime(tracker.ACCOUNTS, (stamp, stamp))

    def test_reload_button_rendered_with_count(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('name="action" value="reload_accounts"', html)
        self.assertIn("1 creators in accounts.txt", html)

    def test_post_reload_reports_the_count(self):
        html = self.client.post("/", data={"action": "reload_accounts"}).get_data(as_text=True)
        self.assertIn("reloaded accounts.txt", html)
        self.assertIn("1 creators in accounts.txt", html)

    def test_file_edit_shows_up_on_next_page_load(self):
        self._write("# mine\nnewcreator\nsecondcreator\n")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("2 creators in accounts.txt", html)

    def test_reload_picks_up_a_new_line_added_after_startup(self):
        with open(tracker.ACCOUNTS, "a") as f:
            f.write("added_later\n")
        stamp = time.time() + 20
        os.utime(tracker.ACCOUNTS, (stamp, stamp))
        html = self.client.post("/", data={"action": "reload_accounts"}).get_data(as_text=True)
        self.assertIn("2 creators in accounts.txt", html)


if __name__ == "__main__":
    unittest.main()
