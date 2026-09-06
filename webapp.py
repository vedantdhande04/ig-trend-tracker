#!/usr/bin/env python3
"""IG Trend Tracker — web UI. Run: python webapp.py  (default http://127.0.0.1:8050)"""
import os, threading, time, webbrowser
from flask import Flask, request, render_template, jsonify
import tracker

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
app.template_folder = os.path.join(BASE, "templates")

_polling = {"active": False, "msg": ""}
WINDOWS = [7, 14]
LIKE_STEPS = [1000, 2000, 5000, 10000, 20000, 30000, 50000, 100000, 250000, 500000]


def fmt_last_checked(v):
    """Epoch-seconds string -> 'YYYY-MM-DD HH:MM' local time.

    Legacy rows may already hold a formatted string — pass those through.
    Returns None for NULL/empty values.
    """
    if not v:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v)))
    except (TypeError, ValueError):
        return v


def _do_poll():
    try:
        _polling["msg"] = "running Apify Instagram scrape…"
        s = tracker.sync()
        if s.get("error"):
            _polling["msg"] = f"✗ {s['error']}"
        else:
            _polling["msg"] = (f"✓ {s['found']} reels fetched (~${s['cost_usd']:.2f}), {s['new']} new, "
                               f"pool: {s['kept']} qualifying ({s['dropped']} dropped)")
    except Exception as e:
        _polling["msg"] = f"poll error: {e}"
    finally:
        _polling["active"] = False


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if request.form.get("action") == "poll" and not _polling["active"]:
            _polling["active"] = True
            _polling["msg"] = "starting poll…"
            threading.Thread(target=_do_poll, daemon=True).start()
        if request.form.get("action") == "add" and request.form.get("link"):
            tracker.add_reel(request.form["link"].strip())
    window = int(request.args.get("window", request.form.get("window", 14)))
    likes = int(request.args.get("likes", request.form.get("likes", 5000)))
    lang = request.args.get("lang", request.form.get("lang", "all"))
    hits = tracker.matches(window, likes, lang)
    con = tracker.db()
    total = con.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
    checked = con.execute("SELECT COUNT(*) FROM reels WHERE last_checked IS NOT NULL").fetchone()[0]
    last_updated = fmt_last_checked(con.execute("SELECT MAX(last_checked) FROM reels").fetchone()[0])
    recent = con.execute(
        "SELECT shortcode,url,author,likes,likes_raw,posted,lang,status FROM reels WHERE status='active' ORDER BY COALESCE(posted,first_seen) DESC LIMIT 60").fetchall()
    return render_template("index.html", hits=hits, recent=recent, total=total, checked=checked,
                           window=window, likes=likes, lang=lang,
                           windows=WINDOWS, like_steps=LIKE_STEPS, polling=_polling["active"],
                           poll_msg=_polling["msg"], last_updated=last_updated)


@app.route("/health")
def health():
    con = tracker.db()
    total = con.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
    active = con.execute("SELECT COUNT(*) FROM reels WHERE status='active'").fetchone()[0]
    return jsonify({"status": "ok", "reels_total": total, "reels_active": active,
                    "polling": _polling["active"]})


@app.route("/api/hits")
def api_hits():
    window = int(request.args.get("window", 7))
    likes = int(request.args.get("likes", 20000))
    lang = request.args.get("lang", "all")
    return jsonify(tracker.matches(window, likes, lang))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False)
