#!/usr/bin/env python3
"""
Edge Desk proxy  --  serves your PRIVATE clv_log.jsonl to the dashboard.

WHY THIS EXISTS
  The dashboard is a static page. Your bet log lives in a PRIVATE GitHub repo,
  so a browser can't read it without a token -- and a token in browser JS is a
  leaked token. This tiny server sits in between: it holds the token
  server-side (as an env var, never in the page), fetches the private file,
  and hands the dashboard clean JSON. The token never reaches the browser.

  It also (optionally) fetches a public cross-check feed and computes a couple
  of summary stats server-side, so the page stays dumb and fast.

WHAT IT SERVES
  GET /            -> the dashboard HTML (same file, now auto-loading)
  GET /api/ledger  -> parsed rows from your private clv_log.jsonl, as JSON
  GET /api/health  -> quick status: token present? repo reachable? row count?

DEPLOY (Railway / Render, both have free tiers)
  Set these environment variables:
    GH_TOKEN   = a GitHub fine-grained PAT with read-only "Contents" access
                 to just the kalshi-edges repo. Nothing else.
    GH_REPO    = poneil/kalshi-edges
    GH_PATH    = clv_log.jsonl           (path within the repo)
    GH_BRANCH  = main                    (optional, defaults to main)
  Then it runs itself. No local setup, no secrets in the code.

SECURITY NOTES
  - The token is read from the environment only. It is never logged, never
    sent to the browser, never written to disk.
  - A fine-grained PAT scoped to read-only Contents on ONE repo is the least
    privilege that works. If it leaks, the worst case is someone reads your
    paper-bet log -- no write access, no other repos, no account access.
  - CORS is open (any origin can GET), because the responses contain only your
    own already-non-sensitive paper data. If you'd rather lock it to one
    origin, set ALLOWED_ORIGIN and it will echo only that.
"""

import json
import os
import time
from urllib.parse import quote

from flask import Flask, Response, jsonify, request

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency. This runs with: pip install -r requirements.txt")

app = Flask(__name__)

# --------------------------------------------------------------------------
# CONFIG  (all from the environment -- nothing sensitive in this file)
# --------------------------------------------------------------------------
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = (os.environ.get("GH_REPO") or "poneil/kalshi-edges").strip()
GH_PATH = (os.environ.get("GH_PATH") or "clv_log.jsonl").strip()
GH_BRANCH = (os.environ.get("GH_BRANCH") or "main").strip()
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

# Optional public cross-check feed (SportsBookISH). Off unless a URL is set,
# so a third-party outage can never affect your own dashboard.
CROSSCHECK_URL = os.environ.get("CROSSCHECK_URL", "").strip()

# Small in-process cache so hammering refresh doesn't hammer GitHub.
_CACHE = {"ledger": None, "ts": 0.0}
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS") or "60")

# Path to the dashboard file next to this script.
HERE = os.path.dirname(os.path.abspath(__file__))
DASH_FILE = os.path.join(HERE, "kalshi_dashboard.html")


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def fetch_private_log():
    """Fetch clv_log.jsonl from the private repo via the GitHub Contents API.

    Uses the raw media type so we get the file body directly (not base64 JSON).
    Returns (rows, error). rows is a list of parsed alert dicts; error is a
    human string or None. Never raises -- the dashboard should degrade, not 500.
    """
    if not GH_TOKEN:
        return [], "GH_TOKEN is not set on the server."
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{quote(GH_PATH)}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "edge-desk-proxy/1.0",
    }
    params = {"ref": GH_BRANCH}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
    except Exception as e:
        return [], f"network error reaching GitHub: {e}"

    if r.status_code == 404:
        return [], (f"file not found: {GH_REPO}/{GH_PATH}@{GH_BRANCH}. "
                    f"Check GH_REPO, GH_PATH, GH_BRANCH.")
    if r.status_code in (401, 403):
        return [], (f"GitHub rejected the token (HTTP {r.status_code}). "
                    f"Check the PAT has read-only Contents access to {GH_REPO}.")
    if r.status_code != 200:
        return [], f"GitHub returned HTTP {r.status_code}."

    rows, bad = [], 0
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "alert":
                rows.append(obj)
        except json.JSONDecodeError:
            bad += 1
    note = f"{bad} unparseable lines skipped" if bad else None
    return rows, note


def summarize(rows):
    """Compute the same core stats the dashboard shows, server-side, so /api
    consumers (or a phone widget) can read them without the front-end."""
    settled = [r for r in rows if r.get("result") in ("win", "loss")]
    wins = sum(1 for r in settled if r["result"] == "win")
    pnl = sum((r.get("pnl") or 0.0) for r in settled)
    staked = sum((r.get("paper_stake") or 0.0) for r in settled)
    bal = None
    for r in rows:
        if r.get("balance_after") is not None:
            bal = r["balance_after"]
    return {
        "rows": len(rows),
        "settled": len(settled),
        "open": len(rows) - len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": (wins / len(settled)) if settled else None,
        "pnl": round(pnl, 2),
        "staked": round(staked, 2),
        "roi": (pnl / staked) if staked else None,
        "balance": bal,
    }


@app.route("/api/ledger")
def api_ledger():
    now = time.time()
    if _CACHE["ledger"] is not None and now - _CACHE["ts"] < CACHE_SECONDS:
        rows, note = _CACHE["ledger"], "cached"
    else:
        rows, note = fetch_private_log()
        if note != "GH_TOKEN is not set on the server.":
            _CACHE["ledger"], _CACHE["ts"] = rows, now
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": note,
        "summary": summarize(rows),
        "rows": rows,
    }
    return _cors(jsonify(payload))


@app.route("/api/crosscheck")
def api_crosscheck():
    """Optional passthrough to a public consensus feed, so the browser doesn't
    hit CORS issues. Degrades to empty on any failure -- never blocks."""
    if not CROSSCHECK_URL:
        return _cors(jsonify({"enabled": False, "data": None}))
    try:
        r = requests.get(CROSSCHECK_URL, timeout=10,
                         headers={"User-Agent": "edge-desk-proxy/1.0"})
        if r.status_code == 200:
            return _cors(jsonify({"enabled": True, "data": r.json()}))
        return _cors(jsonify({"enabled": True, "data": None,
                              "error": f"feed HTTP {r.status_code}"}))
    except Exception as e:
        return _cors(jsonify({"enabled": True, "data": None, "error": str(e)}))


@app.route("/api/health")
def api_health():
    rows, note = fetch_private_log()
    return _cors(jsonify({
        "token_present": bool(GH_TOKEN),
        "repo": GH_REPO,
        "path": GH_PATH,
        "branch": GH_BRANCH,
        "reachable": note is None or note == "cached" or "skipped" in (note or ""),
        "note": note,
        "row_count": len(rows),
        "crosscheck_enabled": bool(CROSSCHECK_URL),
    }))


@app.route("/")
def index():
    try:
        with open(DASH_FILE, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("kalshi_dashboard.html not found next to server.py",
                        status=500, mimetype="text/plain")


@app.route("/api/ledger", methods=["OPTIONS"])
@app.route("/api/crosscheck", methods=["OPTIONS"])
def _preflight():
    return _cors(Response(""))


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or "5555")
    app.run(host="0.0.0.0", port=port)
