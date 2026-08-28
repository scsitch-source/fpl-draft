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

# Regular season / playoff structure. Adjust via env vars if your league's
# schedule differs (e.g. a 20-team league that finishes earlier).
REG_SEASON_END = int(os.environ.get("REG_SEASON_END", "35"))
SEMI_GW = int(os.environ.get("SEMI_GW", "36"))
ELIM_GW = int(os.environ.get("ELIM_GW", "37"))
FINAL_GW = int(os.environ.get("FINAL_GW", "38"))

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


def get_json_soft(path):
    """Like get_json but returns None (and prints a warning) instead of raising."""
    try:
        return get_json(path)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  warning: GET {path} failed ({exc})")
        return None


def build():
    print(f"Fetching league {LEAGUE_ID} details...")
    details = get_json(f"league/{LEAGUE_ID}/details")

    print("Fetching bootstrap-static (players/gameweeks)...")
    bootstrap = get_json("bootstrap-static")
    print(f"  bootstrap-static keys: {list(bootstrap.keys())}")
    _events_raw = bootstrap.get("events", [])
    print(f"  events: type={type(_events_raw).__name__}")
    if isinstance(_events_raw, dict):
        print(f"  events dict keys: {list(_events_raw.keys())}")
        print(f"  events dict sample: {json.dumps(_events_raw)[:600]}")
        # Some Draft API responses nest the real gameweek list under a key
        # like 'data' or 'events' inside this dict, rather than being the
        # list itself. Try the common possibilities; fall back to treating
        # the dict's values as the event objects.
        events = (_events_raw.get("data")
                  or _events_raw.get("events")
                  or list(_events_raw.values()))
    elif isinstance(_events_raw, list):
        events = _events_raw
        print(f"  events list len: {len(events)}")
        if events:
            print(f"  events[0] sample: {json.dumps(events[0])[:400]}")
    else:
        events = []

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
    elements = bootstrap.get("elements", [])
    element_types = bootstrap.get("element_types", [])
    teams_pl = bootstrap.get("teams", [])

    if not league_entries or not standings_raw:
        print("ERROR: league details response did not contain the expected "
              "'league_entries' / 'standings' keys.", file=sys.stderr)
        print(json.dumps(details)[:2000], file=sys.stderr)
        sys.exit(1)

    # Player lookup: element id -> name / position / real-life club
    pos_by_type = {et.get("id"): et.get("singular_name_short", "?") for et in element_types if isinstance(et, dict)}
    club_by_id = {t.get("id"): t.get("short_name", "?") for t in teams_pl if isinstance(t, dict)}
    player_by_id = {}
    for el in elements:
        if not isinstance(el, dict):
            continue
        player_by_id[el.get("id")] = {
            "name": el.get("web_name", "Unknown"),
            "position": pos_by_type.get(el.get("element_type"), "?"),
            "club": club_by_id.get(el.get("team"), "?"),
        }

    # Map league_entry id -> readable info
    entry_by_id = {}
    your_league_entry_id = None
    for e in league_entries:
        le_id = e.get("id")
        real_entry_id = e.get("entry_id")
        if real_entry_id is None:
            real_entry_id = e.get("entry")  # older/alternate field name, just in case
        entry_by_id[le_id] = {
            "league_entry_id": le_id,
            "entry_id": real_entry_id,
            "team_name": e.get("entry_name") or e.get("short_name") or "Unknown",
            "manager_name": " ".join(
                filter(None, [e.get("player_first_name"), e.get("player_last_name")])
            ).strip() or "Unknown Manager",
            "short_name": e.get("short_name"),
        }
        if ENTRY_ID and str(real_entry_id) == str(ENTRY_ID):
            your_league_entry_id = le_id

    # Current / next gameweek
    current_event = None
    next_event = None
    next_deadline = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
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
            "played": s.get("matches_played") if s.get("matches_played") is not None else (
                (s.get("matches_won", 0) or 0)
                + (s.get("matches_drawn", 0) or 0)
                + (s.get("matches_lost", 0) or 0)
            ),
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

    # ---------------- Draft recap (draft-day pick order) ----------------
    print("Fetching draft picks...")
    draft_data = get_json_soft(f"draft/{LEAGUE_ID}/choices")
    draft_picks = []
    if draft_data:
        items = draft_data if isinstance(draft_data, list) else draft_data.get("choices", [])
        for pick in items or []:
            le_id = pick.get("entry")
            info = entry_by_id.get(le_id, {})
            player_info = player_by_id.get(pick.get("element"), {})
            draft_picks.append({
                "round": pick.get("round"),
                "pick": pick.get("pick"),
                "overall_pick": pick.get("index") if pick.get("index") is not None else None,
                "team_name": pick.get("entry_name") or info.get("team_name", "Unknown"),
                "manager_name": info.get("manager_name", ""),
                "player_name": player_info.get("name", f"Player {pick.get('element')}"),
                "position": player_info.get("position", "?"),
                "club": player_info.get("club", "?"),
            })
        # Sort into draft order: by round then pick-within-round if available,
        # falling back to whatever order the API returned.
        draft_picks.sort(key=lambda p: (
            p["round"] if p["round"] is not None else 0,
            p["pick"] if p["pick"] is not None else 0,
        ))
    else:
        print("  note: could not fetch draft choices; Draft Recap tab will be empty.")

    # ---------------- Playoff bracket (top 4 after reg season) ----------------
    def find_match(gw, le_a, le_b):
        if le_a is None or le_b is None:
            return None
        for f in fixtures_by_event.get(str(gw), []):
            ids = {f["home"]["league_entry_id"], f["away"]["league_entry_id"]}
            if ids == {le_a, le_b}:
                return f
        return None

    def winner_of(f):
        if not f or not f["finished"] or f["home"]["score"] is None or f["away"]["score"] is None:
            return None
        if f["home"]["score"] == f["away"]["score"]:
            return None
        return f["home"]["league_entry_id"] if f["home"]["score"] > f["away"]["score"] else f["away"]["league_entry_id"]

    def loser_of(f):
        w = winner_of(f)
        if w is None or not f:
            return None
        return f["away"]["league_entry_id"] if w == f["home"]["league_entry_id"] else f["home"]["league_entry_id"]

    def team_ref(le_id):
        if le_id is None:
            return None
        info = entry_by_id.get(le_id, {})
        return {"league_entry_id": le_id, "team_name": info.get("team_name", "TBD"),
                "manager_name": info.get("manager_name", "")}

    playoffs = None
    top4 = [s["league_entry_id"] for s in standings[:4]]
    if len(top4) == 4:
        semi_a = find_match(SEMI_GW, top4[0], top4[1])
        semi_b = find_match(SEMI_GW, top4[2], top4[3])
        semi_a_winner, semi_a_loser = winner_of(semi_a), loser_of(semi_a)
        semi_b_winner = winner_of(semi_b)
        elim = find_match(ELIM_GW, semi_a_loser, semi_b_winner)
        elim_winner = winner_of(elim)
        final = find_match(FINAL_GW, semi_a_winner, elim_winner)
        final_winner = winner_of(final)
        playoffs = {
            "semi_gw": SEMI_GW, "elim_gw": ELIM_GW, "final_gw": FINAL_GW,
            "seeds": [team_ref(le) for le in top4],
            "semi_a": {"fixture": semi_a, "team_a": team_ref(top4[0]), "team_b": team_ref(top4[1])},
            "semi_b": {"fixture": semi_b, "team_a": team_ref(top4[2]), "team_b": team_ref(top4[3])},
            "semi_a_winner": team_ref(semi_a_winner),
            "semi_a_bye_to_final": team_ref(semi_a_winner) if semi_a_winner else None,
            "elim": {"fixture": elim, "team_a": team_ref(semi_a_loser), "team_b": team_ref(semi_b_winner)},
            "elim_winner": team_ref(elim_winner),
            "final": {"fixture": final, "team_a": team_ref(semi_a_winner), "team_b": team_ref(elim_winner)},
            "final_winner": team_ref(final_winner),
        }

    # ---------------- Golden gameweek (best single score, GW1..REG_SEASON_END) ----------------
    best_per_entry = {}
    for le_id, scores in weekly_scores.items():
        for ev, pts in scores.items():
            if ev is None or ev > REG_SEASON_END or pts is None:
                continue
            if le_id not in best_per_entry or pts > best_per_entry[le_id]["points"]:
                best_per_entry[le_id] = {"league_entry_id": le_id, "points": pts, "event": ev}

    golden_leaderboard = sorted(best_per_entry.values(), key=lambda r: r["points"], reverse=True)[:8]
    for row in golden_leaderboard:
        info = entry_by_id.get(row["league_entry_id"], {})
        row["team_name"] = info.get("team_name", "Unknown")
        row["manager_name"] = info.get("manager_name", "")
    golden_gameweek = {
        "reg_season_end": REG_SEASON_END,
        "leader": golden_leaderboard[0] if golden_leaderboard else None,
        "leaderboard": golden_leaderboard,
    }

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
        "playoffs": playoffs,
        "golden_gameweek": golden_gameweek,
        "draft_picks": draft_picks,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/league.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data/league.json ({len(standings)} teams, "
          f"{len(finished_events)} finished gameweeks).")


if __name__ == "__main__":
    build()
