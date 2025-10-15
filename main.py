import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- データ管理 ---
players = {}  # {user_id: {"pt": int, "rank": str, "challenge": bool}}

RANKS = [
    (0, 4, "Beginner"),
    (5, 9, "Silver"),
    (10, 14, "Gold"),
    (15, 19, "Master"),
    (20, 24, "GroundMaster"),
    (25, float("inf"), "Challenger")
]

RANK_EMOJI = {
    "Beginner": "🔰",
    "Silver": "🥈",
    "Gold": "🥇",
    "Master": "⚔️",
    "GroundMaster": "🪽",
    "Challenger": "😈"
}

# --- イベント管理 ---
event_start = None
event_end = None

# --- マッチング管理 ---
pending_matches = {}  # {winner_id: {"loser_id": id, "approved": bool}}

# --- ユーティリティ ---
def get_rank(pt: int):
    for low, high, name in RANKS:
        if low <= pt <= high:
            return name
    return "Unknown"

async def update_member_display(user_id: int):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if not member:
        return
    pt = players[user_id]["pt"]
    rank = get_rank(pt)
    challenge = "🔥" if players[user_id].get("challenge", False) else ""
    display_name = f"{member.name} {RANK_EMOJI[rank]}{challenge} ({pt}pt)"
    try:
        await member.edit(nick=display_name)
    except discord.Forbidden:
        pass

# --- on_ready ---
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await tree.sync(guild=guild)
        print("ギルドコマンド同期完了")
    # ランキング定期投稿開始
    post_ranking.start()

# --- ランキング定期投稿 ---
@tasks.loop(minutes=10)
async def post_ranking():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = discord.utils.get(guild.text_channels, name="ランキング")
    if not channel:
        return
    sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    msg = "**ランキング**\n"
    for uid, data in sorted_players[:10]:
        rank = get_rank(data["pt"])
        msg += f"<@{uid}> {RANK_EMOJI[rank]} ({data['pt']}pt)\n"
    await channel.send(msg)

# --- スラッシュコマンド ---
@tree.command(name="イベント設定", description="イベント開始・終了日時を設定")
@app_commands.describe(start="開始日時 (YYYY-MM-DD HH:MM)", end="終了日時 (YYYY-MM-DD HH:MM)")
async def set_event(interaction: discord.Interaction, start: str, end: str):
    global event_start, event_end
    try:
        event_start = datetime.strptime(start, "%Y-%m-%d %H:%M")
        event_end = datetime.strptime(end, "%Y-%m-%d %H:%M")
        await interaction.response.send_message(f"イベント期間を設定しました: {start} 〜 {end}")
    except Exception as e:
        await interaction.response.send_message(f"日時の形式が不正です: {e}")

@tree.command(name="マッチング申請", description="試合申請")
@app_commands.describe(opponent="対戦相手")
async def matching_request(interaction: discord.Interaction, opponent: discord.Member):
    uid = interaction.user.id
    opp_id = opponent.id
    if uid not in players:
        players[uid] = {"pt": 0, "challenge": False}
    if opp_id not in players:
        players[opp_id] = {"pt": 0, "challenge": False}
    # 同階級などの条件はここで確認可能
    pending_matches[uid] = {"loser_id": opp_id, "approved": False}
    await interaction.response.send_message(f"{interaction.user.mention} が {opponent.mention} に対戦申請しました。承認待ちです。")

@tree.command(name="試合結果報告", description="勝者が試合結果を報告")
@app_commands.describe(loser="敗者")
async def report_result(interaction: discord.Interaction, loser: discord.Member):
    winner_id = interaction.user.id
    loser_id = loser.id
    match = pending_matches.get(winner_id)
    if not match or match["loser_id"] != loser_id or not match["approved"]:
        await interaction.response.send_message(f"事前にマッチング申請が承認されていません。@kurosawa0118 にご報告ください。")
        return
    # Pt計算
    players[winner_id]["pt"] += 1
    if players[winner_id]["pt"] in [4,9,14,19,24]:
        players[winner_id]["challenge"] = True
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    del pending_matches[winner_id]
    guild = bot.get_guild(GUILD_ID)
    rank = get_rank(players[winner_id]["pt"])
    channel = discord.utils.get(guild.text_channels, name="ランキング")
    if channel:
        await channel.send(f"🔥 <@{winner_id}> が昇級しました！ {RANK_EMOJI[rank]}")

# --- ボット起動 ---
bot.run(TOKEN)
