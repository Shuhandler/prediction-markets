#!/usr/bin/env python3
"""
discover.py — daily game schedule with exchange search strings.

Lists every game scheduled in a date window (from ESPN's public
scoreboard API — NOT from Kalshi or Polymarket), with copy-paste search
strings for finding the matching market on each exchange yourself:

    Kalshi:  concatenated team abbreviations, e.g.  NYYBOS
    Poly:    team nickname phrase,            e.g.  Yankees Red Sox

Usage:
    python discover.py --date today
    python discover.py --date tomorrow --league mlb
    python discover.py --date 2026-07-12 --json

The search strings are AIDS, not identifiers — abbreviation conventions
occasionally differ between ESPN and Kalshi.  Known mismatches live in
KALSHI_ABBREV_OVERRIDES; add corrections there as you hit them.

Data source note: ESPN's scoreboard endpoint is unofficial (no key, no
SLA).  All fetching is isolated in fetch_scoreboard() so the source can
be swapped without touching the parsing or formatting.

Dependencies: requests (already in requirements.txt).
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta

import requests

ESPN_BASE = "http://site.api.espn.com/apis/site/v2/sports"
TIMEOUT = 15

# (espn sport path, espn league code, label, three_way_outcomes)
# three_way=True marks soccer-style markets (draws) — listed for
# completeness but flagged: the bot's binary-arb model must not trade them.
LEAGUES = [
    ("baseball", "mlb", "MLB", False),
    ("basketball", "wnba", "WNBA", False),
    # Uncomment as seasons start:
    # ("football", "nfl", "NFL", False),
    # ("basketball", "nba", "NBA", False),
    # ("hockey", "nhl", "NHL", False),
    # ("football", "college-football", "NCAAF", False),
    # ("basketball", "mens-college-basketball", "NCAAMB", False),
    ("soccer", "usa.1", "MLS", True),
    ("soccer", "eng.1", "EPL", True),
    ("soccer", "esp.1", "La Liga", True),
    ("soccer", "ita.1", "Serie A", True),
    ("soccer", "ger.1", "Bundesliga", True),
    ("soccer", "fra.1", "Ligue 1", True),
    ("soccer", "uefa.champions", "UCL", True),
]

# ESPN abbreviation -> Kalshi abbreviation, per league label.
# Seeded by diffing ESPN vs live Kalshi listings (2026-07-09); extend
# as new mismatches surface in practice.
KALSHI_ABBREV_OVERRIDES: dict[str, dict[str, str]] = {
    "MLB": {
        "ARI": "AZ",    # Diamondbacks: ESPN ARI, Kalshi AZ
        "CHW": "CWS",   # White Sox:    ESPN CHW, Kalshi CWS
    },
    "WNBA": {
        "POR": "PDX",   # Portland Fire:   ESPN POR, Kalshi PDX
        "CON": "CONN",  # Connecticut Sun: ESPN CON, Kalshi CONN
    },
}


# ====================================================================
# Fetching (isolated so the data source can be swapped)
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


# ====================================================================
# Parsing + search-string generation
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
        if home is None or away is None:
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


def kalshi_search(game: dict) -> str:
    """Kalshi site-search string: away+home abbreviations, e.g. NYYBOS."""
    overrides = KALSHI_ABBREV_OVERRIDES.get(game["league"], {})
    away = overrides.get(game["away_abbr"], game["away_abbr"])
    home = overrides.get(game["home_abbr"], game["home_abbr"])
    return f"{away}{home}"


def poly_search(game: dict) -> str:
    """Polymarket site-search phrase: team nicknames."""
    return f"{game['away_name']} {game['home_name']}"


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

def resolve_date(arg: str) -> date:
    """today / tomorrow / YYYY-MM-DD, interpreted in US/Eastern.

    ESPN's `dates` parameter is US-Eastern based, and both exchanges'
    game days live there; late games cross midnight UTC.  Falls back to
    the local date if the tz database is unavailable (Windows without
    the tzdata package).
    """
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


def _print_table(games_by_league: dict[str, list[dict]], day: date) -> None:
    total = 0
    for label, games in games_by_league.items():
        if not games:
            continue
        three_way = games[0]["three_way"]
        tag = "   [3-way markets — avoid trading]" if three_way else ""
        print(f"\n{label} — {day.strftime('%A %Y-%m-%d')}{tag}")
        for game in sorted(games, key=local_start):
            status = {"in": " [LIVE]", "post": " [FINAL]"}.get(
                game["state"], "",
            )
            matchup = f"{game['away_name']} @ {game['home_name']}"
            print(
                f"  {local_start(game)}  {matchup:<38}"
                f"Kalshi: {kalshi_search(game):<10}"
                f"Poly: {poly_search(game)}{status}"
            )
            total += 1
    print(f"\n{total} game(s)." if total else "\nNo games found.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List scheduled games with exchange search strings.",
    )
    parser.add_argument(
        "--date", default="today",
        help="today, tomorrow, or YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--league", default="",
        help="filter by league label, e.g. mlb, wnba (default: all)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="machine-readable output",
    )
    args = parser.parse_args()

    try:
        day = resolve_date(args.date)
    except ValueError:
        print(f"Unrecognized --date: {args.date!r} (use YYYY-MM-DD)")
        sys.exit(1)
    yyyymmdd = day.strftime("%Y%m%d")

    games_by_league: dict[str, list[dict]] = {}
    for sport, league, label, three_way in LEAGUES:
        if args.league and args.league.lower() != label.lower():
            continue
        payload = fetch_scoreboard(sport, league, yyyymmdd)
        if payload is None:
            print(f"  (warning: could not fetch {label} schedule)",
                  file=sys.stderr)
            continue
        games_by_league[label] = parse_games(payload, label, three_way)

    if args.as_json:
        out = []
        for games in games_by_league.values():
            for game in games:
                out.append({
                    **game,
                    "kalshi_search": kalshi_search(game),
                    "poly_search": poly_search(game),
                    "local_start": local_start(game),
                })
        print(json.dumps(out, indent=2))
        return

    _print_table(games_by_league, day)


if __name__ == "__main__":
    main()
