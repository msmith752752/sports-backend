from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import requests
import httpx
import asyncio
import os
from collections import defaultdict
from datetime import datetime, timezone

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── HEADERS — looks like a real browser to NBA.com ──────────────────────────
NBA_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Host": "cdn.nba.com",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

SCOREBOARD_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
PBP_URL        = "https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{game_id}.json"
# ─────────────────────────────────────────────────────────────────────────────

# ─── CONFIG — loaded from .env file, never hardcoded ─────────────────────────
YOUTUBE_API_KEY      = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_LIVE_CHAT_ID = os.getenv("YOUTUBE_LIVE_CHAT_ID")
# ─────────────────────────────────────────────────────────────────────────────

# ─── VOTE STATE ──────────────────────────────────────────────────────────────
votes = defaultdict(int)          # { "Brunson": 12, "Mitchell": 7, ... }
over_under = {"over": 0, "under": 0}
voters = set()                    # deduplicate by channel ID
vote_question = "Who scores next?"
last_chat_token = None
chat_poll_active = False
# ─────────────────────────────────────────────────────────────────────────────

# ─── SCORE HISTORY for momentum graph ────────────────────────────────────────
score_history = []   # [ { time, awayScore, homeScore, period } ]
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/")
def home():
    return {"status": "Sports Terminal Backend Running"}


@app.get("/game-feed")
def get_game_feed():
    try:
        r    = requests.get(SCOREBOARD_URL, headers=NBA_HEADERS, timeout=10)
        data = r.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}

    games = data["scoreboard"]["games"]

    if not games:
        return {"status": "no_games"}

    # Target Knicks vs Cavaliers specifically
    game = None
    for g in games:
        teams = [g["homeTeam"]["teamName"], g["awayTeam"]["teamName"]]
        if "Knicks" in teams or "Cavaliers" in teams:
            game = g
            break
    if game is None:
        game = games[0]

    game_id    = game["gameId"]
    home_team  = game["homeTeam"]["teamName"]
    away_team  = game["awayTeam"]["teamName"]
    home_score = game["homeTeam"]["score"]
    away_score = game["awayTeam"]["score"]
    period     = game["period"]
    clock      = game["gameClock"]

    score_history.append({
        "t":      datetime.now(timezone.utc).isoformat(),
        "away":   away_score,
        "home":   home_score,
        "period": period,
    })
    if len(score_history) > 200:
        score_history.pop(0)

    # Play-by-play
    last_play   = None
    action_team = None
    play_type   = None

    try:
        pbp_r   = requests.get(PBP_URL.format(game_id=game_id), headers=NBA_HEADERS, timeout=10)
        pbp     = pbp_r.json()
        actions = pbp.get("game", {}).get("actions", [])

        if actions:
            latest    = actions[-1]
            last_play = latest.get("description", "")
            play_type = classify_play(last_play)

            if latest.get("teamId") == game["homeTeam"]["teamId"]:
                action_team = "home"
            elif latest.get("teamId") == game["awayTeam"]["teamId"]:
                action_team = "away"
    except Exception:
        pass

    return {
        "homeTeam":   home_team,
        "awayTeam":   away_team,
        "homeScore":  home_score,
        "awayScore":  away_score,
        "period":     period,
        "clock":      clock,
        "lastPlay":   last_play,
        "actionTeam": action_team,
        "playType":   play_type,
    }


def classify_play(text: str) -> str:
    """Tag a play-by-play description with a type icon + label."""
    if not text:
        return None
    t = text.lower()
    if "3pt"    in t or "3-pt"    in t or "three" in t: return "3PT"
    if "free throw"                                in t: return "FT"
    if "dunk"                                      in t: return "DUNK"
    if "alley oop"                                 in t: return "ALLEY OOP"
    if "block"                                     in t: return "BLOCK"
    if "steal"                                     in t: return "STEAL"
    if "turnover"                                  in t: return "TURNOVER"
    if "timeout"                                   in t: return "TIMEOUT"
    if "foul"                                      in t: return "FOUL"
    if "and 1"  in t or "and-1"   in t            : return "AND-1"
    if "jump shot" in t or "layup" in t            : return "2PT"
    return "PLAY"


@app.get("/score-history")
def get_score_history():
    return score_history


@app.get("/votes")
def get_votes():
    total_player = sum(votes.values())
    total_ou     = over_under["over"] + over_under["under"]
    return {
        "question":   vote_question,
        "players":    dict(votes),
        "overUnder":  over_under,
        "totalVotes": total_player,
        "totalOU":    total_ou,
    }


@app.post("/reset-votes")
def reset_votes(new_question: str = "Who scores next?"):
    global vote_question
    votes.clear()
    over_under["over"]  = 0
    over_under["under"] = 0
    voters.clear()
    vote_question = new_question
    return {"status": "reset", "question": vote_question}


@app.post("/start-chat-poll")
async def start_chat_poll(background_tasks: BackgroundTasks):
    global chat_poll_active
    if not chat_poll_active:
        chat_poll_active = True
        background_tasks.add_task(poll_youtube_chat)
    return {"status": "polling started"}


@app.post("/stop-chat-poll")
def stop_chat_poll():
    global chat_poll_active
    chat_poll_active = False
    return {"status": "polling stopped"}


async def poll_youtube_chat():
    """Continuously read YouTube live chat and tally !predict / !over / !under / !mvp commands."""
    global last_chat_token, chat_poll_active

    known_players = [
        "Brunson", "Towns", "Anunoby", "Bridges", "Hart", "DiVincenzo",
        "Mitchell", "Mobley", "Garland", "Allen", "Strus", "Hunter"
    ]

    async with httpx.AsyncClient() as client:
        while chat_poll_active:
            try:
                params = {
                    "liveChatId": YOUTUBE_LIVE_CHAT_ID,
                    "part":       "snippet,authorDetails",
                    "key":        YOUTUBE_API_KEY,
                    "maxResults": 200,
                }
                if last_chat_token:
                    params["pageToken"] = last_chat_token

                r    = await client.get(
                    "https://www.googleapis.com/youtube/v3/liveChat/messages",
                    params=params, timeout=10
                )
                data = r.json()

                last_chat_token = data.get("nextPageToken")
                poll_interval   = data.get("pollingIntervalMillis", 10000) / 1000

                for item in data.get("items", []):
                    channel_id = item["authorDetails"]["channelId"]
                    if channel_id in voters:
                        continue                        # one vote per viewer

                    msg = item["snippet"].get("displayMessage", "").strip().lower()

                    if msg.startswith("!over"):
                        over_under["over"] += 1
                        voters.add(channel_id)

                    elif msg.startswith("!under"):
                        over_under["under"] += 1
                        voters.add(channel_id)

                    elif msg.startswith("!predict ") or msg.startswith("!mvp "):
                        word = msg.split(" ", 1)[1].strip().title()
                        # match to known player last names
                        matched = next(
                            (p for p in known_players if p.lower() in word.lower()),
                            None
                        )
                        if matched:
                            votes[matched] += 1
                            voters.add(channel_id)

                await asyncio.sleep(poll_interval)

            except Exception as e:
                print(f"Chat poll error: {e}")
                await asyncio.sleep(15)


# ─── DRAFTKINGS ODDS (proxied through local backend) ─────────────────────────
# Your local machine isn't blocked by DK — only cloud servers are
DK_ENDPOINTS = [
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1/leagues/42648?format=json",
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusma/v1/leagues/42648?format=json",
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusco/v1/leagues/42648?format=json",
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkuspa/v1/leagues/42648?format=json",
]

DK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://sportsbook.draftkings.com",
    "Referer": "https://sportsbook.draftkings.com/leagues/basketball/nba",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
}

def parse_dk_odds(data):
    """Parse DraftKings JSON to extract NYK vs CLE spread/ML/total."""
    nyk_spread = nyk_ml = cle_spread = cle_ml = ou_total = None

    # DK structure: eventGroup -> offerCategories -> offers -> outcomes
    categories = data.get("eventGroup", {}).get("offerCategories", [])
    for cat in categories:
        for subcat in cat.get("offerSubcategoryDescriptors", []):
            offers = subcat.get("offerSubcategory", {}).get("offers", [])
            for offer_group in offers:
                for offer in (offer_group if isinstance(offer_group, list) else [offer_group]):
                    label = (offer.get("label") or "").lower()
                    outcomes = offer.get("outcomes", [])

                    # Find NYK vs CLE game
                    teams_in_offer = " ".join(o.get("label","") for o in outcomes).lower()
                    if "knick" not in teams_in_offer and "cavalier" not in teams_in_offer:
                        continue

                    for o in outcomes:
                        ol = (o.get("label") or "").lower()
                        line = o.get("line")
                        odds = o.get("oddsAmerican")

                        if "knick" in ol:
                            if "spread" in label and line is not None: nyk_spread = line
                            if "moneyline" in label and odds is not None: nyk_ml = odds
                        if "cavalier" in ol or "cavs" in ol:
                            if "spread" in label and line is not None: cle_spread = line
                            if "moneyline" in label and odds is not None: cle_ml = odds
                        if "over" in ol and "total" in label and line is not None:
                            ou_total = line

    return nyk_spread, cle_spread, nyk_ml, cle_ml, ou_total


@app.get("/odds")
def get_odds():
    # Try DraftKings first
    for url in DK_ENDPOINTS:
        try:
            r = requests.get(url, headers=DK_HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            nyk_spread, cle_spread, nyk_ml, cle_ml, ou_total = parse_dk_odds(data)
            if nyk_spread is not None:
                return {
                    "status": "ok",
                    "source": "draftkings",
                    "nyk": {"spread": nyk_spread, "ml": nyk_ml},
                    "cle": {"spread": cle_spread, "ml": cle_ml},
                    "total": ou_total,
                }
        except Exception as e:
            print(f"DK odds error ({url[-30:]}): {e}")
            continue

    # Try ESPN public odds API (no key needed)
    try:
        espn_url = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events?limit=20"
        espn_headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        r = requests.get(espn_url, headers=espn_headers, timeout=8)
        if r.status_code == 200:
            events = r.json().get("items", [])
            for ev_ref in events:
                ev_url = ev_ref.get("$ref", "")
                ev_r = requests.get(ev_url, headers=espn_headers, timeout=6)
                if ev_r.status_code != 200:
                    continue
                ev = ev_r.json()
                name = ev.get("name", "")
                if "Knick" not in name and "Cavalier" not in name:
                    continue
                # Found the game — get odds
                comps = ev.get("competitions", [{}])
                for comp in comps:
                    odds_list = comp.get("odds", [])
                    for odd in odds_list:
                        nyk_spread = odd.get("awayTeamOdds", {}).get("spreadOdds")
                        cle_spread = odd.get("homeTeamOdds", {}).get("spreadOdds")
                        nyk_ml = odd.get("awayTeamOdds", {}).get("moneyLine")
                        cle_ml = odd.get("homeTeamOdds", {}).get("moneyLine")
                        ou_total = odd.get("overUnder")
                        if nyk_spread is not None:
                            return {
                                "status": "ok",
                                "source": "espn",
                                "nyk": {"spread": nyk_spread, "ml": nyk_ml},
                                "cle": {"spread": cle_spread, "ml": cle_ml},
                                "total": ou_total,
                            }
    except Exception as e:
        print(f"ESPN odds error: {e}")

    # Final fallback — tonight's opening lines (update each game)
    return {
        "status": "fallback",
        "nyk": {"spread": 2.5,  "ml": 110},
        "cle": {"spread": -2.5, "ml": -130},
        "total": 214.5,
    }
