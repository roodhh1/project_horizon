# Beacon

A navigable job-search app for Data Science / Analytics leadership roles — scored
for fit, comp, and mobility, with a 1st/2nd-degree referral map per company. It
shows **only roles that are currently live** — closed postings are removed, and
newly opened ones are added, automatically every day.

**Live app:** https://roodhh1.github.io/project_horizon/

## How fit scores work

Each live role carries a `fit` block and a composite **fit score (0–10)**. The score is
a weighted blend of five sub-scores, each 0–10, judged against Rodrigo's profile
(~11 yrs DS, ~5 yrs people-management; wedge: product analytics, monetization/pricing,
consumer lending, growth, causal inference / experimentation):

| Sub-score | Weight | What it measures |
|-----------|:------:|------------------|
| **Relevance** | 35% | How closely the role's *actual* focus (read from the JD) matches the wedge |
| **Level** | 20% | Scope fit vs his Senior-Manager level — step-up Director/Head and lateral Manager rank high; IC "Lead"/Staff rank low |
| **Experience** | 20% | Match of his years **and kind** of experience to the JD's requirements — penalizes domain-specific gaps (fraud-management, ML-engineering, people-analytics) even when total years exceed |
| **Company** | 10% | Brand, stage, trajectory, comp ceiling |
| **Comp** | 15% | Relative to his (confidential) target — at / borderline / below |

`composite = 0.35·Relevance + 0.20·Level + 0.20·Experience + 0.10·Company + 0.15·Comp`

Each role also has a **confidence** flag: `high` (JD fetched and parsed, e.g. via the
Greenhouse content API), `med` (prior JD-informed detail), or `low` (JD auth-gated, e.g.
LinkedIn — scored from title/company pending a manual read). The breakdown and the
JD's required years vs. Rodrigo's experience are shown on every card.

Scores are a decision aid, not a verdict — they make the *why* explicit so a low
Relevance or an Experience-gap is visible at a glance.

## Using the app

Beacon is a single-page app with hash-based navigation (works on GitHub Pages):

- **Home** — a hub with stat tiles and tap-in cards to each section, plus the top 3 matches.
- **Roles** — a compact, ranked list. Toggle **Top matches** (score ≥ 7) vs **All roles**,
  filter by track, or search. Tap any role to open its **detail view** (score ring, fit
  breakdown, experience match, compensation, referral path, apply / engage).
- **Radar** — roles you've engaged with.
- **Network** — warm 1st/2nd-degree paths into each company.
- **About** — how the scoring and daily refresh work.

On a role's detail view, **Mark engaged** moves it to your Radar. Engaged state is saved
in your browser (`localStorage`) — it persists across visits and the daily data refresh,
is per-device, and never leaves your machine.

## Data model

All role data lives in **`roles.json`** — the single source of truth
(`{"verified_on", "roles":[...]}`). The dashboard (`index.html`) fetches it and
renders it; it holds no role data of its own.

## How the daily refresh works

Job postings get pulled constantly, and cached Greenhouse permalinks keep resolving
even after a role closes — so "the link still works" is not proof a role is open.
Each morning `verify.py` does three things:

1. **Verify** every tracked role against the company's **live source of truth**:
   - **Greenhouse boards** (Mercury, Anthropic, Gusto, Chime, Brex, Coinbase,
     Affirm, DoorDash) — live only if the job ID (or a matching title) is present in
     `boards-api.greenhouse.io/v1/boards/<token>/jobs`.
   - **Self-hosted careers** (Stripe, Plaid, OpenAI) — the page must return `200`
     and not look like a 404 / closed page.
2. **Prune** — a role confirmed closed is **removed from `roles.json` entirely**, so
   the dashboard only ever holds what is truly live. A live→closed flip is debounced
   (held one run as "unconfirmed") so a transient network blip or anti-bot page never
   drops a real role. If a check errors, the previous status is **kept** — a role is
   never falsely dropped.
3. **Discover** — new DS / Analytics **leadership** roles found on the target
   companies' Greenhouse boards are added automatically, tagged **NEW (unscored)** so
   you can review fit, comp, and referral path. (The match filter is deliberately
   conservative — data/ML signal + leadership signal + US/remote location.)

The GitHub Actions workflow (`.github/workflows/refresh.yml`) runs this daily at
**08:00 PT** and commits the updated `roles.json`. GitHub Pages serves the result.

## The LinkedIn referral sweep (assisted, not automated)

Finding roles via LinkedIn and refreshing the 1st/2nd-degree referral map require a
logged-in LinkedIn session. That is **not** automated here on purpose: headless
LinkedIn login from CI violates LinkedIn's User Agreement (account-ban risk), trips
CAPTCHAs / 2FA, and would mean storing credentials in CI. Instead these sweeps are
run **assisted and on demand** — driven through your own already-logged-in browser
when you ask for one — and the results are written back into `roles.json`.

## Run it yourself

```bash
python3 verify.py        # stdlib only, no dependencies
python3 -m http.server   # then open http://localhost:8000 (needs http, not file://)
```

Or trigger the workflow manually from the **Actions** tab → *Refresh job dashboard*
→ *Run workflow*.

## Notes

- The referral map (1st/2nd-degree LinkedIn contacts) is a network snapshot —
  contacts stay current whether or not a given posting is open.
- To add or remove a role by hand, edit `roles.json`. Each role carries its display
  fields, a `verify` block (how to check it's live), `status`, and `referral`.
- Discovery covers the Greenhouse-hosted target companies. Self-hosted careers sites
  (Stripe, Plaid, OpenAI) are verified but not auto-discovered.
