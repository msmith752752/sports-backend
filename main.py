from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from nba_api.live.nba.endpoints import scoreboard, playbyplay

app = FastAPI()

# Allow frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"status": "Sports Terminal Backend Running"}

@app.get("/game-feed")
def get_game_feed():
    data = scoreboard.ScoreBoard().get_dict()

    games = data["scoreboard"]["games"]

    if not games:
        return {"status": "no_games"}

    game = games[0]  # take first game (we'll refine later)

    game_id = game["gameId"]

    home_team = game["homeTeam"]["teamName"]
    away_team = game["awayTeam"]["teamName"]

    home_score = game["homeTeam"]["score"]
    away_score = game["awayTeam"]["score"]

    period = game["period"]
    clock = game["gameClock"]

    # 👉 PLAY-BY-PLAY
    last_play = None
    action_team = None

    try:
        pbp = playbyplay.PlayByPlay(game_id=game_id).get_dict()
        actions = pbp.get("game", {}).get("actions", [])

        if actions:
            latest = actions[-1]

            last_play = latest.get("description", "")

            # Determine which team acted
            if latest.get("teamId") == game["homeTeam"]["teamId"]:
                action_team = "home"
            elif latest.get("teamId") == game["awayTeam"]["teamId"]:
                action_team = "away"

    except Exception:
        last_play = None
        action_team = None

    return {
        "homeTeam": home_team,
        "awayTeam": away_team,
        "homeScore": home_score,
        "awayScore": away_score,
        "period": period,
        "clock": clock,
        "lastPlay": last_play,
        "actionTeam": action_team
    }