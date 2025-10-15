# main.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
from datetime import datetime, timedelta

# -------------------
# 環境変数
# -------------------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

# -------------------
# Intents
# -------------------
intents = discord.Intents.default()
intents.members = True  # メンバー情報取得用

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# -------------------
# プレイヤーデータ
# -------------------
PLAYERS_FILE = "players.json"

if os.path.exists(PLAYERS_FILE):
    with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
        players = json.load(f)
else:
    players = {}

# -------------------
# 階級定義
# -------------------
RANKS = [
    (0, 4, "Beginner🔰"),
    (5, 9, "Silver🥈"),
    (10, 14, "Gold🥇"),
    (15, 19, "Master⚔️"),
    (20, 24, "GroundMaster🪽"),
    (25, float("inf"), "Challenger😈"),
]

# -------------------
# グローバル変数
# -------------------
event_start = None
event_end = None
pending_matches = {}  # 承認待ちのマッチング

# -------------------
# ユーティリティ
# -------------------
def get_rank(pt):
    for min_pt, max_pt, name in RANKS:
        if min_pt <= pt <= max_pt:
            return name
    return "Unknown"

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if not member:
        return
    data = players.get(str(user_id), {"pt": 0, "challenge": False})
    rank_name = get_rank(data["pt"])
    challenge_icon = "🔥" if data.get("challenge", False) else ""
    new_nick = f"{rank_name}{challenge_icon} {data['pt']}pt"
    try:
        await member.edit(nick=new_nick)
    except Exception as e:
        print(f"Failed to update nickname for {member}: {e}")

def save_players():
    with open(PLAYERS_FILE, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)

# -------------------
# 定期ランキング投稿
# -------------------
@tasks.loop(minutes=1)
async def ranking_post_loop():
    if event_start is None or event_end is None:
        return
    now = datetime.now()
    if now.hour in [14, 22] and now.minute == 0:
        guild = bot.get_guild(GUILD_ID)
        channel = discord.utils.get(guild.text_channels, name="ランキング")
        if not channel:
            return
        ranking = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
        msg = "**現在のランキング**\n"
        for idx, (uid, data) in enumerate(ranking, start=1):
            rank_name = get_rank(data["pt"])
            challenge_icon = "🔥" if data.get("challenge", False) else ""
            member = guild.get_member(int(uid))
            if member:
                msg += f"{idx}. {challenge_icon}{rank_name} {member.display_name} ({data['pt']}pt)\n"
        await channel.send(msg)

# -------------------
# on_ready
# -------------------
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    try:
        synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    ranking_post_loop.start()

# -------------------
# /イベント設定
# -------------------
@tree.command(name="イベント設定", description="イベント期間を設定", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(start="開始日時 (例: 2025-10-15T14:00)", end="終了日時 (例: 2025-10-16T22:00)")
async def event_setup(interaction: discord.Interaction, start: str, end: str):
    global event_start, event_end
    try:
        event_start = datetime.fromisoformat(start)
        event_end = datetime.fromisoformat(end)
        await interaction.response.send_message(f"イベント期間を設定しました: {start} ～ {end}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"日時の形式が不正です: {e}", ephemeral=True)

# -------------------
# /マッチング申請
# -------------------
@tree.command(name="マッチング申請", description="対戦相手にマッチング申請", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="対戦相手")
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    uid = str(interaction.user.id)
    oid = str(opponent.id)
    key = tuple(sorted([uid, oid]))
    if key in pending_matches:
        await interaction.response.send_message("既に承認待ちの申請があります。取り下げてください。", ephemeral=True)
        return
    pending_matches[key] = {"requester": uid, "approved": False}
    await interaction.response.send_message(f"{opponent.mention} に承認を依頼しました。", ephemeral=True)

# -------------------
# /承認
# -------------------
@tree.command(name="承認", description="マッチング申請を承認", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="申請者")
async def approve(interaction: discord.Interaction, opponent: discord.Member):
    uid = str(interaction.user.id)
    oid = str(opponent.id)
    key = tuple(sorted([uid, oid]))
    match = pending_matches.get(key)
    if not match or match["approved"]:
        await interaction.response.send_message("承認できる申請がありません。", ephemeral=True)
        return
    if match["requester"] == uid:
        await interaction.response.send_message("自分の申請は承認できません。", ephemeral=True)
        return
    match["approved"] = True
    await interaction.response.send_message("承認しました。", ephemeral=True)

# -------------------
# /試合結果報告
# -------------------
@tree.command(name="試合結果報告", description="勝者が試合結果を報告", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="対戦相手")
async def report(interaction: discord.Interaction, opponent: discord.Member):
    uid = str(interaction.user.id)
    oid = str(opponent.id)
    key = tuple(sorted([uid, oid]))
    match = pending_matches.get(key)
    if not match or not match["approved"]:
        await interaction.response.send_message(f"事前にマッチング申請が承認されていません。\n@kurosawa0118 までご報告ください", ephemeral=True)
        return

    # プレイヤーデータ初期化
    for id_ in [uid, oid]:
        if id_ not in players:
            players[id_] = {"pt":0, "challenge":False}

    # Pt計算（簡易）
    players[uid]["pt"] += 1
    if players[oid]["pt"] > 0:
        players[oid]["pt"] -= 1
    save_players()

    await update_member_display(int(uid))
    await update_member_display(int(oid))

    # 昇級通知
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="ランキング")
    if channel:
        rank_name = get_rank(players[uid]["pt"])
        challenge_icon = "🔥" if players[uid].get("challenge", False) else ""
        await channel.send(f"{challenge_icon} <@{uid}> が昇級しました！ {rank_name} {players[uid]['pt']}pt")

    del pending_matches[key]
    await interaction.response.send_message("試合結果を反映しました。", ephemeral=True)

# -------------------
# Bot 起動
# -------------------
bot.run(TOKEN)
