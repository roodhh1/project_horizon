#!/usr/bin/env python3
"""
Project Horizon — daily role refresh: VERIFY · PRUNE · DISCOVER.

Single source of truth is roles.json ({"verified_on", "roles":[...]}). The
dashboard (index.html) fetches that file and renders only what's in it.

Each run:
  1. VERIFY  every tracked role against the company's LIVE source of truth
     (Greenhouse board API, or the self-hosted careers page) — NOT the cached
     Greenhouse permalink, which keeps resolving after a posting is pulled.
  2. PRUNE   roles confirmed closed are removed from roles.json entirely, so
     the dashboard only ever holds what is truly live. A live->closed flip is
     debounced (held one run as "unconfirmed") so a transient blip / anti-bot
     page never drops a real role.
  3. DISCOVER new matching DS/Analytics-leadership roles on the target
     companies' Greenhouse boards and add them, tagged NEW (unscored) for
     review. (LinkedIn discovery + the referral sweep are done assisted, in a
     logged-in browser — never headless here.)

Posting dates: where a role is on a Greenhouse board, we record `posted`
(the board's first_published, YYYY-MM-DD) so the dashboard can show posting age.

Stdlib only — no pip installs. Designed to run in GitHub Actions.
"""

import json
import re
import sys
import datetime
import urllib.request
import urllib.error

DATA = "roles.json"
PIPELINE = "pipeline.json"   # YOU own this: pinned applications + app stages. Merged into
                             # roles.json every run; the bot never overwrites it (clobber-proof).
UA = {"User-Agent": "ProjectHorizon-verifier/2.2 (+github actions)"}
TODAY = datetime.date.today().isoformat()

# --- Discovery tuning --------------------------------------------------------
DISCOVER = True
# A discovered role must show a data/ML signal AND a leadership signal, and be
# US-based / remote. Conservative on purpose — new finds are flagged for review,
# not trusted blindly.
DATA_SIGNAL = ("data scien", "analytics", "machine learning",
               "ml engineer", "applied scien", "data engineer")
LEAD_SIGNAL = ("manager", " lead", "lead,", "lead ", "head of", "head,",
               "director", "principal", "vp ", "vp,")
US_HINTS = ("united states", "remote", "u.s", " us", "new york", "san francisco",
            "sf", "bay area", "seattle", "los angeles", "austin", "denver",
            "chicago", "boston", "washington", "atlanta", "mountain view",
            "palo alto", "sunnyvale", "menlo park", "california", "texas",
            "oregon", "new york city", "nyc")
MAX_NEW_PER_COMPANY = 8

_board_cache = {}  # token -> {"jobs":[...], "ids":set, "titles":[...], "pub":{id:date}}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read().decode("utf-8", "replace")


def load_board(token):
    if token in _board_cache:
        return _board_cache[token]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    code, body = fetch(url)
    jobs = json.loads(body).get("jobs", [])
    info = {"jobs": jobs,
            "ids": {str(j.get("id")) for j in jobs},
            "titles": [str(j.get("title", "")).lower() for j in jobs],
            "pub": {str(j.get("id")): (j.get("first_published") or "")[:10] for j in jobs}}
    _board_cache[token] = info
    return info


def check_greenhouse(cfg):
    info = load_board(cfg["token"])
    if "id" in cfg:
        live = str(cfg["id"]) in info["ids"]
    else:
        rx = re.compile(cfg["title_regex"], re.I)
        live = any(rx.search(t) for t in info["titles"])
    return live, (f"Board API · checked {TODAY}" if live
                  else f"Not on board · checked {TODAY}")


def check_url(cfg):
    dead_markers = ("lost in 404", "page not found", "this position is no longer",
                    "no longer accepting", "has been filled", "position is closed",
                    "404 error")
    try:
        code, body = fetch(cfg["url"])
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False, f"{cfg['dead_label']} · checked {TODAY}"
        raise
    low = body.lower()
    looks_dead = any(m in low for m in dead_markers)
    live = (code == 200) and not looks_dead
    return live, (f"{cfg['live_label']} · checked {TODAY}" if live
                  else f"{cfg['dead_label']} · checked {TODAY}")


def verify_one(role):
    """Return (live, v) for a role, with fail-safe + live->closed debounce."""
    cfg = role.get("verify")
    prev = role.get("status", {"live": True, "v": ""})
    if not cfg:                       # unverifiable: keep whatever it had
        return prev["live"], prev.get("v", "")
    try:
        live, v = (check_greenhouse(cfg) if cfg["type"] == "greenhouse"
                   else check_url(cfg))
    except Exception as e:            # network blip -> keep previous status
        print(f"  WARN {role['co']}|{role['role']}: {e!r} -> kept previous")
        return prev["live"], f"{prev.get('v','')} (recheck failed {TODAY})".strip()
    # Debounce: one isolated closed read on a live role is held, not acted on.
    if (not live) and prev.get("live") and "unconfirmed" not in prev.get("v", ""):
        print(f"  HOLD {role['co']}|{role['role']}: closed read held pending confirmation")
        return True, f"Live (closing? unconfirmed) · {TODAY}"
    return live, v


def posted_for(cfg):
    """first_published (YYYY-MM-DD) for a greenhouse role, or '' if unavailable.
    Self-hosted / LinkedIn postings don't expose a reliable date, so they have none."""
    if not cfg or cfg.get("type") != "greenhouse":
        return ""
    try:
        info = load_board(cfg["token"])
    except Exception:
        return ""
    if cfg.get("id"):
        return info["pub"].get(str(cfg["id"]), "")
    rx = re.compile(cfg.get("title_regex", "$^"), re.I)   # title_regex roles
    for j in info["jobs"]:
        if rx.search(str(j.get("title", ""))):
            return (j.get("first_published") or "")[:10]
    return ""


def load_pipeline():
    """Read the user-owned application pipeline. Returns {} on any problem so a
    bad/missing pipeline.json never breaks the daily run."""
    try:
        return json.load(open(PIPELINE, encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  WARN pipeline.json unreadable ({e!r}) -> skipping merge")
        return {}


def merge_pipeline(kept):
    """Merge YOUR pipeline.json into the bot's auto set:
      - overrides: attach pinned + app stage to an auto-discovered role you've applied to.
      - pinned_roles: full manually-tracked applications — verified for display but NEVER
        pruned (so closed/rejected applications stay in your history)."""
    pipe = load_pipeline()
    if not pipe:
        return kept
    by_key = {r["co"] + "|" + r["role"]: r for r in kept}
    for key, ov in (pipe.get("overrides") or {}).items():
        if key in by_key:
            by_key[key]["pinned"] = True
            if "app" in ov:
                by_key[key]["app"] = ov["app"]
            print(f"  PIPE  override {key}")
    for pr in (pipe.get("pinned_roles") or []):
        pr = dict(pr)
        pr["pinned"] = True
        live, v = verify_one(pr)
        pr["status"] = {"live": live, "v": v}
        p = posted_for(pr.get("verify"))
        if p:
            pr["posted"] = p
        key = pr["co"] + "|" + pr["role"]
        if key in by_key:
            by_key[key].update(pr)
        else:
            kept.append(pr)
        print(f"  PIPE  pinned {key} (status: {v or 'n/a'})")
    return kept


def norm_title(s):
    s = s.lower().replace("&", "and")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def discover(existing_roles, token_to_co):
    """Find new leadership DS/analytics roles on the target Greenhouse boards."""
    seen = {(r["verify"]["token"], str(r["verify"].get("id")))
            for r in existing_roles
            if r.get("verify", {}).get("type") == "greenhouse" and r["verify"].get("id")}
    # Also dedup by company + normalized title, so a role we already track (or
    # just added) isn't re-surfaced because it was reposted under a new id or a
    # slightly different spelling ("&" vs "and", punctuation, casing).
    seen_titles = {(r["co"].lower(), norm_title(r["role"])) for r in existing_roles}
    found = []
    for token in sorted(token_to_co):
        try:
            info = load_board(token)
        except Exception as e:
            print(f"  WARN discover {token}: {e!r}")
            continue
        added = 0
        co = token_to_co[token]
        for j in info["jobs"]:
            jid = str(j.get("id"))
            if (token, jid) in seen:
                continue
            title = str(j.get("title", ""))
            if (co.lower(), norm_title(title)) in seen_titles:
                continue
            tl = title.lower()
            loc = str((j.get("location") or {}).get("name", ""))
            ll = loc.lower()
            if not any(s in tl for s in DATA_SIGNAL):
                continue
            if not any(s in tl for s in LEAD_SIGNAL):
                continue
            if not any(h in ll for h in US_HINTS):
                continue
            if added >= MAX_NEW_PER_COMPANY:
                print(f"  NOTE discover {token}: capped at {MAX_NEW_PER_COMPANY}, more may exist")
                break
            found.append({
                "co": co, "role": title, "loc": loc or "See posting",
                "track": "new", "trackLabel": "New find", "extraTrack": "",
                "score": None, "domain": None,
                "comp": "Not posted", "compFit": None, "tier": "",
                "url": j.get("absolute_url", ""),
                "posted": (j.get("first_published") or "")[:10],
                "note": (f"Auto-discovered on {co}'s job board {TODAY}. "
                         "Not yet scored — review fit, comp, and referral path."),
                "verify": {"type": "greenhouse", "token": token, "id": jid},
                "status": {"live": True, "v": f"Board API · discovered {TODAY}"},
                "referral": {"level": "pending", "text": ""},
                "discovered": True, "isNew": True,
            })
            seen.add((token, jid))
            seen_titles.add((co.lower(), norm_title(title)))
            added += 1
        if added:
            print(f"  DISCOVER {token_to_co[token]}: +{added} new")
    return found


def main():
    data = json.load(open(DATA, encoding="utf-8"))
    roles = data.get("roles", [])

    token_to_co = {r["verify"]["token"]: r["co"] for r in roles
                   if r.get("verify", {}).get("type") == "greenhouse"}

    kept, pruned = [], []
    for role in roles:
        live, v = verify_one(role)
        role["status"] = {"live": live, "v": v}
        p = posted_for(role.get("verify"))   # stamp posting date when available
        if p:
            role["posted"] = p
        # PINNED roles (manually tracked in the application pipeline) are never
        # pruned, even when read as closed — we still record their live/closed
        # status for display, but they stay in roles.json so the pipeline keeps
        # its history (applied / rejected / closed). Their `app` field is
        # preserved automatically on round-trip.
        if live:
            kept.append(role)
            print(f"  LIVE  {role['co']}|{role['role']}")
        elif role.get("pinned"):
            kept.append(role)
            print(f"  PIN   {role['co']}|{role['role']} (kept; status: {v})")
        else:
            pruned.append(role)
            print(f"  PRUNE {role['co']}|{role['role']} ({v})")

    new_roles = discover(kept, token_to_co) if DISCOVER else []
    kept.extend(new_roles)

    # Merge YOUR application pipeline last, so pinned applications + stages survive
    # even if roles.json was clobbered by a manual upload.
    kept = merge_pipeline(kept)

    data["verified_on"] = TODAY
    data["roles"] = kept
    json.dump(data, open(DATA, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print(f"\nVerified {TODAY}: {len(kept)} live "
          f"({len(new_roles)} newly discovered), {len(pruned)} pruned.")
    if pruned:
        print("Pruned (closed):\n  - " +
              "\n  - ".join(f"{r['co']}|{r['role']}" for r in pruned))
    if new_roles:
        print("New (needs review):\n  - " +
              "\n  - ".join(f"{r['co']}|{r['role']}" for r in new_roles))


if __name__ == "__main__":
    main()
