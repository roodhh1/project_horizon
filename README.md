# Project Horizon — Job Finder

A self-maintaining dashboard of Data Science / Analytics leadership roles, scored
for fit, comp, and mobility, with a 1st/2nd-degree referral map per company. It
shows **only roles that are currently live** — closed postings are removed, and
newly opened ones are added, automatically every day.

**Live dashboard:** https://roodhh1.github.io/project_horizon/

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
