#!/usr/bin/env python3
"""
Pulls data from the official (unofficial-but-public) FPL Draft API and writes
a single consolidated JSON file that the static dashboard (index.html) reads.

Env vars (with sensible defaults matching this league):
    LEAGUE_ID   - FPL Draft league id, e.g. 6038
    ENTRY_ID    - Your FPL Draft entry (team) id, e.g. 27110, used to
                  highlight "your team" on the dashboard. Optional.

Nothing here requires authentication - the FPL Draft API's league/entry/
bootstrap endpoints are public read-only endpoints.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

BASE = "https://draft.premierleague.com/api"
LEAGUE_ID = os.environ.get("LEAGUE_ID", "6038")
ENTRY_ID = os.environ.get("ENTRY_ID", "27110")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "fpl-draft-dashboard/1.0 (+github actions)"})


def get_json(path):
    url = f"{BASE}/{path}"
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def build():
    print(f"Fetching league {LEAGUE_ID} details...")
    details = get_json(f"league/{LEAGUE_ID}/details")

    print("Fetching bootstrap-static (players/gameweeks)...")
    bootstrap = get_json("bootstrap-static")

    print("Fetching game state...")
    try:
        game = get_json("game")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  warning: could not fetch /game ({exc})")
        game = {}

    league = details.get("league", {})
    league_entries = details.get("league_entries", [])
    standings_raw = details.get("standings", [])
    matches_raw = details.get("matches", [])
    events = bootstrap.get("events", [])

    if not league_entries or not standings_raw:
        print("ERROR: league details response did not contain the expected "
              "'league_entries' / 'standings' keys.", file=sys.stderr)
        print(json.dumps(details)[:2000], file=sys.stderr)
        sys.exit(1)

    # Map league_entry id -> readable info
    entry_by_id = {}
    your_league_entry_id = None
    for e in league_entries:
        le_id = e.get("id")
        entry_by_id[le_id] = {
            "league_entry_id": le_id,
            "entry_id": e.get("entry"),
            "team_name": e.get("entry_name") or e.get("short_name") or "Unknown",
            "manager_name": " ".join(
                filter(None, [e.get("player_first_name"), e.get("player_last_name")])
            ).strip() or "Unknown Manager",
            "short_name": e.get("short_name"),
        }
        if ENTRY_ID and str(e.get("entry")) == str(ENTRY_ID):
            your_league_entry_id = le_id

    # Current / next gameweek
    current_event = None
    next_event = None
    next_deadline = None
    for ev in events:
        if ev.get("is_current"):
            current_event = ev.get("id")
        if ev.get("is_next"):
            next_event = ev.get("id")
            next_deadline = ev.get("deadline_time")
    if current_event is None:
        current_event = game.get("current_event")
    if next_event is None:
        next_event = game.get("next_event")

    # Per-entry weekly scores + form, derived from finished matches
    weekly_scores = {le_id: {} for le_id in entry_by_id}
    match_results = {le_id: [] for le_id in entry_by_id}  # list of (event, result) W/D/L
    fixtures_by_event = {}

    for m in matches_raw:
        event = m.get("event")
        le1, le2 = m.get("league_entry_1"), m.get("league_entry_2")
        p1, p2 = m.get("league_entry_1_points"), m.get("league_entry_2_points")
        finished = bool(m.get("finished"))

        if le1 in entry_by_id and p1 is not None:
            weekly_scores[le1][event] = p1
        if le2 in entry_by_id and p2 is not None:
            weekly_scores[le2][event] = p2

        if finished and le1 in entry_by_id and le2 in entry_by_id:
            if p1 is not None and p2 is not None:
                if p1 > p2:
                    match_results[le1].append((event, "W"))
                    match_results[le2].append((event, "L"))
                elif p1 < p2:
                    match_results[le1].append((event, "L"))
                    match_results[le2].append((event, "W"))
                else:
                    match_results[le1].append((event, "D"))
                    match_results[le2].append((event, "D"))

        fixtures_by_event.setdefault(str(event), []).append({
            "home": {
                "league_entry_id": le1,
                "team_name": safe_get(entry_by_id, le1, "team_name", default="TBD"),
                "manager_name": safe_get(entry_by_id, le1, "manager_name", default=""),
                "score": p1,
            },
            "away": {
                "league_entry_id": le2,
                "team_name": safe_get(entry_by_id, le2, "team_name", default="TBD"),
                "manager_name": safe_get(entry_by_id, le2, "manager_name", default=""),
                "score": p2,
            },
            "finished": finished,
            "started": bool(m.get("started")),
            "winning_league_entry": m.get("winning_league_entry"),
        })

    # Standings, enriched
    standings = []
    for s in standings_raw:
        le_id = s.get("league_entry")
        info = entry_by_id.get(le_id, {})
        form_sorted = sorted(match_results.get(le_id, []), key=lambda t: t[0])
        form_last5 = [r for _, r in form_sorted[-5:]]
        standings.append({
            "rank": s.get("rank"),
            "last_rank": s.get("last_rank"),
            "league_entry_id": le_id,
            "entry_id": info.get("entry_id"),
            "team_name": info.get("team_name", "Unknown"),
            "manager_name": info.get("manager_name", "Unknown Manager"),
            "played": (s.get("matches_won", 0) or 0)
                      + (s.get("matches_drawn", 0) or 0)
                      + (s.get("matches_lost", 0) or 0),
            "won": s.get("matches_won", 0),
            "drawn": s.get("matches_drawn", 0),
            "lost": s.get("matches_lost", 0),
            "points_for": s.get("points_for", 0),
            "points_against": s.get("points_against", 0),
            "total": s.get("total", 0),
            "event_total": s.get("event_total", 0),
            "form": form_last5,
            "is_you": le_id == your_league_entry_id,
        })

    standings.sort(key=lambda r: (r["rank"] is None, r["rank"] if r["rank"] is not None else 0))

    # Progression chart data: cumulative points per finished gameweek, per team
    finished_events = sorted({
        int(ev) for ev, fixtures in fixtures_by_event.items()
        for f in fixtures if f["finished"]
    })
    progression_teams = {}
    for le_id, info in entry_by_id.items():
        cumulative = []
        running = 0
        weekly = []
        for ev in finished_events:
            pts = weekly_scores.get(le_id, {}).get(ev)
            pts = pts if pts is not None else 0
            running += pts
            cumulative.append(running)
            weekly.append(pts)
        progression_teams[str(le_id)] = {
            "name": info["team_name"],
            "weekly": weekly,
            "cumulative": cumulative,
        }

    # Your team spotlight: find next fixture (current or next event)
    your_next_fixture = None
    if your_league_entry_id is not None:
        for probe_event in [current_event, next_event]:
            if probe_event is None:
                continue
            for f in fixtures_by_event.get(str(probe_event), []):
                if your_league_entry_id in (f["home"]["league_entry_id"], f["away"]["league_entry_id"]):
                    your_next_fixture = {"event": probe_event, **f}
                    break
            if your_next_fixture:
                break

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league": {
            "id": league.get("id", LEAGUE_ID),
            "name": league.get("name", "FPL Draft League"),
            "scoring": league.get("scoring"),
            "draft_status": league.get("draft_status"),
        },
        "current_event": current_event,
        "next_event": next_event,
        "next_deadline": next_deadline,
        "your_entry_id": int(ENTRY_ID) if ENTRY_ID else None,
        "your_league_entry_id": your_league_entry_id,
        "your_next_fixture": your_next_fixture,
        "standings": standings,
        "fixtures_by_event": fixtures_by_event,
        "progression": {
            "events": finished_events,
            "teams": progression_teams,
        },
    }

    os.makedirs("data", exist_ok=True)
    with open("data/league.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data/league.json ({len(standings)} teams, "
          f"{len(finished_events)} finished gameweeks).")


if __name__ == "__main__":
    build()
