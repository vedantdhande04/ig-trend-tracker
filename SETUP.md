# IG Trend Tracker v3 — Apify setup (one time, ~3 min)

The tracker now uses Apify's official `instagram-scraper` actor instead of search-engine
discovery + Microlink (both were constantly rate-limited).

## 1. Create a free Apify account
- Go to https://apify.com and sign up.
- Free plan: **$5 of credits every month, no credit card required**.

## 2. Copy your API token
- Apify Console → Settings → Integrations → **API token** → Copy.

## 3. Save the token
Either (pick one):

**Option A — environment variable (persistent):**
```
setx APIFY_TOKEN "apify_xxxxxxxxxxxxxxxx"
```
(new terminals pick it up; restart the webapp after)

**Option B — .env file in this folder:**
Create `.env` next to `tracker.py` with one line:
```
APIFY_TOKEN=apify_xxxxxxxxxxxxxxxx
```

## 4. No Instagram account needed

The actor scrapes public data logged-out. (If you see `no_items / Empty or private data`,
double-check the APIFY_TOKEN is set — that error can also appear when the run input
didn't reach the actor.)

## Verify
```
python tracker.py poll --likes 5000 --window 14
```
You should see something like:
`Apify scrape: 325 reels fetched (… new, … updated) in 210s — est. $0.75 ($2.30/1k)`

## Cost model
- Main actor (instagram-scraper): **$0.0027/result** on the free plan ($2.70/1k).
- Hashtag-scraper (discovery feed): **$0.0026/result** ($2.60/1k on the free plan).
- One sync = 3 stages: creator profiles (~70) + hashtag feed (8 tags × 12) + enrichment of
  discovered reels (≤100, deduped) ≈ **~230 items ≈ $0.70/run**.
- Weekly cadence ≈ **$2.80–3.50/month** → fits the $5 free tier. Check spend anytime at
  console.apify.com → Usage (each "Run poll" click in the UI costs ~$0.70!).

## How a sync works (3 stages)
1. **Profiles** — main actor, reels mode, your accounts.txt creators (5/week rotation) + any
   user-added reel links → full stats directly.
2. **Hashtag discovery** — hashtag-scraper feeds for `HASHTAGS` → real shortcodes
   (the grid likes are 0, so we don't trust them).
3. **Enrichment** — main actor re-scrapes each discovered reel URL → real likes + plays.

## Tuning knobs (top of tracker.py)
- `HASHTAGS` — the trend-discovery tags (add Hindi ones for the Hindi feed)
- `RESULTS_LIMIT` — reels per hashtag/profile per run
- `PROFILES_PER_RUN` — how many of your curated accounts.txt creators per run
  (they rotate weekly so all get scraped over a month)
- `MIN_LIKES` / `MAX_AGE_DAYS` — pool retention rules

## Testing without spending anything
```
python tracker.py poll --mock
```
Simulates an Apify dataset locally (no account, no credits) — exercises the whole
pipeline: upsert → prune → matches.
