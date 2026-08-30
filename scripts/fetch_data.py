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


def load_previous_output():
    """Load the JSON we wrote last run, if any, so we can reuse cached squad
    data for gameweeks that are already finished (their picks/stats never
    change once a gameweek is done, so there's no need to re-fetch them)."""
    try:
        with open("data/league.json") as f:
            return json.load(f)
    except Exception:
        return {}


# Standard FPL formation limits, used to keep auto-subs valid.
MIN_BY_POS = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}


def apply_autosubs(starting, bench):
    """Bench players with minutes>0 replace starting players with 0 minutes
    IN A FINISHED MATCH (never sub someone out just because their match
    hasn't kicked off yet), without breaking formation minimums
    (>=3 DEF, >=2 MID, >=1 FWD, =1 GKP). Captaincy passes to the
    vice-captain if the captain didn't end up playing."""
    starting = [dict(p) for p in starting]
    bench = [dict(p) for p in bench]

    counts = {"GKP": 0, "DEF": 0, "MID": 0, "FWD": 0}
    for p in starting:
        counts[p["position"]] = counts.get(p["position"], 0) + 1

    for p in starting:
        p["subbed_out"] = False
    for p in bench:
        p["subbed_in"] = False

    for starter in starting:
        if starter["minutes"] > 0:
            continue  # played, no sub needed
        if not starter.get("match_finished"):
            continue  # hasn't kicked off / still in progress - too early to sub
        if starter.get("subbed_out"):
            continue

        # Goalkeepers can only be replaced by the bench goalkeeper.
        candidates = [b for b in bench if not b["subbed_in"] and b["minutes"] > 0
                      and (b["position"] == "GKP") == (starter["position"] == "GKP")]

        chosen = None
        for cand in candidates:
            new_counts = dict(counts)
            new_counts[starter["position"]] -= 1
            new_counts[cand["position"]] = new_counts.get(cand["position"], 0) + 1
            if all(new_counts.get(pos, 0) >= minimum for pos, minimum in MIN_BY_POS.items()):
                chosen = cand
                break

        if chosen:
            counts[starter["position"]] -= 1
            counts[chosen["position"]] = counts.get(chosen["position"], 0) + 1
            starter["subbed_out"] = True
            chosen["subbed_in"] = True

    final_starting = [p for p in starting if not p["subbed_out"]] + [b for b in bench if b["subbed_in"]]
    final_bench = [p for p in starting if p["subbed_out"]] + [b for b in bench if not b["subbed_in"]]

    # Captaincy: only passes to the vice-captain once the captain's own match
    # has finished with them not playing - not just because they haven't
    # kicked off yet.
    captain_row = next((p for p in final_starting if p["is_captain"]), None)
    captain_confirmed_out = captain_row and captain_row["minutes"] == 0 and captain_row.get("match_finished")
    captain_in_final = captain_row if (captain_row and not captain_confirmed_out) else None
    if not captain_in_final:
        vc = next((p for p in final_starting if p["is_vice_captain"] and p["minutes"] > 0), None)
        if vc:
            vc["effective_captain"] = True

    for p in final_starting:
        is_effective_captain = (p["is_captain"] and p is captain_in_final) or p.get("effective_captain")
        p["multiplier"] = 2 if is_effective_captain else 1
        p["points"] = p["base_points"] * p["multiplier"]

    return final_starting, final_bench


# Defensive Contribution: DEF need combined defensive actions >= 10,
# MID/FWD need >= 12. The raw count comes from the live-stats endpoint;
# we apply the position threshold ourselves rather than trusting a
# pre-computed flag, since that field's exact meaning isn't documented.
DC_THRESHOLDS = {"DEF": 10, "MID": 12, "FWD": 12}


def meets_dc_threshold(raw_count, position):
    threshold = DC_THRESHOLDS.get(position)
    if threshold is None:
        return False
    return (raw_count or 0) >= threshold


# Fallback shirt colours if a player photo fails to load, keyed by club
# short_name. Covers recent Premier League clubs; anything unlisted (e.g. a
# newly promoted club) falls back to a neutral grey.
CLUB_SHIRT_COLORS = {
    "ARS": "#EF0107", "AVL": "#670E36", "BOU": "#DA020E", "BRE": "#FFDB00",
    "BHA": "#0057B8", "BUR": "#6C1D45", "CHE": "#034694", "CRY": "#1B458F",
    "EVE": "#003399", "FUL": "#000000", "IPS": "#3A64A3", "LEE": "#FFCD00",
    "LEI": "#003090", "LIV": "#C8102E", "LUT": "#F78F1E", "MCI": "#6CABDD",
    "MUN": "#DA291C", "NEW": "#241F20", "NFO": "#DD0000", "SHU": "#EE2737",
    "SOU": "#D71920", "SUN": "#EB172B", "TOT": "#132257", "WAT": "#FBEE23",
    "WBA": "#122F67", "WHU": "#7A263A", "WOL": "#FDB913",
}
DEFAULT_SHIRT_COLOR = "#6B7280"
PLAYER_PHOTO_BASE = "https://resources.premierleague.com/premierleague25/photos/players/110x140"


def player_initials(name):
    parts = [p for p in name.replace("-", " ").split(" ") if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


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
    if matches_raw:
        print(f"  matches sample: {json.dumps(matches_raw[0])[:400]}")
    else:
        print("  note: 'matches' was empty in league/details response "
              "(check league.scoring - h2h fixtures only exist for h2h-scored leagues).")
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
        club_short = club_by_id.get(el.get("team"), "?")
        code = el.get("code")
        player_by_id[el.get("id")] = {
            "name": el.get("web_name", "Unknown"),
            "position": pos_by_type.get(el.get("element_type"), "?"),
            "club": club_short,
            "club_id": el.get("team"),
            "season_points": el.get("total_points", 0),
            "photo_url": f"{PLAYER_PHOTO_BASE}/{code}.png" if code else None,
            "shirt_color": CLUB_SHIRT_COLORS.get(club_short, DEFAULT_SHIRT_COLOR),
            "initials": player_initials(el.get("web_name", "?")),
        }

    # Per-gameweek, per-club "has this club's fixture(s) finished" lookup.
    # Used to gate auto-subs: a player only gets auto-subbed once their own
    # match has actually finished, not just because they show 0 minutes
    # while the match simply hasn't kicked off yet.
    fixtures_raw = bootstrap.get("fixtures", [])
    club_finished_by_event = {}
    club_opponent_by_event = {}
    if isinstance(fixtures_raw, list):
        for fx in fixtures_raw:
            if not isinstance(fx, dict):
                continue
            ev = fx.get("event")
            if ev is None:
                continue
            finished = bool(fx.get("finished"))
            bucket = club_finished_by_event.setdefault(ev, {})
            opp_bucket = club_opponent_by_event.setdefault(ev, {})
            home_id, away_id = fx.get("team_h"), fx.get("team_a")
            for club_id in (home_id, away_id):
                if club_id is None:
                    continue
                # If a club has multiple fixtures in one event (rare, DGW),
                # it only counts as "finished" once ALL of them are.
                bucket[club_id] = finished and bucket.get(club_id, True)
            if home_id is not None and away_id is not None:
                opp_bucket.setdefault(home_id, []).append({
                    "opponent": club_by_id.get(away_id, "?"), "is_home": True,
                })
                opp_bucket.setdefault(away_id, []).append({
                    "opponent": club_by_id.get(home_id, "?"), "is_home": False,
                })

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
    current_event_finished = False
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("is_current"):
            current_event = ev.get("id")
            current_event_finished = bool(ev.get("finished"))
        if ev.get("is_next"):
            next_event = ev.get("id")
            next_deadline = ev.get("deadline_time")
    if current_event is None:
        current_event = game.get("current_event")
        current_event_finished = bool(game.get("current_event_finished"))
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
            "current_gw_score": weekly_scores.get(le_id, {}).get(current_event),
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

    # ---------------- Per-player squads for each fixture (click-to-expand) ----------------
    # One call per team per gameweek is the priciest part of this script, so we
    # cache aggressively: any gameweek that finished last run (its picks/stats
    # never change) is reused rather than re-fetched.
    previous = load_previous_output()
    previous_fixtures = previous.get("fixtures_by_event", {}) if isinstance(previous, dict) else {}
    live_points_cache = {}

    _live_debug_printed = set()

    def live_stats_for_event(ev):
        """{element_id: {'points': int, 'goals': int, 'assists': int, 'minutes': int}} for one gameweek."""
        if ev in live_points_cache:
            return live_points_cache[ev]
        data = get_json_soft(f"event/{ev}/live")
        out = {}
        if not data:
            live_points_cache[ev] = out
            return out

        elements = data.get("elements", data)
        if ev not in _live_debug_printed:
            _live_debug_printed.add(ev)
            print(f"  event/{ev}/live: top-level keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}, "
                  f"elements type={type(elements).__name__}")
            # One full raw stats blob, to confirm field names like
            # 'defensive_contribution', 'clean_sheets', 'tackles', etc.
            sample_stats = None
            if isinstance(elements, dict):
                for el in elements.values():
                    if isinstance(el, dict):
                        sample_stats = el.get("stats", el)
                        break
            elif isinstance(elements, list) and elements:
                sample_stats = elements[0].get("stats", elements[0])
            if sample_stats:
                print(f"  sample raw stats blob: {json.dumps(sample_stats)[:500]}")

        def _row(stats):
            return {
                "points": stats.get("total_points", 0),
                "goals": stats.get("goals_scored", 0),
                "assists": stats.get("assists", 0),
                "minutes": stats.get("minutes", 0),
                "clean_sheets": stats.get("clean_sheets", 0),
                "defensive_contribution": stats.get("defensive_contribution", 0),
                "bonus": stats.get("bonus", 0),
                "yellow_cards": stats.get("yellow_cards", 0),
                "red_cards": stats.get("red_cards", 0),
            }

        if isinstance(elements, dict):
            for key, el in elements.items():
                if not isinstance(el, dict):
                    continue
                stats = el.get("stats", el)
                try:
                    el_id = int(key)
                except (TypeError, ValueError):
                    el_id = el.get("id") or el.get("element")
                if el_id is not None:
                    out[el_id] = _row(stats)
        elif isinstance(elements, list):
            for el in elements:
                if not isinstance(el, dict):
                    continue
                el_id = el.get("id") or el.get("element")
                stats = el.get("stats", el)
                if el_id is not None:
                    out[el_id] = _row(stats)
        live_points_cache[ev] = out
        return out

    _picks_debug_printed = set()

    def build_squad(real_entry_id, ev, live_stats):
        data = get_json_soft(f"entry/{real_entry_id}/event/{ev}")
        if not data:
            return None
        if ev not in _picks_debug_printed:
            _picks_debug_printed.add(ev)
            print(f"  entry/{real_entry_id}/event/{ev}: top-level keys={list(data.keys())}")
            picks_preview = data.get("picks")
            if isinstance(picks_preview, list) and picks_preview:
                print(f"  first pick sample: {json.dumps(picks_preview[0])[:300]}")
        if not isinstance(data.get("picks"), list):
            return None

        raw_starting, raw_bench = [], []
        for pick in data["picks"]:
            el_id = pick.get("element")
            info = player_by_id.get(el_id, {"name": f"Player {el_id}", "position": "?", "club": "?"})
            multiplier = pick.get("multiplier", 1) or 0
            # 'position' on a pick is the squad SLOT (1-15: 1-11 starting XI
            # in formation order, 12-15 bench) - not the player's playing
            # position (GKP/DEF/MID/FWD, which comes from player_by_id above).
            # This is the authoritative starting/bench signal; multiplier is
            # only used as a fallback if the slot field isn't present.
            slot = pick.get("position")
            if isinstance(slot, int):
                is_starting = slot <= 11
            else:
                is_starting = multiplier > 0
            stat = live_stats.get(el_id, {"points": 0, "goals": 0, "assists": 0, "minutes": 0,
                                          "clean_sheets": 0, "defensive_contribution": 0,
                                          "bonus": 0, "yellow_cards": 0, "red_cards": 0})
            row = {
                "name": info["name"],
                "position": info["position"],
                "club": info["club"],
                "photo_url": info.get("photo_url"),
                "shirt_color": info.get("shirt_color", "#6B7280"),
                "initials": info.get("initials", "?"),
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
                "base_points": stat["points"],
                "goals": stat["goals"],
                "assists": stat["assists"],
                "minutes": stat["minutes"],
                "match_finished": club_finished_by_event.get(ev, {}).get(info.get("club_id"), False),
                "clean_sheet": bool(stat.get("clean_sheets")) and info["position"] in ("GKP", "DEF"),
                "defensive_contribution": meets_dc_threshold(stat.get("defensive_contribution"), info["position"]),
                "opponents": club_opponent_by_event.get(ev, {}).get(info.get("club_id"), []),
                "bonus": stat.get("bonus", 0) or 0,
                "yellow_card": bool(stat.get("yellow_cards")),
                "red_card": bool(stat.get("red_cards")),
            }
            (raw_starting if is_starting else raw_bench).append(row)

        final_starting, final_bench = apply_autosubs(raw_starting, raw_bench)
        # Anyone left on the bench (not subbed in) just shows their raw points, unmultiplied.
        for p in final_bench:
            p.setdefault("multiplier", 1)
            p.setdefault("points", p["base_points"])

        played_count = sum(1 for p in final_starting if p["minutes"] > 0)
        return {
            "starting": final_starting,
            "bench": final_bench,
            "played_count": played_count,
            "squad_size": len(final_starting),
        }

    def squad_looks_valid(squads_dict):
        """A cached squad set is only trusted if it (a) has real scoring data
        and (b) matches the current row schema - an older cache from before a
        field like 'photo_url' existed would otherwise render blank forever
        in views that depend on it."""
        REQUIRED_KEYS = {"photo_url", "shirt_color", "initials", "opponents"}
        found_signal = False
        for squad in squads_dict.values():
            for p in squad.get("starting", []) + squad.get("bench", []):
                if not REQUIRED_KEYS.issubset(p.keys()):
                    return False
                if p.get("minutes", 0) > 0 or p.get("points", 0) != 0:
                    found_signal = True
        return found_signal

    print("Fetching per-gameweek squads (cached where possible)...")
    for ev_key, fixtures in fixtures_by_event.items():
        ev = int(ev_key)
        all_finished = all(f["finished"] for f in fixtures)
        prev_fixtures_for_ev = previous_fixtures.get(ev_key, [])

        for idx, f in enumerate(fixtures):
            cached_squads = prev_fixtures_for_ev[idx].get("squads") if (all_finished and idx < len(prev_fixtures_for_ev)) else None
            if cached_squads and squad_looks_valid(cached_squads):
                f["squads"] = cached_squads
                continue
            if not f["started"]:
                continue

            live_stats = live_stats_for_event(ev)
            squads = {}
            for side in ("home", "away"):
                le_id = f[side]["league_entry_id"]
                if le_id is None:
                    continue
                real_entry_id = entry_by_id.get(le_id, {}).get("entry_id")
                if real_entry_id is None:
                    continue
                squad = build_squad(real_entry_id, ev, live_stats)
                if squad:
                    squads[str(le_id)] = squad
            if squads:
                f["squads"] = squads
            elif cached_squads:
                # Refetch attempt also came back empty/invalid - keep the old
                # cached version rather than losing the data entirely.
                f["squads"] = cached_squads

    # ---------------- Latest squads per team (for the Squads tab) ----------------
    latest_squad_event = None
    for ev_key, fixtures in sorted(fixtures_by_event.items(), key=lambda kv: int(kv[0]), reverse=True):
        if any(f.get("squads") for f in fixtures):
            latest_squad_event = int(ev_key)
            break

    latest_squads = {}
    if latest_squad_event is not None:
        for f in fixtures_by_event[str(latest_squad_event)]:
            for le_id_str, squad in (f.get("squads") or {}).items():
                latest_squads[le_id_str] = squad

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
                "league_entry_id": le_id,
                "team_name": pick.get("entry_name") or info.get("team_name", "Unknown"),
                "short_name": info.get("short_name") or (info.get("team_name", "TEA")[:3].upper()),
                "manager_name": info.get("manager_name", ""),
                "player_name": player_info.get("name", f"Player {pick.get('element')}"),
                "position": player_info.get("position", "?"),
                "club": player_info.get("club", "?"),
                "season_points": player_info.get("season_points", 0),
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

    # ---------------- Golden gameweek (best single score, whole season GW1..FINAL_GW) ----------------
    best_per_entry = {}
    for le_id, scores in weekly_scores.items():
        for ev, pts in scores.items():
            if ev is None or ev > FINAL_GW or pts is None:
                continue
            if le_id not in best_per_entry or pts > best_per_entry[le_id]["points"]:
                best_per_entry[le_id] = {"league_entry_id": le_id, "points": pts, "event": ev}

    golden_leaderboard = sorted(best_per_entry.values(), key=lambda r: r["points"], reverse=True)[:8]
    for row in golden_leaderboard:
        info = entry_by_id.get(row["league_entry_id"], {})
        row["team_name"] = info.get("team_name", "Unknown")
        row["manager_name"] = info.get("manager_name", "")
    top_score = golden_leaderboard[0]["points"] if golden_leaderboard else None
    golden_leaders = [r for r in golden_leaderboard if r["points"] == top_score] if golden_leaderboard else []
    golden_gameweek = {
        "season_end": FINAL_GW,
        "leader": golden_leaderboard[0] if golden_leaderboard else None,
        "leaders": golden_leaders,
        "leaderboard": golden_leaderboard,
    }

    # ---------------- Current streaks (hot/cold), from match results ----------------
    streaks = []
    for le_id, results in match_results.items():
        if not results:
            continue
        ordered = sorted(results, key=lambda t: t[0])
        last_result = ordered[-1][1]
        streak_len = 0
        for _, r in reversed(ordered):
            if r == last_result:
                streak_len += 1
            else:
                break
        info = entry_by_id.get(le_id, {})
        streaks.append({
            "league_entry_id": le_id,
            "team_name": info.get("team_name", "Unknown"),
            "manager_name": info.get("manager_name", ""),
            "result": last_result,
            "length": streak_len,
        })
    hottest = max((s for s in streaks if s["result"] == "W"), key=lambda s: s["length"], default=None)
    coldest = max((s for s in streaks if s["result"] == "L"), key=lambda s: s["length"], default=None)
    streaks_summary = {"hottest": hottest, "coldest": coldest, "all": streaks}

    # ---------------- Most / least in-form (last 3 finished H2H results) ----------------
    RESULT_POINTS = {"W": 3, "D": 1, "L": 0}
    form_table = []
    for le_id, results in match_results.items():
        ordered = sorted(results, key=lambda t: t[0])
        last3 = ordered[-3:]
        if not last3:
            continue
        score = sum(RESULT_POINTS[r] for _, r in last3)
        info = entry_by_id.get(le_id, {})
        form_table.append({
            "league_entry_id": le_id,
            "team_name": info.get("team_name", "Unknown"),
            "manager_name": info.get("manager_name", ""),
            "results": [r for _, r in last3],
            "games_counted": len(last3),
            "score": score,
        })
    most_in_form = max(form_table, key=lambda f: f["score"], default=None) if form_table else None
    least_in_form = min(form_table, key=lambda f: f["score"], default=None) if form_table else None
    in_form_summary = {"most": most_in_form, "least": least_in_form}

    # ---------------- Waiver transactions (accepted only) ----------------
    print("Fetching waiver transactions...")
    transactions_raw = get_json_soft(f"draft/league/{LEAGUE_ID}/transactions")
    accepted_transactions = []

    # The transactions endpoint's 'entry' field can be either the league_entry
    # id (used everywhere else in this script) or the real FPL entry id,
    # depending on the response - so build a lookup covering both.
    entry_lookup = dict(entry_by_id)
    for info in entry_by_id.values():
        if info.get("entry_id") is not None:
            entry_lookup[info["entry_id"]] = info

    rank_by_le_id = {s["league_entry_id"]: s["rank"] for s in standings if s.get("rank") is not None}

    if transactions_raw:
        items = transactions_raw.get("transactions", []) if isinstance(transactions_raw, dict) else transactions_raw
        kind_labels = {"w": "Waiver", "f": "Free agent", "t": "Trade"}
        for idx, t in enumerate(items or []):
            if t.get("result") != "a":
                continue
            le_id = t.get("entry")
            info = entry_lookup.get(le_id, {})
            # Resolve to a league_entry id specifically (entry_lookup may have
            # matched via the real entry id) so we can look up their rank.
            resolved_le_id = info.get("league_entry_id", le_id)
            player_in = player_by_id.get(t.get("element_in"), {}).get("name") if t.get("element_in") else None
            player_out = player_by_id.get(t.get("element_out"), {}).get("name") if t.get("element_out") else None
            kind = kind_labels.get(t.get("kind"), t.get("kind") or "Waiver")
            accepted_transactions.append({
                "event": t.get("event"),
                "team_name": info.get("team_name", "Unknown"),
                "manager_name": info.get("manager_name", ""),
                "player_in": player_in,
                "player_out": player_out,
                "kind": kind,
                "_rank": rank_by_le_id.get(resolved_le_id),
                "_original_index": idx,
            })
        # Waiver moves are processed worst-team-first (reverse ladder order,
        # since that's how waiver priority works) - so sort those by rank
        # descending. Free agent / trade moves keep their original relative
        # order, since there's no priority queue for those.
        num_teams = len(standings) or 1

        def sort_key(t):
            if t["kind"] == "Waiver" and t["_rank"] is not None:
                priority = num_teams - t["_rank"]  # worst team (highest rank number) sorts first
            else:
                priority = num_teams + t["_original_index"]  # keep original order, after all waivers
            return (t["event"] is None, t["event"] or 0, priority)

        accepted_transactions.sort(key=sort_key)
        for t in accepted_transactions:
            del t["_rank"]
            del t["_original_index"]
    else:
        print("  note: could not fetch league transactions; waivers-cleared list will be empty.")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league": {
            "id": league.get("id", LEAGUE_ID),
            "name": league.get("name", "FPL Draft League"),
            "scoring": league.get("scoring"),
            "draft_status": league.get("draft_status"),
        },
        "current_event": current_event,
        "current_event_finished": current_event_finished,
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
        "streaks": streaks_summary,
        "in_form": in_form_summary,
        "accepted_transactions": accepted_transactions,
        "latest_squad_event": latest_squad_event,
        "latest_squads": latest_squads,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/league.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data/league.json ({len(standings)} teams, "
          f"{len(finished_events)} finished gameweeks).")


if __name__ == "__main__":
    build()
