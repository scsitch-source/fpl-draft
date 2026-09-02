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
from datetime import datetime, timedelta, timezone

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


def get_json_absolute(url):
    """Like get_json but for a full URL outside the Draft API base - used for
    the classic FPL fixtures fallback below."""
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_json_soft_absolute(url):
    try:
        return get_json_absolute(url)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  warning: GET {url} failed ({exc})")
        return None


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

        # Walk the bench in real substitute-priority order (GK-for-GK only).
        # Crucially: if we reach a bench player whose OWN match hasn't
        # finished yet, we must STOP and wait - not skip past them to a
        # lower-priority player who merely happened to play earlier. Only a
        # candidate whose match has genuinely finished with 0 minutes should
        # be treated as unavailable and skipped.
        candidates = [b for b in bench if not b["subbed_in"]
                      and (b["position"] == "GKP") == (starter["position"] == "GKP")]

        chosen = None
        for cand in candidates:
            if cand["minutes"] == 0 and not cand.get("match_finished"):
                break  # still could play - wait, don't consider anyone after them
            if cand["minutes"] == 0:
                continue  # confirmed did not play - try the next candidate
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
            "season_goals": el.get("goals_scored", 0) or 0,
            "season_assists": el.get("assists", 0) or 0,
            "season_clean_sheets": el.get("clean_sheets", 0) or 0,
            "season_bonus": el.get("bonus", 0) or 0,
            "season_defcon": el.get("defensive_contribution", 0) or 0,
            "photo_url": f"{PLAYER_PHOTO_BASE}/{code}.png" if code else None,
            "shirt_color": CLUB_SHIRT_COLORS.get(club_short, DEFAULT_SHIRT_COLOR),
            "initials": player_initials(el.get("web_name", "?")),
        }

    # ---------------- Season-long achievement badges ----------------
    # Computed once across EVERY real Premier League player (not just those
    # on a fantasy roster here), since bootstrap-static's elements list
    # already covers the whole league - no extra API calls needed.
    real_players = [el for el in elements if isinstance(el, dict)]
    if real_players:
        sample = real_players[0]
        print(f"  badge diagnostic - sample element keys: {list(sample.keys())}")
        print(f"  badge diagnostic - sample goals_scored={sample.get('goals_scored')!r}, "
              f"assists={sample.get('assists')!r}, total_points={sample.get('total_points')!r}")
        nonzero_goals = sum(1 for el in real_players if (el.get("goals_scored") or 0) > 0)
        nonzero_assists = sum(1 for el in real_players if (el.get("assists") or 0) > 0)
        print(f"  badge diagnostic - players with goals_scored>0: {nonzero_goals}, "
              f"with assists>0: {nonzero_assists}, total real_players: {len(real_players)}")

    def _top_ids(key, n=1):
        """Element ids at/above the n-th highest value for `key`, ties included."""
        valued = [(el.get(key) or 0, el.get("id")) for el in real_players if el.get(key) is not None]
        valued.sort(reverse=True)
        if not valued:
            return set()
        cutoff = valued[min(n, len(valued)) - 1][0]
        return {eid for val, eid in valued if val >= cutoff}

    golden_boot_ids = _top_ids("goals_scored", 1)
    most_assists_ids = _top_ids("assists", 1)
    top10_overall_ids = _top_ids("total_points", 10)
    best_by_position_ids = {}  # position code -> set of element ids in the top 3
    for pos_code, pos_label in pos_by_type.items():
        pos_players = [el for el in real_players if el.get("element_type") == pos_code]
        valued = sorted(((el.get("total_points") or 0, el.get("id")) for el in pos_players), reverse=True)
        if valued:
            cutoff = valued[min(3, len(valued)) - 1][0]
            best_by_position_ids[pos_label] = {eid for val, eid in valued if val >= cutoff}

    def _position_rank_map(pos_players_list, stat_key, combine_keys=None):
        """Standard competition ranking (ties share a rank, e.g. 1,1,3) of
        every player in this position group by a given season-aggregate
        stat, or the sum of two stats if combine_keys is given (used for
        defenders/keepers' combined goals+assists)."""
        def _val(el):
            if combine_keys:
                return sum(el.get(k) or 0 for k in combine_keys)
            return el.get(stat_key) or 0
        ranked = sorted(pos_players_list, key=lambda el: -_val(el))
        ranks, prev_val, prev_rank = {}, None, 0
        for i, el in enumerate(ranked, start=1):
            v = _val(el)
            if v != prev_val:
                prev_rank = i
                prev_val = v
            ranks[el.get("id")] = prev_rank
        return ranks

    position_ranks = {}  # element id -> {"goals":, "assists":, "bonus":, "clean_sheets":, "ga":, "defcon":}
    for pos_code, pos_label in pos_by_type.items():
        pos_players = [el for el in real_players if el.get("element_type") == pos_code]
        goals_r = _position_rank_map(pos_players, "goals_scored")
        assists_r = _position_rank_map(pos_players, "assists")
        bonus_r = _position_rank_map(pos_players, "bonus")
        cs_r = _position_rank_map(pos_players, "clean_sheets")
        ga_r = _position_rank_map(pos_players, "goals_scored", combine_keys=["goals_scored", "assists"])
        defcon_r = _position_rank_map(pos_players, "defensive_contribution")
        for el in pos_players:
            eid = el.get("id")
            position_ranks[eid] = {
                "goals": goals_r.get(eid), "assists": assists_r.get(eid), "bonus": bonus_r.get(eid),
                "clean_sheets": cs_r.get(eid), "ga": ga_r.get(eid), "defcon": defcon_r.get(eid),
            }

    for el in real_players:
        eid = el.get("id")
        badges = []
        if eid in golden_boot_ids:
            badges.append({"code": "golden_boot", "label": "Golden Boot (most goals)", "icon": "\u26bd"})
        if eid in most_assists_ids:
            badges.append({"code": "most_assists", "label": "Most assists", "icon": "\U0001F3AF"})
        if eid in top10_overall_ids:
            badges.append({"code": "top10", "label": "Top 10 points overall", "icon": "\U0001F3C6"})
        pos_label = pos_by_type.get(el.get("element_type"))
        if pos_label and eid in best_by_position_ids.get(pos_label, set()):
            badges.append({"code": f"best_{pos_label.lower()}", "label": f"Top 3 {pos_label}", "icon": "\u2b50"})
        if eid in player_by_id:
            player_by_id[eid]["badges"] = badges
            player_by_id[eid]["position_ranks"] = position_ranks.get(eid, {})


    # Used to gate auto-subs: a player only gets auto-subbed once their own
    # match has actually finished, not just because they show 0 minutes
    # while the match simply hasn't kicked off yet.
    fixtures_raw = bootstrap.get("fixtures", [])
    print(f"  bootstrap 'fixtures' key: type={type(fixtures_raw).__name__}, "
          f"len={len(fixtures_raw) if isinstance(fixtures_raw, (list, dict)) else 'n/a'}")
    if not isinstance(fixtures_raw, list) or len(fixtures_raw) == 0:
        # The Draft API's bootstrap-static has been quirky about several
        # fields in the past (e.g. 'events' being a dict, not a list) - if
        # 'fixtures' is missing/empty here, fall back to the classic FPL
        # API's own fixtures endpoint, which carries the same team IDs and
        # is a stable, public, unauthenticated endpoint.
        print("  'fixtures' missing/empty from Draft bootstrap-static - "
              "falling back to fantasy.premierleague.com/api/fixtures/")
        fallback = get_json_soft_absolute("https://fantasy.premierleague.com/api/fixtures/")
        if isinstance(fallback, list) and fallback:
            fixtures_raw = fallback
            print(f"  fallback fixtures fetched OK: {len(fixtures_raw)} entries")
        else:
            print("  fallback fixtures fetch also failed or returned nothing")
    # A match's own 'finished' flag can occasionally lag behind reality by a
    # short window even after full-time (stats confirmation, TV coverage
    # delays, etc). As a dependency-free safety net, also treat a fixture as
    # finished if its kickoff was long enough ago that it's certainly over -
    # avoids needing a second external API just to cross-check this.
    FIXTURE_ASSUMED_DURATION = timedelta(hours=2, minutes=30)
    now_utc = datetime.now(timezone.utc)

    def _fixture_effectively_finished(fx):
        if bool(fx.get("finished")):
            return True
        kickoff = fx.get("kickoff_time")
        if not kickoff:
            return False
        try:
            kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return now_utc >= kickoff_dt + FIXTURE_ASSUMED_DURATION

    club_finished_by_event = {}
    club_opponent_by_event = {}
    if isinstance(fixtures_raw, list):
        for fx in fixtures_raw:
            if not isinstance(fx, dict):
                continue
            ev = fx.get("event")
            if ev is None:
                continue
            finished = _fixture_effectively_finished(fx)
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
                home_score, away_score = fx.get("team_h_score"), fx.get("team_a_score")
                opp_bucket.setdefault(home_id, []).append({
                    "opponent": club_by_id.get(away_id, "?"), "is_home": True,
                    "team_score": home_score, "opponent_score": away_score,
                    "finished": finished,
                })
                opp_bucket.setdefault(away_id, []).append({
                    "opponent": club_by_id.get(home_id, "?"), "is_home": False,
                    "team_score": away_score, "opponent_score": home_score,
                    "finished": finished,
                })

    # Club-name-keyed version, for the frontend's squad planner - player
    # rows only carry the club's short name, not its numeric id, so this
    # lets it look up "who does this player's club play in gameweek N" for
    # ANY player (rostered or a free agent), without needing to pre-compute
    # a preview row for every player/gameweek pair.
    fixtures_by_club_and_gw = {}
    for ev, bucket in club_opponent_by_event.items():
        ev_out = {}
        for club_id, opp_list in bucket.items():
            club_name = club_by_id.get(club_id)
            if club_name:
                ev_out[club_name] = opp_list
        fixtures_by_club_and_gw[ev] = ev_out

    # Whether EVERY real Premier League fixture in a gameweek is over
    # (reusing the same kickoff-time safety net above) - lets us confidently
    # treat our own H2H match / the whole gameweek as finished even if the
    # platform's own flags are slow to update, while still requiring every
    # single real fixture to have concluded before doing so.
    real_gw_effectively_finished = {
        ev: bool(bucket) and all(bucket.values())
        for ev, bucket in club_finished_by_event.items()
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

    # Current / next gameweek.
    # CONFIRMED from real Draft API log output: individual gameweek objects
    # do NOT have 'is_current'/'is_next' flags at all (only id/name/finished/
    # deadline_time) - the loop below relying on those never matched
    # anything. The real, reliable source is the top-level events dict's own
    # 'current'/'next' integer fields (e.g. {"current": 2, "data": [...],
    # "next": ...}), which is checked first now.
    current_event = None
    next_event = None
    next_deadline = None
    current_event_finished = False
    if isinstance(_events_raw, dict):
        current_event = _events_raw.get("current")
        next_event = _events_raw.get("next")

    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Still respect is_current/is_next if a Draft API variant DOES
        # provide them (belt and braces), but don't rely on it exclusively.
        if ev.get("is_current"):
            current_event = ev.get("id")
        if ev.get("is_next"):
            next_event = ev.get("id")
            next_deadline = ev.get("deadline_time")
        # Once we know which id is current, pull ITS real 'finished' flag.
        if current_event is not None and ev.get("id") == current_event:
            current_event_finished = bool(ev.get("finished"))
        if next_event is not None and ev.get("id") == next_event and not next_deadline:
            next_deadline = ev.get("deadline_time")

    if current_event is None:
        current_event = game.get("current_event")
        current_event_finished = bool(game.get("current_event_finished"))
    if next_event is None:
        next_event = game.get("next_event")

    # Same safety net as individual fixtures: if every real-world match in
    # the current gameweek is well past its assumed finish time, trust that
    # over a possibly-slow-to-update official flag.
    if current_event is not None and real_gw_effectively_finished.get(current_event):
        current_event_finished = True

    print(f"  current_event={current_event}, current_event_finished={current_event_finished}")
    print(f"  club_finished_by_event[current_event] = "
          f"{club_finished_by_event.get(current_event, '(no entry at all for this event)')}")

    # Per-entry weekly scores + form, derived from finished matches
    weekly_scores = {le_id: {} for le_id in entry_by_id}
    match_results = {le_id: [] for le_id in entry_by_id}  # list of (event, result) W/D/L
    points_against_by_entry = {le_id: [] for le_id in entry_by_id}  # list of opponent scores, finished matches only
    fixtures_by_event = {}

    for m in matches_raw:
        event = m.get("event")
        le1, le2 = m.get("league_entry_1"), m.get("league_entry_2")
        p1, p2 = m.get("league_entry_1_points"), m.get("league_entry_2_points")
        finished = bool(m.get("finished")) or real_gw_effectively_finished.get(event, False)

        if le1 in entry_by_id and p1 is not None:
            weekly_scores[le1][event] = p1
        if le2 in entry_by_id and p2 is not None:
            weekly_scores[le2][event] = p2

        if finished and le1 in entry_by_id and le2 in entry_by_id:
            if p1 is not None and p2 is not None:
                points_against_by_entry[le1].append(p2)
                points_against_by_entry[le2].append(p1)
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
        own_results = sorted(match_results.get(le_id, []), key=lambda t: t[0])
        form_last5 = [r for _, r in own_results[-5:]]

        # Recomputed from our OWN match results (which already reflect the
        # kickoff-time safety net above) rather than trusting the API's raw
        # won/drawn/lost/points_for numbers directly - those can lag behind
        # by a gameweek if the platform's own backend hasn't caught up yet,
        # which would otherwise put the ladder out of sync with everything
        # else on the dashboard that already uses the corrected data.
        own_won = sum(1 for _, r in own_results if r == "W")
        own_drawn = sum(1 for _, r in own_results if r == "D")
        own_lost = sum(1 for _, r in own_results if r == "L")
        own_points_for = sum(weekly_scores.get(le_id, {}).get(ev, 0) or 0 for ev, _ in own_results)
        own_points_against = sum(points_against_by_entry.get(le_id, []))
        own_total = own_won * 3 + own_drawn

        standings.append({
            "rank": s.get("rank"),
            "last_rank": s.get("last_rank"),
            "league_entry_id": le_id,
            "entry_id": info.get("entry_id"),
            "team_name": info.get("team_name", "Unknown"),
            "manager_name": info.get("manager_name", "Unknown Manager"),
            "played": own_won + own_drawn + own_lost,
            "won": own_won,
            "drawn": own_drawn,
            "lost": own_lost,
            "points_for": own_points_for,
            "points_against": own_points_against,
            "total": own_total,
            "event_total": s.get("event_total", 0),
            "current_gw_score": weekly_scores.get(le_id, {}).get(current_event),
            "form": form_last5,
            "is_you": le_id == your_league_entry_id,
        })

    # Rank (and last_rank) also need recomputing to match the corrected
    # totals above - otherwise a team's points can visibly overtake another
    # team's while the API's own (now-stale) rank number hasn't caught up.
    # Same sort criteria used everywhere else here: league points desc, then
    # points-for as the tiebreaker.
    standings.sort(key=lambda r: (-r["total"], -r["points_for"]))
    for i, row in enumerate(standings, start=1):
        row["rank"] = i

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

    # Rank history: each team's standings position at the end of every
    # finished gameweek, for the rank-over-time chart. Reconstructed from
    # match results rather than pulled from the API, since the Draft API
    # only exposes the LATEST cumulative rank, not a per-gameweek history.
    # Uses the standard 3/1/0 win/draw/loss scoring with points-for as the
    # tiebreaker - this is a close approximation of the real standings sort,
    # not a guaranteed exact match to any head-to-head tiebreaker the
    # platform might apply.
    import re as _re

    def _team_initials(name):
        words = _re.findall(r"[A-Za-z0-9]+", name)
        if not words:
            return (name[:2] or "??").upper()
        if len(words) == 1:
            return words[0][:2].upper()
        return "".join(w[0] for w in words[:3]).upper()

    rank_history = {str(le_id): [] for le_id in entry_by_id}
    for ev in finished_events:
        snapshot = []
        for le_id in entry_by_id:
            results_so_far = [r for e, r in match_results[le_id] if e <= ev]
            w = results_so_far.count("W")
            d = results_so_far.count("D")
            pf = sum((weekly_scores.get(le_id, {}).get(e) or 0) for e in finished_events if e <= ev)
            snapshot.append((le_id, w * 3 + d, pf))
        snapshot.sort(key=lambda x: (-x[1], -x[2]))
        for rank, (le_id, _, _) in enumerate(snapshot, start=1):
            rank_history[str(le_id)].append(rank)

    rank_progression = {
        str(le_id): {
            "name": info["team_name"],
            "initials": _team_initials(info["manager_name"]),
            "ranks": rank_history[str(le_id)],
        }
        for le_id, info in entry_by_id.items()
    }

    # Now that rank history exists, backfill last_rank on the standings rows
    # too (rank as of the end of the previous gameweek), rather than trusting
    # the API's own last_rank which is subject to the same staleness issue.
    for row in standings:
        history = rank_progression.get(str(row["league_entry_id"]), {}).get("ranks", [])
        row["last_rank"] = history[-2] if len(history) >= 2 else row["rank"]

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

        def _to_float(v):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return 0.0

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
                "own_goals": stats.get("own_goals", 0),
                "goals_conceded": stats.get("goals_conceded", 0),
                "saves": stats.get("saves", 0),
                # These arrive as strings (e.g. "0.35") from the live-stats
                # endpoint, hence the explicit float conversion.
                "xg": _to_float(stats.get("expected_goals")),
                "xa": _to_float(stats.get("expected_assists")),
                "xgi": _to_float(stats.get("expected_goal_involvements")),
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

        # Last 5 FINISHED gameweeks up to and including this one, for the
        # per-player form strip. Computed once per build_squad call; the
        # actual live-stats lookups it triggers are cheap since
        # live_stats_for_event() memoizes per event already.
        form_events = sorted(
            e.get("id") for e in events
            if isinstance(e, dict) and e.get("finished") and e.get("id") is not None and e.get("id") <= ev
        )[-5:]

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
                                          "bonus": 0, "yellow_cards": 0, "red_cards": 0,
                                          "own_goals": 0, "goals_conceded": 0, "saves": 0,
                                          "xg": 0.0, "xa": 0.0, "xgi": 0.0})
            row = {
                "name": info["name"],
                "position": info["position"],
                "club": info["club"],
                "photo_url": info.get("photo_url"),
                "shirt_color": info.get("shirt_color", "#6B7280"),
                "initials": info.get("initials", "?"),
                "season_points": info.get("season_points", 0),
                "season_goals": info.get("season_goals", 0),
                "season_assists": info.get("season_assists", 0),
                "season_clean_sheets": info.get("season_clean_sheets", 0),
                "season_bonus": info.get("season_bonus", 0),
                "season_defcon": info.get("season_defcon", 0),
                "badges": info.get("badges", []),
                "position_ranks": info.get("position_ranks", {}),
                "form": [live_stats_for_event(fev).get(el_id, {"points": 0}).get("points", 0) for fev in form_events],
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
                "base_points": stat["points"],
                "goals": stat["goals"],
                "assists": stat["assists"],
                "minutes": stat["minutes"],
                # For a gameweek that's fully in the past, the whole real-world
                # round is definitely over regardless of which club a player
                # is CURRENTLY registered to - so trust that instead of doing
                # a club-based fixture lookup, which breaks for anyone who's
                # transferred clubs since that gameweek (we'd otherwise check
                # their new club's fixture, not the one they actually played
                # in). Only the still-in-progress current gameweek needs the
                # real per-club lookup, since transfers don't happen mid-week.
                "match_finished": (
                    True if (current_event is not None and ev < current_event)
                    # Safety net: if the per-club fixture lookup somehow says
                    # not-finished but we've independently confirmed (via the
                    # gameweek's own finished flag) that the whole round is
                    # over, trust that rather than blocking a valid auto-sub.
                    else (
                        club_finished_by_event.get(ev, {}).get(info.get("club_id"), False)
                        or (ev == current_event and bool(current_event_finished))
                    )
                ),
                "clean_sheet": bool(stat.get("clean_sheets")) and info["position"] in ("GKP", "DEF"),
                "defensive_contribution": meets_dc_threshold(stat.get("defensive_contribution"), info["position"]),
                "defensive_contribution_count": stat.get("defensive_contribution", 0) or 0,
                "opponents": club_opponent_by_event.get(ev, {}).get(info.get("club_id"), []),
                "bonus": stat.get("bonus", 0) or 0,
                "yellow_card": bool(stat.get("yellow_cards")),
                "red_card": bool(stat.get("red_cards")),
                "own_goals": stat.get("own_goals", 0) or 0,
                "goals_conceded": stat.get("goals_conceded", 0) or 0,
                "saves": stat.get("saves", 0) or 0,
                "xg": stat.get("xg", 0.0),
                "xa": stat.get("xa", 0.0),
                "xgi": stat.get("xgi", 0.0),
            }
            (raw_starting if is_starting else raw_bench).append(row)

        final_starting, final_bench = apply_autosubs(raw_starting, raw_bench)
        # Anyone left on the bench (not subbed in) just shows their raw points, unmultiplied.
        for p in final_bench:
            p.setdefault("multiplier", 1)
            p.setdefault("points", p["base_points"])

        if ev == current_event:
            dnp_starters = [p for p in final_starting if p["minutes"] == 0]
            if dnp_starters:
                print(f"  [autosub check] GW{ev} entry {real_entry_id}: "
                      f"{len(dnp_starters)} starting XI player(s) with 0 minutes NOT subbed out -> "
                      + ", ".join(f"{p['name']} (match_finished={p['match_finished']})" for p in dnp_starters))
            promoted = [p for p in final_starting if p.get("subbed_in")]
            if promoted:
                print(f"  [autosub check] GW{ev} entry {real_entry_id}: "
                      f"auto-subbed in -> {', '.join(p['name'] for p in promoted)}")

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
        # Every persistent per-player field ever added since caching began -
        # if an older cached gameweek is missing ANY of these, it predates
        # that field's introduction and needs refetching, not reuse. Keep
        # this list updated whenever a new field is added to squad rows.
        REQUIRED_KEYS = {
            "photo_url", "shirt_color", "initials", "opponents",
            "season_points", "badges", "form", "xg", "xa", "xgi",
            "saves", "own_goals", "goals_conceded", "position_ranks",
            "season_goals", "season_assists", "season_clean_sheets",
            "season_bonus", "season_defcon",
        }
        found_signal = False
        for squad in squads_dict.values():
            for p in squad.get("starting", []) + squad.get("bench", []):
                if not REQUIRED_KEYS.issubset(p.keys()):
                    return False
                if p.get("minutes", 0) > 0 or p.get("points", 0) != 0:
                    found_signal = True
        return found_signal

    def _apply_squad_based_score(f):
        """Overwrite the fixture's score with the sum of each side's actual
        (post-auto-sub) starting XI points. The Draft API's own match score
        appears NOT to reflect auto-substitutions in this league - it stays
        as whatever the nominal starting XI scored, understating a team that
        had a sub come on and contribute. Since our own squad data already
        correctly resolves auto-subs, use that as the real source of truth
        everywhere a score is shown, rather than the raw API figure. The
        original API score is kept alongside as 'api_score' for reference.
        """
        squads = f.get("squads")
        if not squads:
            return
        for side in ("home", "away"):
            le_id = f[side]["league_entry_id"]
            squad = squads.get(str(le_id)) if le_id is not None else None
            if not squad:
                continue
            f[side]["api_score"] = f[side].get("score")
            f[side]["score"] = sum(p.get("points", 0) for p in squad.get("starting", []))

    print("Fetching per-gameweek squads (cached where possible)...")
    for ev_key, fixtures in fixtures_by_event.items():
        ev = int(ev_key)
        all_finished = all(f["finished"] for f in fixtures)
        prev_fixtures_for_ev = previous_fixtures.get(ev_key, [])

        for idx, f in enumerate(fixtures):
            cached_squads = prev_fixtures_for_ev[idx].get("squads") if (all_finished and idx < len(prev_fixtures_for_ev)) else None
            if cached_squads and squad_looks_valid(cached_squads):
                f["squads"] = cached_squads
                _apply_squad_based_score(f)
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
            _apply_squad_based_score(f)

    # Keep the standings' live "current GW score" bracket consistent with the
    # same auto-sub-corrected total now used for the fixture scoreboard,
    # rather than the raw (pre-sub) figure computed earlier from matches_raw.
    current_fixtures = fixtures_by_event.get(str(current_event), []) if current_event is not None else []
    corrected_gw_scores = {}
    for f in current_fixtures:
        for side in ("home", "away"):
            le_id = f[side]["league_entry_id"]
            if le_id is not None and f[side].get("score") is not None:
                corrected_gw_scores[le_id] = f[side]["score"]
    for row in standings:
        if row["league_entry_id"] in corrected_gw_scores:
            row["current_gw_score"] = corrected_gw_scores[row["league_entry_id"]]

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

    # ---------------- Future gameweek previews ----------------
    # Picks for a future gameweek don't exist yet, so there's no real squad
    # to show. As a preview, reuse each team's most recently rolled-out
    # roster and recompute every player's real-life opponent for THAT
    # specific gameweek instead of the one that roster actually played -
    # letting someone see roughly how a future matchup shapes up before it
    # starts. All scoring fields reset to zero/not-played, since nothing has
    # happened yet. Built for every future gameweek (not just the next one)
    # so the Fixtures tab can preview any of them, not only the immediate
    # next fixture.
    _name_to_player_info = {info.get("name"): info for info in player_by_id.values() if info.get("name")}

    def _preview_row(row, ev):
        info = _name_to_player_info.get(row.get("name"), {})
        club_id = info.get("club_id")
        preview = dict(row)
        preview.update({
            "minutes": 0, "points": 0, "base_points": 0, "goals": 0, "assists": 0,
            "clean_sheet": False, "defensive_contribution": False, "defensive_contribution_count": 0,
            "bonus": 0, "yellow_card": False, "red_card": False, "own_goals": 0,
            "goals_conceded": 0, "saves": 0, "xg": 0.0, "xa": 0.0, "xgi": 0.0,
            "match_finished": False, "subbed_in": False, "subbed_out": False,
            "opponents": club_opponent_by_event.get(ev, {}).get(club_id, []),
        })
        return preview

    for ev_key, fixtures in fixtures_by_event.items():
        ev = int(ev_key)
        if current_event is not None and ev <= current_event:
            continue  # only genuinely future gameweeks get a preview
        for f in fixtures:
            if f.get("squads"):
                continue  # a real squad already exists (shouldn't happen for a future GW, but just in case)
            preview_squads = {}
            for side in ("home", "away"):
                le_id_str = str(f[side]["league_entry_id"])
                base_squad = latest_squads.get(le_id_str)
                if not base_squad:
                    continue
                preview_squads[le_id_str] = {
                    "starting": [_preview_row(p, ev) for p in base_squad.get("starting", [])],
                    "bench": [_preview_row(p, ev) for p in base_squad.get("bench", [])],
                    "played_count": 0,
                    "squad_size": len(base_squad.get("starting", [])) + len(base_squad.get("bench", [])),
                }
            if preview_squads:
                f["preview_squads"] = preview_squads
                f["is_preview"] = True

    # ---------------- Team of the season ----------------
    # Best real-world XI across the WHOLE Premier League (not just players
    # drafted here), using season-long points. Squad of 15 (2 GKP, 5 DEF,
    # 5 MID, 3 FWD) selected purely by total_points, then the best possible
    # starting XI from those 15 (1 GK + 10 outfield, respecting the same
    # min 3 DEF / min 2 MID / min 1 FWD formation rule used for auto-subs).
    # Recomputed fresh from current totals every run - if someone gets
    # overtaken, a new player simply appears in that spot next time.
    TOS_SQUAD_SIZE = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
    TOS_MIN_OUTFIELD = {"DEF": 3, "MID": 2, "FWD": 1}

    def _owner_lookup(player_name):
        for le_id_str, squad in latest_squads.items():
            names_here = {p["name"] for p in squad.get("starting", []) + squad.get("bench", [])}
            if player_name in names_here:
                return entry_by_id.get(int(le_id_str), {}).get("team_name")
        return None

    def _full_receipt_row(el_id, ev):
        """Same per-gameweek stat shape used for real fantasy squad rows
        (minutes, goals, points, match_finished, etc.), but for an
        arbitrary real player who isn't necessarily on anyone's roster -
        used so Team of the Season / top scorers can open the same
        points-receipt popover as everywhere else on the dashboard."""
        info = player_by_id.get(el_id, {})
        if ev is None:
            stat = {}
        else:
            stat = live_stats_for_event(ev).get(el_id, {})
        stat = stat or {"points": 0, "goals": 0, "assists": 0, "minutes": 0,
                         "clean_sheets": 0, "defensive_contribution": 0,
                         "bonus": 0, "yellow_cards": 0, "red_cards": 0,
                         "own_goals": 0, "goals_conceded": 0, "saves": 0,
                         "xg": 0.0, "xa": 0.0, "xgi": 0.0}
        position = info.get("position", "?")
        club_id = info.get("club_id")
        match_finished = (
            True if (ev is not None and current_event is not None and ev < current_event)
            else (
                club_finished_by_event.get(ev, {}).get(club_id, False)
                or (ev == current_event and bool(current_event_finished))
            )
        ) if ev is not None else False
        return {
            "name": info.get("name", "Unknown"),
            "position": position,
            "club": info.get("club", "?"),
            "club_id": club_id,
            "photo_url": info.get("photo_url"),
            "shirt_color": info.get("shirt_color", "#6B7280"),
            "initials": info.get("initials", "?"),
            "season_points": info.get("season_points", 0),
            "is_captain": False, "is_vice_captain": False,
            "subbed_in": False, "subbed_out": False,
            "base_points": stat.get("points", 0),
            "points": stat.get("points", 0),
            "goals": stat.get("goals", 0),
            "assists": stat.get("assists", 0),
            "minutes": stat.get("minutes", 0),
            "match_finished": match_finished,
            "clean_sheet": bool(stat.get("clean_sheets")) and position in ("GKP", "DEF"),
            "defensive_contribution": meets_dc_threshold(stat.get("defensive_contribution"), position),
            "defensive_contribution_count": stat.get("defensive_contribution", 0) or 0,
            "opponents": club_opponent_by_event.get(ev, {}).get(club_id, []) if ev is not None else [],
            "bonus": stat.get("bonus", 0) or 0,
            "yellow_card": bool(stat.get("yellow_cards")),
            "red_card": bool(stat.get("red_cards")),
            "own_goals": stat.get("own_goals", 0) or 0,
            "goals_conceded": stat.get("goals_conceded", 0) or 0,
            "saves": stat.get("saves", 0) or 0,
            "xg": stat.get("xg", 0.0),
            "xa": stat.get("xa", 0.0),
            "xgi": stat.get("xgi", 0.0),
        }

    def _build_best_xi(point_getter, receipt_ev, rank_field):
        """Selects a 15-player squad (2 GKP/5 DEF/5 MID/3 FWD) by whatever
        point_getter ranks players on, then the best 11 from those 15 (1 GK
        + 10 outfield, min 3 DEF/2 MID/1 FWD) - same shape used for both
        Team of the Week (this gameweek's points) and Team of the Season
        (season totals), just fed a different ranking."""
        squad_by_pos = {}
        for pos_code, pos_label in pos_by_type.items():
            pos_players = sorted(
                (el for el in real_players if el.get("element_type") == pos_code),
                key=lambda el: -(point_getter(el) or 0),
            )
            squad_by_pos[pos_label] = pos_players[:TOS_SQUAD_SIZE.get(pos_label, 0)]

        outfield_pool = [
            (pos_label, el)
            for pos_label in ("DEF", "MID", "FWD")
            for el in squad_by_pos.get(pos_label, [])
        ]
        outfield_pool.sort(key=lambda t: (point_getter(t[1]) or 0))  # ascending - drop from the front
        outfield_counts = {pos: len(squad_by_pos.get(pos, [])) for pos in ("DEF", "MID", "FWD")}
        dropped_ids = set()
        i = 0
        while len(dropped_ids) < 3 and i < len(outfield_pool):
            pos_label, el = outfield_pool[i]
            if outfield_counts[pos_label] - 1 >= TOS_MIN_OUTFIELD[pos_label]:
                dropped_ids.add(el.get("id"))
                outfield_counts[pos_label] -= 1
            i += 1

        gk_list = squad_by_pos.get("GKP", [])

        def _player_row(el):
            eid = el.get("id")
            row = _full_receipt_row(eid, receipt_ev)
            row["owner_team_name"] = _owner_lookup(row["name"])
            return row

        starting, bench = [], []
        if gk_list:
            starting.append(_player_row(gk_list[0]))
        if len(gk_list) > 1:
            bench.append(_player_row(gk_list[1]))
        for pos_label in ("DEF", "MID", "FWD"):
            for el in squad_by_pos.get(pos_label, []):
                target = bench if el.get("id") in dropped_ids else starting
                target.append(_player_row(el))

        # Mark the two highest scorers in the STARTING XI (fun "who'd you
        # captain" touch, not a real fantasy captaincy) as C and VC.
        ranked_starting = sorted(starting, key=lambda p: -(p.get(rank_field) or 0))
        if len(ranked_starting) > 0:
            ranked_starting[0]["is_captain"] = True
        if len(ranked_starting) > 1:
            ranked_starting[1]["is_vice_captain"] = True

        return {"starting": starting, "bench": bench}

    team_of_season = _build_best_xi(
        point_getter=lambda el: el.get("total_points"),
        receipt_ev=latest_squad_event,
        rank_field="season_points",
    )

    def _fixture_started(fx):
        if bool(fx.get("started")) or bool(fx.get("finished")):
            return True
        kickoff = fx.get("kickoff_time")
        if not kickoff:
            return False
        try:
            kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return now_utc >= kickoff_dt

    def _any_fixture_started(ev):
        if ev is None or not isinstance(fixtures_raw, list):
            return False
        return any(_fixture_started(fx) for fx in fixtures_raw if fx.get("event") == ev)

    def _gw_points_getter(ev):
        def _getter(el):
            return live_stats_for_event(ev).get(el.get("id"), {}).get("points", 0)
        return _getter

    # Team of the Week, computed for every gameweek that's actually begun
    # (not ones still in the future - nothing's happened there yet).
    team_of_week_by_gw = {}
    for ev in sorted(set(finished_events) | ({current_event} if current_event is not None else set())):
        if _any_fixture_started(ev):
            team_of_week_by_gw[ev] = _build_best_xi(
                point_getter=_gw_points_getter(ev), receipt_ev=ev, rank_field="points",
            )

    # Which gameweek to show by default: the current one once its first
    # match has kicked off, otherwise fall back to the last one that has
    # actually started (so it's never an empty, all-zero "in progress"
    # gameweek with nothing to show yet).
    default_week_gw = None
    if current_event is not None and _any_fixture_started(current_event):
        default_week_gw = current_event
    elif team_of_week_by_gw:
        default_week_gw = max(team_of_week_by_gw.keys())

    team_of_week = team_of_week_by_gw.get(default_week_gw, {"starting": [], "bench": []})

    # Top scorers league-wide (overall, and per position for the tabs),
    # each with the same full receipt row so these are clickable too.
    TOP_SCORERS_N = 10

    def _top_scorers_list(pool):
        ranked = sorted(pool, key=lambda el: -(el.get("total_points") or 0))[:TOP_SCORERS_N]
        out = []
        for rank, el in enumerate(ranked, start=1):
            row = _full_receipt_row(el.get("id"), latest_squad_event)
            row["rank"] = rank
            row["owner_team_name"] = _owner_lookup(row["name"])
            out.append(row)
        return out

    top_scorers_by_tab = {"all": _top_scorers_list(real_players)}
    for pos_code, pos_label in pos_by_type.items():
        top_scorers_by_tab[pos_label] = _top_scorers_list(
            [el for el in real_players if el.get("element_type") == pos_code]
        )

    # ---------------- Players directory (Players tab) ----------------
    # Every real Premier League player, not just a top-N cut, for a
    # searchable/browsable directory. Same full receipt row as everywhere
    # else so these are clickable too.
    def _all_players_list(pool):
        ranked = sorted(pool, key=lambda el: -(el.get("total_points") or 0))
        out = []
        for rank, el in enumerate(ranked, start=1):
            row = _full_receipt_row(el.get("id"), latest_squad_event)
            row["rank"] = rank
            row["owner_team_name"] = _owner_lookup(row["name"])
            out.append(row)
        return out

    players_directory = {"all": _all_players_list(real_players)}
    for pos_code, pos_label in pos_by_type.items():
        players_directory[pos_label] = _all_players_list(
            [el for el in real_players if el.get("element_type") == pos_code]
        )

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
        kind_labels = {"w": "Waiver", "f": "Free Agent", "t": "Trade"}
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
                "league_entry_id": resolved_le_id,
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

    # Remapped by club short name (not id) - the frontend's players_directory
    # rows carry a readable club short name, so this is the more convenient
    # key for recomputing a waiver-in player's fixture for an arbitrary
    # future gameweek.
    fixtures_by_club_and_gw = {
        ev: {club_by_id.get(club_id, "?"): fixtures for club_id, fixtures in by_club.items()}
        for ev, by_club in club_opponent_by_event.items()
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
        "rank_progression": {
            "events": finished_events,
            "teams": rank_progression,
        },
        "playoffs": playoffs,
        "golden_gameweek": golden_gameweek,
        "draft_picks": draft_picks,
        "streaks": streaks_summary,
        "in_form": in_form_summary,
        "accepted_transactions": accepted_transactions,
        "latest_squad_event": latest_squad_event,
        "team_of_season": team_of_season,
        "team_of_week": team_of_week,
        "team_of_week_by_gw": team_of_week_by_gw,
        "fixtures_by_club_and_gw": fixtures_by_club_and_gw,
        "fixtures_by_club_and_gw": fixtures_by_club_and_gw,
        "default_week_gw": default_week_gw,
        "top_scorers": top_scorers_by_tab,
        "players_directory": players_directory,
        "latest_squads": latest_squads,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/league.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data/league.json ({len(standings)} teams, "
          f"{len(finished_events)} finished gameweeks).")


if __name__ == "__main__":
    build()
