#!/usr/bin/env python3
"""
IG Trend Tracker v2 — finds fresh AI/tech reels (English/Hindi) and flags the
ones that cross a likes threshold within a time window (likes proxy for 1M+ views).

Discovery is LIVE: every poll run searches multiple engines for new reels, so
the candidate pool grows instead of going stale.

Usage:
  python tracker.py poll [--discover] [--window 7|14] [--likes 10000] [--lang all|en|hi]
  python tracker.py list [--window 7] [--likes 10000] [--lang all|en|hi]
  python tracker.py add https://www.instagram.com/reel/SHORTCODE/
  python tracker.py seeds        # discovery only
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.parse, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "state.db")
ACCOUNTS = os.path.join(BASE, "accounts.txt")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# Pool retention: only reels that can actually qualify are tracked.
MAX_AGE_DAYS = 14      # posted within this window
MIN_LIKES = 5000       # minimum likes to keep a reel in the pool

KEYWORDS = [
    "artificial intelligence", "AI tools", "ChatGPT", "machine learning",
    "AI video", "tech news", "AI trends", "AI tools Hindi", "technology",
    "AI 2026", "AI robot", "AI news", "tech gadgets", "AI viral",
    "AI startup", "coding", "AI explained",
]
REEL_RE = re.compile(r"instagram\.com/(?:reel|p)/([A-Za-z0-9_-]{8,})")
LIKES_RE = re.compile(r"([\d,.]+[KkMm]?)\s+likes")
HINDI_RE = re.compile(r"[\u0900-\u097F]")

ENGINES = [
    ("ddg", "https://html.duckduckgo.com/html/?q={q}"),
    ("bing", "https://www.bing.com/search?q={q}"),
    ("brave", "https://search.brave.com/search?q={q}"),
]


def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS reels(
        shortcode TEXT PRIMARY KEY, url TEXT, author TEXT, title TEXT,
        likes INTEGER, likes_raw TEXT, posted TEXT, lang TEXT, desc TEXT,
        first_seen TEXT, last_checked TEXT, status TEXT DEFAULT 'tracked')""")
    # migration: add desc if missing
    cols = [r[1] for r in con.execute("PRAGMA table_info(reels)")]
    if "desc" not in cols:
        con.execute("ALTER TABLE reels ADD COLUMN desc TEXT")
    con.commit()
    return con


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def parse_likes(desc):
    if not desc:
        return None
    m = LIKES_RE.search(desc)
    if not m:
        return None
    s = m.group(1).replace(",", "")
    mult = 1
    if s[-1] in "Kk":
        mult, s = 1000, s[:-1]
    elif s[-1] in "Mm":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def load_accounts():
    try:
        with open(ACCOUNTS) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def build_queries():
    qs = []
    for kw in KEYWORDS:
        qs.append(f"site:instagram.com/reel {kw}")
    for acc in load_accounts():
        qs.append(f"instagram.com {acc} reel")
    qs.append("site:instagram.com/reel AI Hindi")
    qs.append("site:instagram.com/reel AI India")
    return qs


def ddg_fetch(q, page=0):
    """Fetch DDG HTML search, retrying on rate-limit (202) with backoff."""
    url = "https://html.duckduckgo.com/html/?q={q}&s={s}".format(q=urllib.parse.quote(q), s=page * 10)
    for attempt in range(3):
        try:
            html = get(url, timeout=20)
            if "anomaly" in html.lower() or len(html) < 5000:
                raise Exception("rate-limited")
            return html
        except Exception:
            time.sleep(10 + 5 * attempt)
    return None


def fetch_engine(engine, q, page=0):
    """Fetch search HTML for a named engine, retrying on rate-limit with backoff.
    Returns HTML string or None if the engine is rate-limited/unreachable."""
    if engine == "ddg":
        url = "https://html.duckduckgo.com/html/?q={q}&s={s}".format(q=urllib.parse.quote(q), s=page * 10)
    elif engine == "brave":
        url = "https://search.brave.com/search?q={q}".format(q=urllib.parse.quote(q))
    elif engine == "bing":
        url = "https://www.bing.com/search?q={q}".format(q=urllib.parse.quote(q))
    else:
        return None
    for attempt in range(2):
        try:
            html = get(url, timeout=15)
            if "anomaly" in html.lower() or len(html) < 5000:
                raise Exception("rate-limited")
            return html
        except Exception:
            time.sleep(5 + 3 * attempt)
    return None


def discover(max_total=80, max_queries=20):
    """Search for fresh reel shortcodes, rotating engines on rate-limit.
    Returns {shortcode: url}."""
    found = {}
    queries = build_queries()
    engines = ["ddg", "brave", "bing"]
    consecutive_failures = 0
    for qi, q in enumerate(queries[:max_queries]):
        got_hits = False
        for eng in engines:
            html = fetch_engine(eng, q)
            if html is None:
                print(f"  [{eng}] {q!r} -> rate-limited, skipping")
                continue
            hits = list(dict.fromkeys(REEL_RE.findall(html)))
            # try a second page for more results
            if len(hits) == 10:
                html2 = fetch_engine(eng, q, page=1)
                if html2:
                    hits += list(dict.fromkeys(REEL_RE.findall(html2)))
            if hits:
                for sc in hits:
                    found[sc] = f"https://www.instagram.com/reel/{sc}/"
                print(f"  [{eng}] {q!r} -> {len(hits)} reel(s) (total {len(found)})")
                got_hits = True
                break
        if got_hits:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            print(f"  all engines failed for {q!r} ({consecutive_failures} in a row)")
            # if every engine is down, stop burning requests — they need cooldown
            if consecutive_failures >= 3:
                print("  ⚠ every engine rate-limited 3x in a row — stopping discovery early")
                break
        time.sleep(8)
        if len(found) >= max_total:
            break
    return found


def add_reel(url):
    m = REEL_RE.search(url)
    if not m:
        return None
    con = db()
    con.execute("INSERT OR IGNORE INTO reels(shortcode, url, status, first_seen) VALUES(?,?,?,?)",
                (m.group(1), f"https://www.instagram.com/reel/{m.group(1)}/", "tracked", time.strftime("%Y-%m-%d %H:%M")))
    con.commit()
    return m.group(1)


def poll_reel(sc, url):
    """Fetch reel stats via Microlink; returns dict or None.
    Returns the string 'rate-limited' when Microlink is throttled (429)."""
    try:
        j = json.loads(get("https://api.microlink.io/?url=" + urllib.parse.quote(url)))
        d = j.get("data", {}) or {}
        desc = d.get("description", "") or ""
        return {
            "author": (d.get("author") or "").strip(),
            "title": (d.get("title") or "").strip(),
            "posted": d.get("date"),
            "lang": (d.get("lang") or "").split("-")[0].lower() or None,
            "likes": parse_likes(desc),
            "likes_raw": re.search(LIKES_RE, desc).group(1) if LIKES_RE.search(desc) else None,
            "desc": desc[:300],
        }
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "rate-limited"
        return None
    except Exception:
        return None


def poll_all(force=False, max_reels=15):
    """Poll reel stats via Microlink, budget-aware (free tier ≈50 req/day).
    Stops early on 429 so the whole budget isn't burned in one run."""
    con = db()
    # priority: never-polled first, then most recently discovered
    rows = con.execute(
        "SELECT shortcode, url, last_checked FROM reels "
        "ORDER BY (last_checked IS NULL) DESC, first_seen DESC").fetchall()
    checked, failed = 0, 0
    for sc, url, last in rows[:max_reels]:
        if not force and last and (time.time() - float(last)) < 6 * 3600:
            continue
        r = poll_reel(sc, url)
        if r == "rate-limited":
            print(f"  [microlink] 429 on {sc} — stopping, budget needs cooldown")
            break
        if r:
            con.execute("""UPDATE reels SET author=?, title=?, likes=?, likes_raw=?, posted=?, lang=?, desc=?, last_checked=?
                           WHERE shortcode=?""",
                        (r["author"], r["title"], r["likes"], r["likes_raw"], r["posted"], r["lang"], r["desc"],
                         str(time.time()), sc))
            checked += 1
        else:
            failed += 1
        time.sleep(2)
    con.commit()
    return checked, failed


def matches(window_days=7, min_likes=10000, lang="all"):
    """Qualifying pool (status='active') filtered by the current UI filters."""
    con = db()
    rows = con.execute("SELECT shortcode,url,author,title,likes,likes_raw,posted,lang,desc FROM reels WHERE status='active'").fetchall()
    now = time.time()
    out = []
    for sc, url, author, title, likes, likes_raw, posted, lang_, desc in rows:
        if likes < min_likes:
            continue
        if posted:
            try:
                age_days = (now - time.mktime(time.strptime(posted[:10], "%Y-%m-%d"))) / 86400
                if age_days > window_days:
                    continue
            except Exception:
                pass
        text = (title or "") + " " + (desc or "")
        is_hi = bool(HINDI_RE.search(text)) or lang_ == "hi"
        if lang == "en" and (lang_ not in ("en", None) or is_hi):
            continue
        if lang == "hi" and not is_hi:
            continue
        out.append({"shortcode": sc, "url": url, "author": author or "?", "title": (title or "")[:80],
                    "likes": likes, "likes_raw": likes_raw or str(likes),
                    "posted": (posted or "?")[:10], "lang": "hi" if is_hi else (lang_ or "?")})
    out.sort(key=lambda r: r["likes"], reverse=True)
    return out


def cmd_seeds(args):
    found = discover(max_total=args.max, max_queries=args.queries)
    con = db()
    for sc, url in found.items():
        con.execute("INSERT OR IGNORE INTO reels(shortcode, url, status, first_seen) VALUES(?,?,?,?)",
                    (sc, url, "tracked", time.strftime("%Y-%m-%d %H:%M")))
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
    print(f"\nseeded {len(found)} new reels (total tracked: {total})")


def prune(min_likes=MIN_LIKES, window=MAX_AGE_DAYS):
    """Keep only reels that can qualify: posted within window AND likes >= min_likes.
    Drops anything older than the window (can never qualify again) or below the like bar.
    Returns (kept, dropped, new_qualifiers)."""
    con = db()
    rows = con.execute("SELECT shortcode, likes, posted, last_checked, status, first_seen FROM reels").fetchall()
    now = time.time()
    kept = dropped = 0
    new_qualifiers = []
    for sc, likes, posted, last_checked, status, first_seen in rows:
        if likes is not None and posted:
            try:
                age = (now - time.mktime(time.strptime(posted[:10], "%Y-%m-%d"))) / 86400
            except Exception:
                age = None
        else:
            age = None
        qualifies = (likes is not None and likes >= min_likes and age is not None and age <= window)
        if qualifies:
            if status != "active":
                new_qualifiers.append(sc)
                con.execute("UPDATE reels SET status='active' WHERE shortcode=?", (sc,))
            kept += 1
        else:
            con.execute("UPDATE reels SET status='dropped' WHERE shortcode=?", (sc,))
            dropped += 1
    con.commit()
    return kept, dropped, new_qualifiers


def cmd_poll(args):
    if args.discover:
        print("discovering new reels...")
        found = discover(max_total=args.max, max_queries=args.queries)
        con = db()
        for sc, url in found.items():
            con.execute("INSERT OR IGNORE INTO reels(shortcode, url, status, first_seen) VALUES(?,?,?,?)",
                        (sc, url, "tracked", time.strftime("%Y-%m-%d %H:%M")))
        con.commit()
        print(f"found {len(found)} new candidates")
    n = add_from_args(args)
    checked, failed = poll_all()
    kept, dropped, new_q = prune(min_likes=args.min)
    ms = matches(args.window, args.likes, args.lang)
    con = db()
    active = con.execute("SELECT COUNT(*) FROM reels WHERE status='active'").fetchone()[0]
    print(f"\nqualifying reels in pool: {active} (kept {kept}, dropped {dropped}) | newly qualifying: {len(new_q)}")
    for sc in new_q:
        r = con.execute("SELECT author,likes_raw,posted,url FROM reels WHERE shortcode=?", (sc,)).fetchone()
        if r:
            print(f"  NEW: {r[1]} likes | {r[2][:10]} | {r[0]} | {r[3]}")
    print(f"matches ({args.window}d, {args.likes}+ likes, lang={args.lang}): {len(ms)}")
    for r in ms:
        print(f"  {r['likes_raw']:>10} likes | {r['posted']} | {r['lang']} | {r['author']} | {r['url']}")


def cmd_list(args):
    ms = matches(args.window, args.likes, args.lang)
    print(f"matches ({args.window}d, {args.likes}+ likes, lang={args.lang}): {len(ms)}")
    for r in ms:
        print(f"  {r['likes_raw']:>10} | {r['posted']} | {r['lang']} | {r['author']} | {r['url']}")


def add_from_args(args):
    n = 0
    if getattr(args, "links", None):
        for l in args.links:
            if add_reel(l):
                n += 1
    return n


def cmd_add(args):
    for l in args.links:
        sc = add_reel(l)
        print(f"added: {sc} <- {l}" if sc else f"could not parse: {l}")


def main():
    ap = argparse.ArgumentParser(description="IG Trend Tracker v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("poll", "list"):
        p = sub.add_parser(name)
        p.add_argument("--window", type=int, default=7)
        p.add_argument("--likes", type=int, default=10000)
        p.add_argument("--lang", default="all", choices=["all", "en", "hi"])
        p.add_argument("--discover", action="store_true")
        p.add_argument("--max", type=int, default=60)
        p.add_argument("--queries", type=int, default=18)
        p.add_argument("--min", type=int, default=MIN_LIKES, help="pool retention min likes")
        p.add_argument("links", nargs="*")
    p = sub.add_parser("add")
    p.add_argument("links", nargs="+")
    p = sub.add_parser("seeds")
    p.add_argument("--max", type=int, default=60)
    p.add_argument("--queries", type=int, default=18)
    args = ap.parse_args()
    if args.cmd == "poll":
        cmd_poll(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "seeds":
        cmd_seeds(args)


if __name__ == "__main__":
    main()
