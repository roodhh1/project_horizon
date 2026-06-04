# Project Horizon — Job Finder

A self-updating dashboard of Data Science / Analytics leadership roles, scored for
fit, comp, and mobility, with a live-vs-closed status for every posting and a
1st/2nd-degree referral map per company.

**Live dashboard:** https://roodhh1.github.io/project_horizon/

## How the daily refresh works

Job postings get pulled constantly, and cached Greenhouse permalinks keep resolving
even after a role closes — so "the link still works" is not proof a role is open.

`verify.py` checks each tracked role against the company's **live source of truth**:

- **Greenhouse boards** (Mercury, Anthropic, Gusto, Chime, Brex) — a role is live only
  if its job ID is present in `boards-api.greenhouse.io/v1/boards/<token>/jobs`.
- **Self-hosted careers** (Stripe, Plaid) — the page must return `200` and not look
  like a 404 / closed page.

It then rewrites the `STATUS{}` block and the `VERIFIED_ON` date inside
`index.html`, between the `// STATUS_BLOCK_START` / `// STATUS_BLOCK_END` markers.
If a check errors (transient network blip), the previous status is **kept** — a role
is never falsely flipped to "closed".

The GitHub Actions workflow (`.github/workflows/refresh.yml`) runs this daily
(08:00 PT) and commits any changes. GitHub Pages serves the result.

The dashboard shows **only roles that are currently live** — once `verify.py`
marks a posting closed, it drops off the page on the next refresh.

## Run it yourself

```bash
python3 verify.py        # stdlib only, no dependencies
```

Or trigger the workflow manually from the **Actions** tab → *Refresh job dashboard*
→ *Run workflow*.

## Notes

- The referral map (1st/2nd-degree LinkedIn contacts) is a static network snapshot —
  contacts stay current whether or not a given posting is open.
- To add or remove a tracked role, edit the `TRACKED` list in `verify.py` and the
  `ROLES` array in `index.html`.
