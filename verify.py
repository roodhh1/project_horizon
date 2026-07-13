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
# EXPANDED 2026-07-13 (Rodrigo widened the search): three qualifying lanes now —
#   (A) DS / Analytics LEADERSHIP  (data signal + people-leadership signal)
#   (B) SENIOR-IC data science     (Staff / Senior Staff / Principal DS — no team,
#       but senior scope; Rodrigo explicitly opened the IC lane)
#   (C) DATA / AI / GROWTH PM       (senior Product Manager adjacent to his wedge —
#       data products, AI/ML products, growth/experimentation, monetization,
#       analytics/data platforms). Pure/off-wedge PM is NOT surfaced.
# Location widened to anywhere in the US (+ remote) AND Mexico City (genuine
# relocation option). New finds are still flagged for review, not trusted blindly.
DATA_SIGNAL = ("data scien", "analytics", "machine learning",
               "ml engineer", "applied scien")
# NOTE: "data engineer" dropped from DATA_SIGNAL 2026-07-13 — Rodrigo is DS/analytics,
# not data-engineering; DE roles were adding noise. Also hard-negatived below.
# "staff" added so Staff / Senior Staff DS ICs qualify as senior-scope leadership.
LEAD_SIGNAL = ("manager", " lead", "lead,", "lead ", "head of", "head,",
               "director", "principal", "vp ", "vp,", "staff")

# --- Senior PM lane (Data / AI / Growth only) --------------------------------
# A role qualifies as a PM find only if it is (title says PM) AND (wedge-adjacent
# domain) AND (senior level). This keeps pure/junior PM off the board.
PM_TITLE = ("product manager", "product management", "group product manager",
            "product lead", "head of product", "director of product",
            "director, product", "vp of product", "vp, product")
PM_WEDGE = ("data", "analytics", " ai", "ai ", "artificial intelligence",
            "machine learning", " ml", "ml ", "growth", "experimentation",
            "monetization", "monetisation", "pricing", "platform", "payments",
            "fintech", "lifecycle", "activation", "acquisition", "retention",
            "personalization", "personalisation")
PM_SENIOR = ("senior", "sr.", "sr ", "staff", "principal", "lead", "group",
             "head", "director", "vp ", "vp,", "gpm", "ii", "iii")

# Location: anywhere in the US / remote, PLUS Mexico City (relocation option).
LOC_HINTS = ("united states", "remote", "u.s", " us", "usa", "new york",
             "san francisco", "sf", "bay area", "seattle", "los angeles",
             "austin", "denver", "chicago", "boston", "washington", "atlanta",
             "mountain view", "palo alto", "sunnyvale", "menlo park",
             "california", "texas", "oregon", "new york city", "nyc",
             # Mexico City (genuine relocation option, full weight):
             "mexico city", "ciudad de mexico", "ciudad de méxico", "cdmx",
             "mexico", "méxico")
# Reject roles really based in a non-target country even though a loose hint (e.g.
# bare "remote") matched — e.g. "Remote Canada". A non-target location is rescued
# only if the string ALSO names an explicit US/Mexico-City place (open in both).
NON_TARGET_LOC = ("canada", "ontario", "british columbia", "alberta",
                  "nova scotia", "quebec", "toronto", "vancouver",
                  "united kingdom", " u.k", "ireland", "india", "singapore",
                  "australia", "germany", "france", "netherlands", "poland",
                  "brazil", "brasil", "argentina", "colombia", "chile",
                  "europe", "emea", "apac", "latam")
STRONG_TARGET = tuple(t for t in LOC_HINTS if t != "remote")


def _loc_ok(ll):
    """Location is US / remote-US or Mexico City. Bare-'remote' matches are kept
    only if the string isn't actually a non-target country ('Remote Canada')."""
    if not any(t in ll for t in LOC_HINTS):
        return False
    if any(n in ll for n in NON_TARGET_LOC) and not any(s in ll for s in STRONG_TARGET):
        return False
    return True


MAX_NEW_PER_COMPANY = 10

# Bullseye companies to actively DISCOVER on every run, beyond whatever is already
# tracked in roles.json. Maps a Greenhouse board token -> company label. Tokens are
# validated live each run: a bad/unknown token just logs a WARN and is skipped (see
# discover()), so this list is safe to extend. Curated for Rodrigo's wedge: high-growth
# fintech, top consumer/marketplace tech, and AI-adjacent data orgs at Director / Sr-Mgr
# scope. (Companies on Ashby/Lever boards — e.g. Ramp, OpenAI, Perplexity — aren't
# Greenhouse and can't be discovered here; they stay LinkedIn-sourced.)
DISCOVER_TOKENS = {
    "robinhood": "Robinhood",
    "coinbase": "Coinbase",
    "brex": "Brex",
    # Plaid removed: it is NOT on Greenhouse (token 404s) — different ATS, can't be
    # auto-discovered here; Plaid stays a manually-tracked referral target.
    "instacart": "Instacart",
    "reddit": "Reddit",
    "airbnb": "Airbnb",
    "databricks": "Databricks",
    "lyft": "Lyft",
    "pinterest": "Pinterest",
    "stripe": "Stripe",
    "sofi": "SoFi",
    # Added 2026-07-13 with the widened search. Bad/unknown tokens just log a WARN
    # and are skipped, so this list is safe to extend. Several chosen for strong
    # Mexico City / LatAm engineering presence now that CDMX is in play.
    "gusto": "Gusto",
    "affirm": "Affirm",
    "doordash": "DoorDash",
    "chime": "Chime",
    "nubank": "Nubank",      # major CDMX/LatAm fintech hub
    "uber": "Uber",          # large CDMX data/PM org
    "mercadolibre": "MercadoLibre",  # LatAm marketplace/fintech (may be non-GH)
}

# --- Wedge curation ----------------------------------------------------------
# Keep the board a focused decision tool aligned to Rodrigo's wedge (product /
# growth / monetization / marketing analytics & DS leadership). Applied ONLY to
# auto-discovered roles — never to pinned applications or the hand-curated base set.
# HARD_NEGATIVE drops a role outright (off-wedge domain). SOFT_NEGATIVE (a wrong
# FUNCTION — ML/data engineering, program/product mgmt) drops only when the title
# lacks an analytics/DS signal, so "Data Science Manager, ..." survives but
# "ML Engineering Manager, ..." does not.
HARD_NEGATIVE = ("fraud", "safety", "security", "mapping", " audit",
                 "internal audit", "contracts", "compliance",
                 "strategic finance", "finance and strategy", "market insights",
                 "program manager", "technical program",
                 "data engineer", "data engineering")
SOFT_NEGATIVE = ("machine learning", "ml engineer", "software engineer",
                 "engineering manager", "program manager", "technical program",
                 "product manager")
PROTECT = ("analytics", "data science", "data scientist")
EXCLUDE_KEYS = {
    "Airbnb|Lead, Advanced Analytics, Services",
    # NOTE: the two "generic IC" excludes (Databricks / Reddit Principal DS) were
    # REMOVED 2026-07-13 — Rodrigo opened the Senior-IC (Staff/Principal DS) lane,
    # so senior individual-contributor DS roles should now surface for review.
}
# Review scores (0-10) stamped onto the discovered wedge-fits we're keeping.
DISCOVERED_SCORES = {
    "Instacart|Senior Director, Media Analytics, Commercial Strategy & Acceleration": 8.6,
    "Reddit|Senior Data Science Manager, Marketing": 8.2,
    "SoFi|Data Science Manager, Borrow": 7.9,
    "Airbnb|Senior Manager, Advanced Analytics": 7.6,
    "Airbnb|Lead, Advanced Analytics, Product": 7.4,
    "Instacart|Media Analytics Manager, Measurement & Attribution": 7.3,
    "Anthropic|Lead Data Scientist, Platform Product": 7.2,
    "Airbnb|Lead, Advanced Analytics, Payments": 7.0,
    "Instacart|Ads AI Analytics Lead II": 7.0,
    "Lyft|Data Science Manager, Machine Learning — Lyft Ads": 7.0,
    "Lyft|Data Science Manager, Machine Learning – Lyft Ads": 7.0,
    "Lyft|Data Science Manager, Machine Learning - Lyft Ads": 7.0,
}


def _off_wedge(tl):
    """True if a (lowercased) title is off Rodrigo's analytics/DS-leadership wedge."""
    if any(h in tl for h in HARD_NEGATIVE):
        return True
    if any(s in tl for s in SOFT_NEGATIVE) and not any(p in tl for p in PROTECT):
        return True
    return False


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
            # --- classify into one of the three qualifying lanes ---------------
            is_ds_lead = (any(s in tl for s in DATA_SIGNAL)
                          and any(s in tl for s in LEAD_SIGNAL)
                          and not _off_wedge(tl))   # DS/analytics leadership or Sr-IC
            is_pm = (any(p in tl for p in PM_TITLE)
                     and any(w in tl for w in PM_WEDGE)
                     and any(s in tl for s in PM_SENIOR)
                     and not any(h in tl for h in HARD_NEGATIVE))  # data/AI/growth Sr PM
            if not (is_ds_lead or is_pm):
                continue
            if not _loc_ok(ll):
                continue
            if added >= MAX_NEW_PER_COMPANY:
                print(f"  NOTE discover {token}: capped at {MAX_NEW_PER_COMPANY}, more may exist")
                break
            kind = "pm" if (is_pm and not is_ds_lead) else "ds"
            found.append({
                "co": co, "role": title, "loc": loc or "See posting",
                "track": ("pm" if kind == "pm" else "new"),
                "trackLabel": ("Product (data/AI/growth)" if kind == "pm" else "New find"),
                "extraTrack": "",
                "score": None, "domain": None,
                "comp": "Not posted", "compFit": None, "tier": "",
                "url": j.get("absolute_url", ""),
                "posted": (j.get("first_published") or "")[:10],
                "note": (f"Auto-discovered on {co}'s job board {TODAY}. "
                         + ("Senior PM (data/AI/growth lane). " if kind == "pm" else "")
                         + "Not yet scored — review fit, comp, and referral path."),
                "verify": {"type": "greenhouse", "token": token, "id": jid},
                "status": {"live": True, "v": f"Board API · discovered {TODAY}"},
                "referral": {"level": "pending", "text": ""},
                "discovered": True, "isNew": True, "kind": kind,
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

    # Discover across (a) every company already tracked via a Greenhouse role, plus
    # (b) the curated bullseye board list. Existing labels win on token collisions.
    token_to_co = {**DISCOVER_TOKENS,
                   **{r["verify"]["token"]: r["co"] for r in roles
                      if r.get("verify", {}).get("type") == "greenhouse"}}

    kept, pruned = [], []
    for role in roles:
        live, v = verify_one(role)
        role["status"] = {"live": live, "v": v}
        p = posted_for(role.get("verify"))   # stamp posting date when available
        if p:
            role["posted"] = p
        # Curate auto-discovered finds to Rodrigo's wedge (never pinned apps / base set):
        # drop off-wedge titles; stamp review scores onto the keepers.
        if role.get("discovered") and not role.get("pinned"):
            key = role["co"] + "|" + role["role"]
            # PM finds are curated at discovery time (their own 3-part test); do NOT
            # run the DS off-wedge filter on them — "product manager" is a SOFT_NEGATIVE
            # there and would wrongly drop every PM role.
            if role.get("kind") != "pm":
                if key in EXCLUDE_KEYS or _off_wedge(role["role"].lower()):
                    pruned.append(role)
                    print(f"  DROP  {key} (off-wedge — curated out)")
                    continue
            if key in DISCOVERED_SCORES:
                role["score"] = DISCOVERED_SCORES[key]
                role["isNew"] = False
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
