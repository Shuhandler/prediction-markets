#!/usr/bin/env python3
"""
discover.py — daily game schedule.

Lists every game scheduled in a date window (from ESPN or Polymarket APIs),
grouped by league.

Usage:
    python discover.py --date today
    python discover.py --date tomorrow --league mlb
    python discover.py --date all --league cs2
    python discover.py --date 2026-07-12 --json

Data source note: ESPN's scoreboard endpoint is unofficial (no key, no
SLA). Polymarket Gamma API is used to discover esports/cricket events.
All fetching is isolated in fetch functions.

Dependencies: requests (already in requirements.txt).
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta

import requests

ESPN_BASE = "http://site.api.espn.com/apis/site/v2/sports"
TIMEOUT = 15

# (espn sport path, espn league code, label, three_way_outcomes, source_platform, source_id)
# three_way=True marks soccer-style markets (draws) — listed for
# completeness but flagged: the bot's binary-arb model must not trade them.
LEAGUES = [
    ("baseball", "mlb", "MLB", False, "espn", ""),
    ("basketball", "wnba", "WNBA", False, "espn", ""),
    ("tennis", "atp", "ATP", False, "espn", ""),
    ("tennis", "wta", "WTA", False, "espn", ""),
    ("", "", "Cricket", False, "polymarket", "517"),
    ("", "", "CS2", False, "polymarket", "100780"),
    # Uncomment as seasons start:
    # ("football", "nfl", "NFL", False, "espn", ""),
    # ("basketball", "nba", "NBA", False, "espn", ""),
    # ("hockey", "nhl", "NHL", False, "espn", ""),
    # ("football", "college-football", "NCAAF", False, "espn", ""),
    # ("basketball", "mens-college-basketball", "NCAAMB", False, "espn", ""),
    ("soccer", "usa.1", "MLS", True, "espn", ""),
    ("soccer", "eng.1", "EPL", True, "espn", ""),
    ("soccer", "esp.1", "La Liga", True, "espn", ""),
    ("soccer", "ita.1", "Serie A", True, "espn", ""),
    ("soccer", "ger.1", "Bundesliga", True, "espn", ""),
    ("soccer", "fra.1", "Ligue 1", True, "espn", ""),
    ("soccer", "uefa.champions", "UCL", True, "espn", ""),
]


# ====================================================================
# Fetching
# ====================================================================

def fetch_scoreboard(sport: str, league: str, yyyymmdd: str) -> dict | None:
    """GET one league's scoreboard for one date.  None on any failure."""
    url = f"{ESPN_BASE}/{sport}/{league}/scoreboard"
    try:
        resp = requests.get(
            url, params={"dates": yyyymmdd}, timeout=TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            return None
        return resp.json()
    except Exception:
        return None


def fetch_polymarket_events(tag_id: str, day: date | None) -> list[dict]:
    """Fetch active Polymarket events for a tag and filter by start date."""
    url = "https://gamma-api.polymarket.com/events"
    try:
        resp = requests.get(
            url, params={"active": "true", "tag_id": tag_id, "limit": 100},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        events = resp.json()
        games = []
        for ev in events:
            start_str = ev.get("startDate") or ev.get("createdAt") or ""
            if not start_str:
                continue
            try:
                # Parse date (UTC)
                dt_utc = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                # Convert to local/Eastern date to match ESPN's date comparison
                event_date = dt_utc.astimezone().date()
            except Exception:
                continue

            if day and event_date != day:
                continue

            title = ev.get("title", "")
            
            # Clean up title for matching matchups
            title_clean = title.replace("Counter-Strike:", "").replace("CS2:", "").strip()
            
            # Try to split by vs / vs.
            if " vs. " in title_clean:
                parts = title_clean.split(" vs. ")
            elif " vs " in title_clean:
                parts = title_clean.split(" vs ")
            else:
                parts = [title_clean]
                
            if len(parts) >= 2:
                away_name = parts[0].strip()
                # Split away any trailing info like "- Cricket T20 World Cup"
                home_name = parts[1].split(" - ")[0].strip()
            else:
                away_name = title_clean
                home_name = ""

            games.append({
                "league": "Cricket" if tag_id == "517" else "CS2",
                "three_way": False,
                "start": start_str,
                "state": "pre",
                "away_abbr": "",
                "home_abbr": "",
                "away_name": away_name,
                "home_name": home_name,
            })
        return games
    except Exception:
        return []


# ====================================================================
# Parsing
# ====================================================================

def _nickname(competitor: dict) -> str:
    team = competitor.get("team") or {}
    return (
        team.get("shortDisplayName")
        or team.get("name")
        or team.get("displayName")
        or "?"
    )


def parse_games(payload: dict, label: str, three_way: bool) -> list[dict]:
    """Extract games from one ESPN scoreboard response."""
    games = []
    for event in payload.get("events", []) or []:
        competitions = event.get("competitions") or [{}]
        competitors = competitions[0].get("competitors") or []
        
        home = next(
            (c for c in competitors if c.get("homeAway") == "home"), None,
        )
        away = next(
            (c for c in competitors if c.get("homeAway") == "away"), None,
        )
        
        # Fallback for leagues/events without explicit home/away designation (e.g. tennis brackets)
        if home is None or away is None:
            if len(competitors) >= 2:
                home = competitors[0]
                away = competitors[1]
            else:
                continue
                
        state = (
            ((event.get("status") or {}).get("type")) or {}
        ).get("state", "")
        
        games.append({
            "league": label,
            "three_way": three_way,
            "start": event.get("date", ""),
            "state": state,                      # pre | in | post
            "away_abbr": (away.get("team") or {}).get("abbreviation", "?"),
            "home_abbr": (home.get("team") or {}).get("abbreviation", "?"),
            "away_name": _nickname(away),
            "home_name": _nickname(home),
        })
    return games


def local_start(game: dict) -> str:
    """Game start in the machine's local time (HH:MM), '--:--' if unknown."""
    raw = game.get("start", "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return "--:--"


# ====================================================================
# CLI
# ====================================================================

def resolve_date(arg: str) -> date | None:
    """today / tomorrow / YYYY-MM-DD / all, interpreted in US/Eastern.

    ESPN's `dates` parameter is US-Eastern based; late games cross midnight UTC.
    Falls back to the local date if the tz database is unavailable.
    """
    if arg == "all":
        return None
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now()
    if arg == "today":
        return now.date()
    if arg == "tomorrow":
        return (now + timedelta(days=1)).date()
    return datetime.strptime(arg, "%Y-%m-%d").date()


def _print_table(games_by_league: dict[str, list[dict]], day: date | None) -> None:
    total = 0
    header_date = day.strftime('%A %Y-%m-%d') if day else "All Active"
    for label, games in games_by_league.items():
        if not games:
            continue
        three_way = games[0]["three_way"]
        tag = "   [3-way markets — avoid trading]" if three_way else ""
        print(f"\n{label} — {header_date}{tag}")
        for game in sorted(games, key=local_start):
            status = {"in": " [LIVE]", "post": " [FINAL]"}.get(
                game["state"], "",
            )
            if game['home_name']:
                matchup = f"{game['away_name']} vs {game['home_name']}"
            else:
                matchup = game['away_name']
            print(f"  {local_start(game)}  {matchup:<45}{status}")
            total += 1
    print(f"\n{total} game(s)." if total else "\nNo games found.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List scheduled games.",
    )
    parser.add_argument(
        "--date", default="today",
        help="today, tomorrow, YYYY-MM-DD, or 'all' (default: today)",
    )
    parser.add_argument(
        "--league", default="",
        help="filter by league label, e.g. mlb, wnba, cs2 (default: all)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="machine-readable output",
    )
    args = parser.parse_args()

    try:
        day = resolve_date(args.date)
    except ValueError:
        print(f"Unrecognized --date: {args.date!r} (use YYYY-MM-DD or 'all')")
        sys.exit(1)
        
    yyyymmdd = day.strftime("%Y%m%d") if day else ""

    games_by_league: dict[str, list[dict]] = {}
    for sport, league, label, three_way, source, source_id in LEAGUES:
        if args.league and args.league.lower() != label.lower():
            continue
            
        if source == "espn":
            if not day:
                # ESPN does not support fetching "all" dates
                continue
            payload = fetch_scoreboard(sport, league, yyyymmdd)
            if payload is None:
                print(f"  (warning: could not fetch {label} schedule)",
                      file=sys.stderr)
                continue
            games_by_league[label] = parse_games(payload, label, three_way)
        elif source == "polymarket":
            games_by_league[label] = fetch_polymarket_events(source_id, day)

    if args.as_json:
        out = []
        for games in games_by_league.values():
            for game in games:
                out.append({
                    **game,
                    "local_start": local_start(game),
                })
        print(json.dumps(out, indent=2))
        return

    _print_table(games_by_league, day)


if __name__ == "__main__":
    main()
