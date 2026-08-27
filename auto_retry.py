#!/usr/bin/env python3
"""Auto-retry discovery until rate limits cool down, then run full poll.
Bounded: tries every 10 min for up to 3 hours, exits on first success."""
import subprocess, sys, time, os, re

BASE = r"C:\Users\ASUS\ig-trend-tracker"
os.chdir(BASE)
MAX_ATTEMPTS = 18
SLEEP = 600

def try_once(i):
    print(f"=== attempt {i}/{MAX_ATTEMPTS} at {time.strftime('%H:%M:%S')} ===", flush=True)
    try:
        out = subprocess.run(
            [sys.executable, "tracker.py", "poll",
             "--min", "5000"],
            capture_output=True, text=True, timeout=900, cwd=BASE)
        text = (out.stdout or "") + (out.stderr or "")
        text = "\n".join(l for l in text.splitlines() if "DeprecationWarning" not in l)
        print(text, flush=True)
        # success = at least one reel actually qualified (stats flowed end-to-end)
        m = re.search(r"qualifying reels in pool: ([0-9]+)", text)
        if m and int(m.group(1)) > 0:
            print("SUCCESS: qualifying reels in pool", flush=True)
            return True
        m = re.search(r"newly qualifying: ([0-9]+)", text)
        if m and int(m.group(1)) > 0:
            print("SUCCESS: newly qualifying reels", flush=True)
            return True
        # candidates discovered but stats still blocked = keep waiting
        m = re.search(r"found (\d+) new candidates", text)
        if m and int(m.group(1)) > 0:
            print("  (candidates found but stats not flowing yet — retrying)", flush=True)
    except subprocess.TimeoutExpired:
        print("  (attempt timed out)", flush=True)
    except Exception as e:
        print(f"  (error: {e})", flush=True)
    return False

def main():
    for i in range(1, MAX_ATTEMPTS + 1):
        if try_once(i):
            sys.exit(0)
        if i < MAX_ATTEMPTS:
            print(f"still rate-limited, waiting {SLEEP}s...", flush=True)
            time.sleep(SLEEP)
    print("gave up after", MAX_ATTEMPTS, "attempts", flush=True)
    sys.exit(1)

if __name__ == "__main__":
    main()
