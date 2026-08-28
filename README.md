# FPL Draft League Dashboard

A static, self-updating dashboard for your **FPL Draft** league — standings,
head-to-head fixtures/results by gameweek, form, and a points-progression
chart. It runs entirely on GitHub: a scheduled Action pulls fresh data from
the public FPL Draft API and commits it, and GitHub Pages serves the page.

Configured out of the box for **League ID 6038**, spotlighting **Entry ID 27110**.

## 1. Push this to GitHub

Create a new repo and push these files (or use "Upload files" in the GitHub UI).

## 2. Turn on GitHub Pages

- Repo → **Settings → Pages**
- Under "Build and deployment", set **Source: Deploy from a branch**
- Branch: `main`, folder: `/ (root)` → **Save**
- Your dashboard will appear at `https://<your-username>.github.io/<repo-name>/`
  after a minute or two.

## 3. Turn on Actions (if needed)

- Repo → **Actions** tab → if prompted, click "I understand my workflows, go ahead and enable them"
- The workflow at `.github/workflows/update-data.yml` runs every 2 hours and
  can also be triggered manually from the Actions tab ("Run workflow").
- It needs permission to push commits back to the repo. This is already
  granted via `permissions: contents: write` in the workflow file, so no
  extra settings changes should be required. If your commit step ever fails
  with a permissions error, check **Settings → Actions → General → Workflow
  permissions** and set it to "Read and write permissions".

## 4. Point it at a different league or entry

Edit the `env:` block near the top of
`.github/workflows/update-data.yml`:

```yaml
env:
  LEAGUE_ID: "6038"
  ENTRY_ID: "27110"
```

`LEAGUE_ID` is required. `ENTRY_ID` is optional — it just highlights that
team as "Your team" on the dashboard. Commit the change, then run the
workflow once manually (Actions tab → "Update league data" → "Run workflow")
to refresh `data/league.json` immediately rather than waiting for the next
scheduled run.

## How it works

- `scripts/fetch_data.py` calls the public, unauthenticated FPL Draft API
  (`draft.premierleague.com/api/...`) for your league's details, standings,
  and match history, plus `bootstrap-static` for gameweek info. It shapes
  everything into one file: `data/league.json`.
- `index.html` is a plain HTML/CSS/JS page (Chart.js loaded from a CDN for
  the chart) that fetches `data/league.json` and renders the dashboard. There
  is no build step and no server-side code — it's just static files, which is
  why GitHub Pages can host it directly.
- The GitHub Action re-runs the script on a schedule and commits the updated
  JSON, so the live site stays current without you doing anything.

## Running / previewing locally

Because `index.html` fetches `data/league.json` with `fetch()`, opening the
file directly (`file://...`) will be blocked by the browser's CORS rules for
local files. Serve the folder instead, e.g.:

```bash
python -m http.server 8000
# then open http://localhost:8000
```

To pull fresh data locally (optional — the committed `data/league.json` is
sample data until the Action runs once):

```bash
pip install -r scripts/requirements.txt
LEAGUE_ID=6038 ENTRY_ID=27110 python scripts/fetch_data.py
```

## Notes

- This uses the same public endpoints the official draft.premierleague.com
  site's front end uses — no login or API key needed, but also no official
  support: if the Premier League changes its API shape, the fetch script may
  need small updates. If a run ever fails, check the Action's logs (Actions
  tab → the failed run) — the script prints what it fetched and where it
  stopped.
- This project is unofficial and isn't affiliated with the Premier League or
  the FPL Draft game.
