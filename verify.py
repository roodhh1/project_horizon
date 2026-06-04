#!/usr/bin/env python3
"""
Project Horizon — daily role-status verifier.

Re-checks each tracked role against the company's LIVE source of truth
(Greenhouse board API, or the self-hosted careers page) — NOT the cached
Greenhouse permalink, which keeps resolving after a posting is pulled.

Rewrites the STATUS{} block and VERIFIED_ON constant inside index.html,
between the // STATUS_BLOCK_START and // STATUS_BLOCK_END markers.

Stdlib only — no pip installs needed. Designed to run in GitHub Actions.
On a per-role network error it KEEPS the previous status (fail-safe) so a
transient blip never flips a role to "closed".
"""

import json
import re
import sys
import datetime
import urllib.request
import urllib.error

HTML = "index.html"
UA = {"User-Agent": "ProjectHorizon-verifier/1.0 (+github actions)"}
TODAY = datetime.date.today().isoformat()

# --- Tracked roles -----------------------------------------------------------
# type "greenhouse": live if job id is on the board (or title_regex matches a live title)
# type "url": live if the page returns 200 and does not look like a 404/closed page
TRACKED = [
    {"key": "Mercury|Senior Data Science Manager",
     "type": "greenhouse", "token": "mercury", "id": "5973037004"},
    {"key": "Anthropic|Analytics Data Engineering Manager, Product",
     "type": "greenhouse", "token": "anthropic", "id": "5125387008"},
    {"key": "Gusto|Senior Manager, Growth Data Science",
     "type": "greenhouse", "token": "gusto", "id": "7357545"},
    {"key": "Chime|Senior Manager, Product Analytics — Lending",
     "type": "greenhouse", "token": "chime", "id": "8484048002"},
    {"key": "Brex|Data Manager, Analytics",
     "type": "greenhouse", "token": "brex", "id": "8174260002"},
    {"key": "Brex|Data Analytics Lead, Business Operations",
     "type": "greenhouse", "token": "brex", "id": "7629290002"},
    {"key": "Brex|Senior Data Scientist, Product Analytics",
     "type": "greenhouse", "token": "brex", "id": "7924881002"},
    {"key": "Plaid|Engineering Manager, Machine Learning",
     "type": "url",
     "url": "https://plaid.com/careers/openings/engineering/new-york/engineering-manager-machine-learning/",
     "live_label": "Careers page", "dead_label": "Careers 404"},
    {"key": "Plaid|Senior Data Scientist (Product)",
     "type": "url",
     "url": "https://plaid.com/careers/openings/engineering/san-francisco/senior-data-scientist/",
     "live_label": "Careers page", "dead_label": "Careers 404"},
    {"key": "Stripe|Data Science Manager, Growth",
     "type": "url",
     "url": "https://stripe.com/jobs/listing/data-science-manager-growth/7440963",
     "live_label": "Careers page", "dead_label": "Careers closed"},
    {"key": "Coinbase|Senior Data Science Manager",
     "type": "greenhouse", "token": "coinbase", "id": "6056727"},
    {"key": "OpenAI|Data Science Manager, Integrity",
     "type": "url",
     "url": "https://openai.com/careers/data-science-manager-integrity-san-francisco/",
     "live_label": "Careers page", "dead_label": "Careers 404"},
    {"key": "Affirm|Director, Analytics, Strategic Insights",
     "type": "greenhouse", "token": "affirm", "id": "7718616003"},
    {"key": "Affirm|Senior Manager, Analytics (Full Stack)",
     "type": "greenhouse", "token": "affirm", "id": "7491433003"},
    {"key": "DoorDash|Senior Manager, Data Science, Analytics — Notifications, Consumer Growth",
     "type": "greenhouse", "token": "doordashusa", "id": "6495978"},
]

_board_cache = {}  # token -> {"ids": set, "titles": [lowercase titles]}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read().decode("utf-8", "replace")


def load_board(token):
    if token in _board_cache:
        return _board_cache[token]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    code, body = fetch(url)
    data = json.loads(body)
    jobs = data.get("jobs", [])
    info = {"ids": {str(j.get("id")) for j in jobs},
            "titles": [str(j.get("title", "")).lower() for j in jobs]}
    _board_cache[token] = info
    return info


def check_greenhouse(role):
    info = load_board(role["token"])
    if "id" in role:
        live = role["id"] in info["ids"]
    else:
        rx = re.compile(role["title_regex"], re.I)
        live = any(rx.search(t) for t in info["titles"])
    return live, (f"Board API · checked {TODAY}" if live
                  else f"Not on board · checked {TODAY}")


def check_url(role):
    # Status-based: live unless the server says gone (404/410) or the page
    # clearly states the role is closed. We deliberately do NOT require a
    # positive "Apply" signal — self-hosted sites vary their markup and can
    # serve different HTML to datacenter IPs, which would false-negative.
    dead_markers = ("lost in 404", "page not found", "this position is no longer",
                    "no longer accepting", "has been filled", "position is closed",
                    "404 error")
    try:
        code, body = fetch(role["url"])
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False, f"{role['dead_label']} · checked {TODAY}"
        raise
    low = body.lower()
    looks_dead = any(m in low for m in dead_markers)
    live = (code == 200) and not looks_dead
    return live, (f"{role['live_label']} · checked {TODAY}" if live
                  else f"{role['dead_label']} · checked {TODAY}")


def parse_existing_status(html):
    m = re.search(r"const STATUS\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        return {}
    js = m.group(1)
    out = {}
    for km in re.finditer(r'"([^"]+)":\s*\{live:(true|false),\s*v:"([^"]*)"\}', js):
        out[km.group(1)] = {"live": km.group(2) == "true", "v": km.group(3)}
    return out


def main():
    html = open(HTML, encoding="utf-8").read()
    prev = parse_existing_status(html)
    new_status = {}
    changes = []

    for role in TRACKED:
        key = role["key"]
        try:
            live, v = (check_greenhouse(role) if role["type"] == "greenhouse"
                       else check_url(role))
        except Exception as e:  # fail-safe: keep previous status on a network error
            p = prev.get(key, {"live": True, "v": ""})
            live, v = p["live"], f"{p.get('v','')} (recheck failed {TODAY})".strip()
            print(f"  WARN {key}: {e!r} -> kept previous ({'live' if live else 'closed'})")
        else:
            # Debounce live->closed: a single "closed" read (e.g. a stale board
            # snapshot or anti-bot page) must not flip a live role. Require TWO
            # consecutive closed reads. First closed read is held as live and
            # tagged "unconfirmed"; the next closed read confirms the close.
            p = prev.get(key)
            if (not live) and p and p.get("live") and "unconfirmed" not in p.get("v", ""):
                live, v = True, f"Live (closing? unconfirmed) · {TODAY}"
                print(f"  HOLD {key}: closed read held pending confirmation")
        new_status[key] = {"live": live, "v": v}
        was = prev.get(key, {}).get("live")
        if was is not None and was != live:
            changes.append(f"{key}: {'OPENED' if live else 'CLOSED'}")
        print(f"  {'LIVE ' if live else 'CLOSED'} {key}")

    # Render the JS block
    lines = ["// STATUS_BLOCK_START",
             f'const VERIFIED_ON = "{TODAY}";',
             "const STATUS = {"]
    items = []
    for role in TRACKED:
        s = new_status[role["key"]]
        k = role["key"].replace('"', '\\"')
        v = s["v"].replace('"', '\\"')
        items.append(f'  "{k}": {{live:{str(s["live"]).lower()}, v:"{v}"}}')
    lines.append(",\n".join(items))
    lines.append("};")
    lines.append("// STATUS_BLOCK_END")
    block = "\n".join(lines)

    new_html, n = re.subn(
        r"// STATUS_BLOCK_START.*?// STATUS_BLOCK_END",
        lambda _: block, html, count=1, flags=re.S)
    if n != 1:
        print("ERROR: could not find STATUS markers in index.html", file=sys.stderr)
        sys.exit(1)

    if new_html != html:
        open(HTML, "w", encoding="utf-8").write(new_html)
        print(f"\nUpdated {HTML} (verified {TODAY}).")
    else:
        print(f"\nNo content change (verified {TODAY}).")

    live_n = sum(1 for s in new_status.values() if s["live"])
    print(f"Summary: {live_n} live / {len(new_status) - live_n} closed of {len(new_status)} tracked.")
    if changes:
        print("Changes since last run:\n  - " + "\n  - ".join(changes))


if __name__ == "__main__":
    main()
