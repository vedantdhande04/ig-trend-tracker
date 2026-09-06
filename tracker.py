#!/usr/bin/env python3
"""
IG Trend Tracker v3 — Apify-backed.

Finds fresh AI/tech Instagram reels (English/Hindi) that cross a likes threshold
within a time window, using the official Apify "Instagram Scraper" actor
(apify/instagram-scraper). ONE actor run replaces BOTH broken v2 layers:
  - discovery  (was: DuckDuckGo/Brave/Bing HTML search — constantly rate-limited)
  - stats      (was: Microlink free tier ~50 req/day — 429s every run)

Every reel arrives with real likesCount, videoViewCount, commentsCount,
timestamp, caption and ownerUsername in a single shot.

Setup (one time):
  1. Create a free Apify account at https://apify.com  ($5 free credits/month, no card)
  2. Settings -> Integrations -> API token -> copy it
  3. Save it:  setx APIFY_TOKEN "apify_xxx"   (or put APIFY_TOKEN=... in a .env file here)

Usage:
  python tracker.py poll [--mock] [--likes 10000] [--window 7|14] [--lang all|en|hi] [--limit 25] [--since-days 14] [--csv out.csv] [--json]
  python tracker.py sync [--mock]        # scrape + upsert + prune (used by webapp)
  python tracker.py seeds [--mock]       # scrape + upsert only, no prune
  python tracker.py list [--window 7] [--likes 10000] [--lang all|en|hi] [--csv out.csv] [--json]
  python tracker.py add https://www.instagram.com/reel/SHORTCODE/

Cost: the actor bills ~$0.0023 per result on the free plan ($2.30/1k).
A default run (~325 reels) is ~$0.75; weekly cadence ~$3/month — inside the $5 free tier.
Use --mock to test the whole pipeline without an account or credits.
"""
import argparse, datetime, json, os, re, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "state.db")
ACCOUNTS = os.path.join(BASE, "accounts.txt")
MOCK = False  # flipped by --mock: all reads/writes go to state_mock.db so fake data can never pollute the real DB

# Pool retention: only reels that can actually qualify are tracked.
MAX_AGE_DAYS = 14      # posted within this window
MIN_LIKES = 5000       # minimum likes to keep a reel in the pool

# Apify
ACTOR_ID = "shu8hvrXbJbY3Eb9W"   # apify/instagram-scraper (REST API needs the ID, not the store name)
HT_ACTOR_ID = "reGe1ST3OBgYZSsZJ"  # apify/instagram-hashtag-scraper — hashtag feed discovery (grid has likes=0, so we enrich)
PRICE_PER_ITEM = 0.0027        # free-plan pay-per-result rate ($2.70/1k)
HT_PRICE_PER_ITEM = 0.0026     # hashtag-scraper free-plan rate ($2.60/1k — store price $1.90 is Starter+)
RESULTS_LIMIT = 25             # per URL (profile / reel)
HASHTAG_LIMIT = 12             # reels per hashtag in the discovery feed
MAX_ENRICH = 100               # cap on discovered reels sent for enrichment
PROFILES_PER_RUN = 5           # rotate through accounts.txt so the whole list stays fresh
ORPHANS_PER_RUN = 10           # user-added reels (added via CLI/webapp) get scraped too
POLL_SECONDS = 5
RUN_TIMEOUT = 900              # give the actor up to 15 min

# Trend discovery: hashtag pages surface reels from unknown creators (the real
# "trend" signal); accounts.txt covers the curated creators.
HASHTAGS = [
    "ai", "aitools", "artificialintelligence", "machinelearning",
    "chatgpt", "tech", "aihindi", "aitoolsindia",
]

REEL_RE = re.compile(r"instagram\.com/(?:reel|p)/([A-Za-z0-9_-]{8,})")
HINDI_RE = re.compile(r"[\u0900-\u097F]")


# ---------------------------------------------------------------- db

def db():
    path = os.path.join(BASE, "state_mock.db") if MOCK else DB
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS reels(
        shortcode TEXT PRIMARY KEY, url TEXT, author TEXT, title TEXT,
        likes INTEGER, likes_raw TEXT, posted TEXT, lang TEXT, desc TEXT,
        first_seen TEXT, last_checked TEXT, status TEXT DEFAULT 'tracked')""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(reels)")]
    if "desc" not in cols:
        con.execute("ALTER TABLE reels ADD COLUMN desc TEXT")
    if "views" not in cols:
        con.execute("ALTER TABLE reels ADD COLUMN views INTEGER")
    con.commit()
    return con


# ---------------------------------------------------------------- apify api

class ApifyError(Exception):
    pass


def env_or_dotenv(key):
    """Read a value from the environment or the project .env file."""
    val = os.environ.get(key)
    if val:
        return val.strip()
    try:
        with open(os.path.join(BASE, ".env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


def load_token():
    return env_or_dotenv("APIFY_TOKEN")


def check_token(token):
    """Fail fast at startup on a missing/bad APIFY_TOKEN with a clear message.

    Returns an error string to show, or None when the token looks fine (or the
    check itself couldn't run — network blips shouldn't block the scrape).
    """
    if not token:
        return ("No APIFY_TOKEN found. Create a free Apify account at apify.com, copy the API token, "
                "then run: setx APIFY_TOKEN \"apify_xxx\" (or put APIFY_TOKEN=... in a .env file here). "
                "Use --mock to test the pipeline without a token.")
    try:
        api("https://api.apify.com/v2/users/me", token=token, timeout=30)
    except ApifyError as e:
        if "out of credits" in str(e):
            return str(e)
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            return (f"APIFY_TOKEN was rejected by Apify ({e}). "
                    "Grab a fresh one at apify.com -> Settings -> Integrations -> API token.")
        return None  # unreachable/timeout etc.: let the run surface real errors
    return None


def api(url, method="GET", body=None, token=None, timeout=120):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8", "ignore"))
            msg = (err.get("error") or {}).get("message") or err.get("message") or str(err)
        except Exception:
            msg = e.reason
        if e.code == 402:
            raise ApifyError("Apify: out of credits (free tier gives $5/mo). Add funds or wait for the reset. " + msg)
        raise ApifyError(f"Apify API HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise ApifyError(f"Apify API unreachable: {e.reason}")


def run_actor(input_, token, actor_id=ACTOR_ID):
    """Start an actor, wait for it, return the cleaned dataset items."""
    # NOTE: the /runs endpoint takes the RAW actor input as the body — wrapping it
    # in {"input": ...} makes the actor see zero URLs and return no_items.
    r = api(f"https://api.apify.com/v2/acts/{actor_id}/runs",
            method="POST", body=input_, token=token)
    run = r.get("data", {})
    run_id = run.get("id")
    if not run_id:
        raise ApifyError("Apify: run start returned no run id")
    dataset_id = run.get("defaultDatasetId")
    deadline = time.time() + RUN_TIMEOUT
    while True:
        time.sleep(POLL_SECONDS)
        run = api(f"https://api.apify.com/v2/actor-runs/{run_id}", token=token).get("data", {})
        status = run.get("status")
        if status == "SUCCEEDED":
            dataset_id = dataset_id or run.get("defaultDatasetId")
            break
        if status in ("FAILED", "ABORTED", "TIMED_OUT"):
            raise ApifyError(f"Apify run {status}: {run.get('errorMessage') or run.get('statusMessage') or 'no message'}")
        if time.time() > deadline:
            try:
                api(f"https://api.apify.com/v2/actor-runs/{run_id}/abort", method="POST", token=token)
            except Exception:
                pass
            raise ApifyError("Apify run timed out locally (run aborted)")
    items = api(f"https://api.apify.com/v2/datasets/{dataset_id}/items?clean=true", token=token, timeout=180)
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------- input building

def load_accounts():
    try:
        with open(ACCOUNTS) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def rotated_profiles(n=PROFILES_PER_RUN):
    """Deterministic rotation by ISO week so every profile gets scraped over time."""
    accs = load_accounts()
    if not accs:
        return []
    week = datetime.date.today().isocalendar()[1]
    start = (week * n) % len(accs)
    return (accs * 2)[start:start + n]


def orphan_reels(n=ORPHANS_PER_RUN):
    """User-added reels that never got stats — include their URLs so they get scraped."""
    con = db()
    rows = con.execute("SELECT shortcode FROM reels WHERE last_checked IS NULL LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows]


def build_profile_input(window_days=MAX_AGE_DAYS, limit=RESULTS_LIMIT):
    """Stage-1 input for the main actor: creator profiles + user-added orphan reels.
    (Hashtag URLs don't work here — they return hashtag metadata, not reels.)"""
    urls = [f"https://www.instagram.com/{acc}/" for acc in rotated_profiles()]
    urls += [f"https://www.instagram.com/reel/{sc}/" for sc in orphan_reels()]
    return {
        "directUrls": urls,
        "resultsType": "reels",
        "resultsLimit": limit,
        "onlyPostsNewerThan": f"{window_days} days",
    }


# ---------------------------------------------------------------- upsert

def format_likes(n):
    if n is None:
        return None
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)


def shortcode_from_url(url):
    m = REEL_RE.search(url or "")
    return m.group(1) if m else None


def upsert_items(items):
    con = db()
    now = time.strftime("%Y-%m-%d %H:%M")
    n_new = n_upd = n_skip = 0
    for it in items:
        if it.get("type") and it.get("type") != "Video":
            n_skip += 1          # reels arrive as type Video; drop anything else
            continue
        sc = it.get("shortCode") or shortcode_from_url(it.get("url"))
        if not sc:
            n_skip += 1
            continue
        likes = it.get("likesCount")
        if likes is None or likes < 0:
            likes = None         # -1 = creator hid the like count
        views = it.get("videoViewCount")
        if views is None:
            views = it.get("videoPlayCount")   # reels mode returns videoPlayCount, not videoViewCount
        if views is not None and views < 0:
            views = None
        caption = (it.get("caption") or "").strip()
        lang = "hi" if HINDI_RE.search(caption) else ("en" if caption else None)
        url = (it.get("url") or f"https://www.instagram.com/reel/{sc}/").strip()
        posted = it.get("timestamp") or None
        author = (it.get("ownerUsername") or "").strip()
        ts = str(time.time())
        if con.execute("SELECT 1 FROM reels WHERE shortcode=?", (sc,)).fetchone():
            con.execute("""UPDATE reels SET url=?, author=?, title=?, likes=?, likes_raw=?, views=?,
                           posted=?, lang=?, desc=?, last_checked=? WHERE shortcode=?""",
                        (url, author, caption[:200], likes, format_likes(likes), views,
                         posted, lang, caption[:300], ts, sc))
            n_upd += 1
        else:
            con.execute("""INSERT INTO reels(shortcode, url, author, title, likes, likes_raw, views,
                           posted, lang, desc, first_seen, last_checked, status)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'tracked')""",
                        (sc, url, author, caption[:200], likes, format_likes(likes), views,
                         posted, lang, caption[:300], now, ts))
            n_new += 1
    con.commit()
    return n_new, n_upd, n_skip


# ---------------------------------------------------------------- pipeline

def enrichment_urls(ht_items, cap=MAX_ENRICH):
    """Reel URLs to send for enrichment from a hashtag-feed item list.

    Keeps Video items only, pulls the shortcode (item field or URL), dedupes
    (same reel shows up in several hashtags) and caps the batch size.
    """
    reel_urls = []
    for it in ht_items:
        if it.get("type") and it.get("type") != "Video":
            continue
        sc = it.get("shortCode") or shortcode_from_url(it.get("url"))
        if sc:
            reel_urls.append(f"https://www.instagram.com/reel/{sc}/")
    return list(dict.fromkeys(reel_urls))[:cap]


def sync(mock=False, window_days=MAX_AGE_DAYS, do_prune=True, limit=RESULTS_LIMIT):
    """Three-stage pipeline. Returns a summary dict (webapp-friendly: {'error': ...} on failure).

    Stage 1 — creator profiles:      main actor, reels mode, profile URLs -> full stats directly.
    Stage 2 — hashtag discovery:     hashtag-scraper, feed grid -> real shortcodes (grid likes are 0).
    Stage 3 — enrichment:            main actor, reels mode, discovered reel URLs -> full stats.
    """
    token = None if mock else load_token()
    if not mock and not token:
        return {"error": "No APIFY_TOKEN found. Create a free Apify account at apify.com, copy the API token, "
                         "then run: setx APIFY_TOKEN \"apify_xxx\" (or put APIFY_TOKEN=... in a .env file here). "
                         "Use --mock to test the pipeline without a token."}
    t0 = time.time()
    all_items = []
    stages = {}
    cost = 0.0
    if mock:
        all_items = mock_items()
        stages["mock"] = len(all_items)
    else:
        items1 = run_actor(build_profile_input(window_days, limit), token)
        stages["profiles"] = len(items1)
        cost += len(items1) * PRICE_PER_ITEM
        all_items += items1

        try:
            ht_items = run_actor({"hashtags": HASHTAGS, "resultsType": "reels", "resultsLimit": HASHTAG_LIMIT},
                                 token, actor_id=HT_ACTOR_ID)
        except ApifyError:
            ht_items = []
        stages["hashtag_feed"] = len(ht_items)
        cost += len(ht_items) * HT_PRICE_PER_ITEM

        reel_urls = enrichment_urls(ht_items)
        if reel_urls:
            items3 = run_actor({"directUrls": reel_urls, "resultsType": "reels", "resultsLimit": 1}, token)
            stages["enriched"] = len(items3)
            cost += len(items3) * PRICE_PER_ITEM
            all_items += items3
        else:
            stages["enriched"] = 0

    n_new, n_upd, n_skip = upsert_items(all_items)
    kept = dropped = 0
    new_q = []
    if do_prune:
        kept, dropped, new_q = prune(min_likes=MIN_LIKES)
    stages.setdefault("enriched", 0)
    return {
        "found": len(all_items), "new": n_new, "updated": n_upd, "skipped": n_skip,
        "kept": kept, "dropped": dropped, "new_qualifiers": new_q,
        "stages": stages,
        "cost_usd": round(cost, 2),
        "seconds": round(time.time() - t0, 1),
    }


def prune(min_likes=MIN_LIKES, window=MAX_AGE_DAYS):
    """Keep only reels that can qualify: posted within window AND likes >= min_likes.
    Returns (kept, dropped, new_qualifiers)."""
    con = db()
    rows = con.execute("SELECT shortcode, likes, posted, status FROM reels").fetchall()
    now = time.time()
    kept = dropped = 0
    new_qualifiers = []
    for sc, likes, posted, status in rows:
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


def matches(window_days=7, min_likes=10000, lang="all"):
    """Qualifying pool (status='active') filtered by the current UI filters.
    Sorted by views (real reach) when available, else likes."""
    con = db()
    rows = con.execute("SELECT shortcode,url,author,title,likes,likes_raw,posted,lang,desc,views FROM reels "
                       "WHERE status='active'").fetchall()
    now = time.time()
    out = []
    for sc, url, author, title, likes, likes_raw, posted, lang_, desc, views in rows:
        if likes is None or likes < min_likes:
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
                    "views": views, "views_raw": format_likes(views) if views else None,
                    "posted": (posted or "?")[:10], "lang": "hi" if is_hi else (lang_ or "?")})
    out.sort(key=lambda r: (r["views"] or 0, r["likes"]), reverse=True)
    return out


def add_reel(url):
    m = REEL_RE.search(url)
    if not m:
        return None
    con = db()
    con.execute("INSERT OR IGNORE INTO reels(shortcode, url, status, first_seen) VALUES(?,?,?,?)",
                (m.group(1), f"https://www.instagram.com/reel/{m.group(1)}/", "tracked",
                 time.strftime("%Y-%m-%d %H:%M")))
    con.commit()
    return m.group(1)


# ---------------------------------------------------------------- mock (no token needed)

def mock_items(n=35):
    import random
    random.seed(42)
    authors = [a for a in load_accounts()] + ["ai.mastery", "tech_viral_hub", "futureai"]
    caps = [
        "This AI tool just changed everything 🔥 #ai #aitools",
        "ChatGPT vs Gemini — which one wins? 🤖 #ai #tech",
        "New AI video generator is INSANE #ai #aitools",
        "ये AI टूल 2026 में हर किसी को चाहिए 🔥 #aihindi #aitoolsindia",
        "Machine learning explained in 60 seconds #ml #ai",
        "This robot will replace developers? #ai #coding",
        "AI news today: everything you missed #technews",
        "मशीन लर्निंग क्या है? आसान भाषा में #aihindi",
        "5 AI tools that will save you hours #aitools #productivity",
        "The future of AI is here #artificialintelligence",
    ]
    out = []
    now = time.time()
    for i in range(n):
        likes = random.choice([-1, None, 500, 1200, 4900, 5200, 9800, 12500, 38000, 75000, 90000, 210000])
        age = random.uniform(-2, 18)  # days; some older than the 14-day window on purpose
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - age * 86400))
        sc = "M" + "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_", k=10))
        views = None if likes in (None, -1) else int((likes or 1000) * random.uniform(8, 90))
        out.append({
            "id": str(1000000 + i), "type": "Video", "shortCode": sc,
            "url": f"https://www.instagram.com/reel/{sc}/",
            "caption": random.choice(caps), "likesCount": likes,
            "commentsCount": random.randint(0, 4000), "videoViewCount": views,
            "timestamp": ts, "ownerUsername": random.choice(authors),
            "hashtags": ["ai"], "videoDuration": round(random.uniform(8, 90), 1),
        })
    out[3]["type"] = "Image"  # must be filtered out by upsert
    return out


# ---------------------------------------------------------------- cli

def write_csv(rows, path):
    """Write the qualifying pool to a CSV file (utf-8-sig so Excel keeps Hindi captions)."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["shortcode", "url", "author", "title", "likes",
                                          "likes_raw", "views", "views_raw", "posted", "lang"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in w.fieldnames})
    return len(rows)


def cmd_sync(args):
    s = sync(mock=args.mock, do_prune=not args.no_prune, window_days=args.since_days, limit=args.limit)
    if s.get("error"):
        print("✗ " + s["error"])
        return 1
    print(f"Apify scrape: {s['found']} reels fetched ({s['new']} new, {s['updated']} updated, "
          f"{s['skipped']} skipped) in {s['seconds']}s — est. ${s['cost_usd']} "
          f"(stages: {json.dumps(s['stages'])})")
    if args.no_prune:
        print("pool: (prune skipped)")
    else:
        print(f"pool: {s['kept']} qualifying ({s['dropped']} dropped) | newly qualifying: {len(s['new_qualifiers'])}")
        con = db()
        for sc in s["new_qualifiers"]:
            r = con.execute("SELECT author, likes_raw, views, posted, url FROM reels WHERE shortcode=?", (sc,)).fetchone()
            if r:
                print(f"  NEW: {r[1]} likes | {format_likes(r[2]) or '—'} views | {r[3][:10]} | {r[0]} | {r[4]}")
    return 0


def cmd_poll(args):
    if getattr(args, "json", False):
        s = sync(mock=args.mock, do_prune=not args.no_prune,
                 window_days=args.since_days, limit=args.limit)
        if s.get("error"):
            print("✗ " + s["error"])
            return 1
        ms = matches(args.window, args.likes, args.lang)
        if args.csv:
            write_csv(ms, args.csv)
        print(json.dumps({"run": s, "matches": ms}))
        return 0
    rc = cmd_sync(args)
    if rc:
        return rc
    ms = matches(args.window, args.likes, args.lang)
    print(f"\nmatches ({args.window}d, {args.likes}+ likes, lang={args.lang}): {len(ms)}")
    for r in ms:
        v = r["views_raw"] or "—"
        print(f"  {r['likes_raw']:>10} likes | {v:>8} views | {r['posted']} | {r['lang']} | {r['author']} | {r['url']}")
    if args.csv:
        n = write_csv(ms, args.csv)
        print(f"wrote {n} rows to {args.csv}")
    return 0


def cmd_list(args):
    ms = matches(args.window, args.likes, args.lang)
    if getattr(args, "json", False):
        if args.csv:
            write_csv(ms, args.csv)
        print(json.dumps(ms))
        return 0
    print(f"matches ({args.window}d, {args.likes}+ likes, lang={args.lang}): {len(ms)}")
    for r in ms:
        v = r["views_raw"] or "—"
        print(f"  {r['likes_raw']:>10} likes | {v:>8} views | {r['posted']} | {r['lang']} | {r['author']} | {r['url']}")
    if args.csv:
        n = write_csv(ms, args.csv)
        print(f"wrote {n} rows to {args.csv}")
    return 0


def cmd_add(args):
    for l in args.links:
        sc = add_reel(l)
        print(f"added: {sc} <- {l}" if sc else f"could not parse: {l}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="IG Trend Tracker v3 (Apify-backed)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    parsers = {}
    for name in ("poll", "sync", "list"):
        p = sub.add_parser(name)
        parsers[name] = p
        p.add_argument("--window", type=int, default=7)
        p.add_argument("--likes", type=int, default=10000)
        p.add_argument("--lang", default="all", choices=["all", "en", "hi"])
        if name in ("poll", "list"):
            p.add_argument("--csv", metavar="PATH", help="write the matches table to a CSV file")
        p.add_argument("--mock", action="store_true", help="simulate an Apify dataset (no token/credits)")
        p.add_argument("--no-prune", action="store_true", help="skip pool pruning (sync/seeds only)")
        p.add_argument("--limit", type=int, default=RESULTS_LIMIT, help="cap reels fetched per account profile")
        p.add_argument("--since-days", type=int, default=MAX_AGE_DAYS, dest="since_days",
                       help="scrape window in days (default 14)")
        p.add_argument("links", nargs="*")
    for name in ("poll", "list"):
        parsers[name].add_argument("--json", action="store_true",
                                   help="print matches as JSON (poll also includes the run summary)")
    p = sub.add_parser("seeds")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--no-prune", action="store_true")
    p.add_argument("--limit", type=int, default=RESULTS_LIMIT)
    p.add_argument("--since-days", type=int, default=MAX_AGE_DAYS, dest="since_days")
    p = sub.add_parser("add")
    p.add_argument("links", nargs="+")
    args = ap.parse_args()
    if getattr(args, "mock", False) and args.cmd in ("poll", "sync", "seeds"):
        global MOCK
        MOCK = True
        print("mock mode: using state_mock.db (real DB untouched)", file=sys.stderr)
    if args.cmd in ("poll", "sync", "seeds") and not getattr(args, "mock", False):
        err = check_token(load_token())
        if err:
            print("✗ " + err)
            return 1
    if args.cmd == "poll":
        return cmd_poll(args)
    if args.cmd == "sync":
        return cmd_sync(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "seeds":
        return cmd_sync(args)
    if args.cmd == "add":
        return cmd_add(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
