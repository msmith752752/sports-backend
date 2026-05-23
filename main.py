from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import requests
import httpx
import asyncio
from collections import defaultdict
from datetime import datetime, timezone

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

# ─── CONFIG ──────────────────────────────────────────────────────────────────
# Drop your keys here before tip-off
YOUTUBE_API_KEY      = "AIzaSyAF-XtYeVTXw1lDTPzyRxElyNdMqABmrfM"
YOUTUBE_LIVE_CHAT_ID = "Cg0KCzlQM3NEOUwzVUdzKicKGFVDQ1l0OTI3QlB6THJleEhDN0YtcktDZxILOVAzc0Q5TDNVR3M"
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

    # Target Thunder vs Spurs specifically
    game = None
    for g in games:
        teams = [g["homeTeam"]["teamName"], g["awayTeam"]["teamName"]]
        if "Thunder" in teams or "Spurs" in teams:
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
        "Gilgeous-Alexander", "Holmgren", "Williams", "Wallace", "Dort",
        "Wembanyama", "Johnson", "Castle", "Jones", "Vassell"
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
