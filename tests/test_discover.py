"""discover.py: schedule parsing and exchange search-string generation
(offline — canned ESPN scoreboard fixture, no network)."""
from datetime import date

import discover


def espn_fixture():
    """Minimal but structurally-faithful ESPN scoreboard response."""
    def team(abbr, nickname, home_away):
        return {
            "homeAway": home_away,
            "team": {
                "abbreviation": abbr,
                "shortDisplayName": nickname,
                "displayName": f"City {nickname}",
            },
        }

    def event(date_iso, state, away, home):
        return {
            "date": date_iso,
            "status": {"type": {"state": state}},
            "competitions": [{
                "competitors": [
                    team(*home, "home"),
                    team(*away, "away"),
                ],
            }],
        }

    return {
        "events": [
            event("2026-07-09T23:05Z", "pre",
                  ("NYY", "Yankees"), ("BOS", "Red Sox")),
            event("2026-07-09T20:10Z", "in",
                  ("CHW", "White Sox"), ("DET", "Tigers")),
            # malformed event without competitors — must be skipped
            {"date": "2026-07-09T22:00Z", "competitions": [{}]},
        ],
    }


def test_parse_games():
    games = discover.parse_games(espn_fixture(), "MLB", False)
    assert len(games) == 2
    first = games[0]
    assert (first["away_abbr"], first["home_abbr"]) == ("NYY", "BOS")
    assert (first["away_name"], first["home_name"]) == ("Yankees", "Red Sox")
    assert first["state"] == "pre"
    assert games[1]["state"] == "in"


def test_kalshi_search_concatenates_away_home():
    games = discover.parse_games(espn_fixture(), "MLB", False)
    assert discover.kalshi_search(games[0]) == "NYYBOS"


def test_kalshi_search_applies_overrides():
    games = discover.parse_games(espn_fixture(), "MLB", False)
    white_sox = games[1]
    # ESPN says CHW; the override table maps to Kalshi's convention
    assert "CHW" in discover.KALSHI_ABBREV_OVERRIDES["MLB"]
    expected = discover.KALSHI_ABBREV_OVERRIDES["MLB"]["CHW"] + "DET"
    assert discover.kalshi_search(white_sox) == expected


def test_poly_search_uses_nicknames():
    games = discover.parse_games(espn_fixture(), "MLB", False)
    assert discover.poly_search(games[0]) == "Yankees Red Sox"


def test_local_start_handles_bad_dates():
    games = discover.parse_games(espn_fixture(), "MLB", False)
    assert discover.local_start(games[0]) != "--:--"
    assert discover.local_start({"start": "not-a-date"}) == "--:--"
    assert discover.local_start({"start": ""}) == "--:--"


def test_resolve_date_explicit():
    assert discover.resolve_date("2026-07-12") == date(2026, 7, 12)


def test_resolve_date_relative():
    today = discover.resolve_date("today")
    tomorrow = discover.resolve_date("tomorrow")
    assert (tomorrow - today).days == 1


def test_nickname_fallbacks():
    assert discover._nickname({"team": {"name": "Nickname Only"}}) == "Nickname Only"
    assert discover._nickname({"team": {"displayName": "Full Name"}}) == "Full Name"
    assert discover._nickname({}) == "?"
