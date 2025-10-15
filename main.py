import os
import json
import asyncio
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta

# === 環境変数 ===
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

# === Intents 設定 ===
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# === データ格納ファイル ===
DATA_FILE = "players.json"

# === 階級設定 ===
RANKS = [
    {"name": "Beginner", "emoji": "🔰", "min_pt": 0},
    {"name": "Silver", "emoji": "🥈", "min_pt": 5},
    {"name": "Gold", "emoji": "🥇", "min_pt": 10},
    {"name": "Master", "emoji": "⚔️", "min_pt": 15},
    {"name": "GroundMaster", "emoji": "🪽", "min_pt": 20},
    {"name": "Challenger", "emoji": "😈", "min_pt": 25}
]

# === プレイヤーデータ読み込み/保存 ===
def load_players():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_players(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

players = load_players()
active_matches = {}  # { (challenger_id, opponent_id) : timestamp }

# === ヘルパー関数 ===
def get_rank(pt):
    for rank in reversed(RANKS):
        if pt >= rank["min_pt"]:
            return rank
    return RANKS[0]

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(int(user_id))
    if member is None:
        return
    pdata = players.get(str(user_id), {"pt":0, "challenge":False})
    rank = get_rank(pdata["pt"])
    challenge_icon = "🔥" if pdata.get("challenge") else ""
    try:
        new_nick = f"{member.name} | {rank['emoji']}{challenge_icon}{pdata['pt']}pt"
        if member.nick != new_nick:
            await member.edit(nick=new_nick)
    except discord.Forbidden:
        print(f"⚠️ 権限不足で {member} のニックネームを更新できません")

def pt_change(winner_pt, loser_pt, winner_rank_idx, loser_rank_idx):
    rank_diff = winner_rank_idx - loser_rank_idx
    # 同階級
    if rank_diff == 0:
        return 1, -1
    # 高い方が勝者
    elif rank_diff > 0:
        return 1, -1 - rank_diff
    else:  # 低い方が勝者
        return 1 - rank_diff, -1

# === イベントハンドラ ===
@bot.event
async def on_connect():
    print("[INFO] Bot が Discord に接続しました")

@bot.event
async def on_ready():
    print(f"[INFO] Bot is ready: {bot.user} (ID: {bot.user.id})")

# === /マッチング申請 ===
@bot.tree.command(name="マッチング申請")
async def matching_request(interaction: discord.Interaction, opponent: discord.User):
    challenger_id = str(interaction.user.id)
    opponent_id = str(opponent.id)

    # 重複申請不可
    if (challenger_id, opponent_id) in active_matches:
        await interaction.response.send_message("⚠️ 既にマッチング申請があります", ephemeral=True)
        return

    active_matches[(challenger_id, opponent_id)] = datetime.utcnow().timestamp()
    await interaction.response.send_message(
        f"{interaction.user.mention} が {opponent.mention} にマッチング申請しました！\n"
        "相手が /承認 または /拒否 で結果を承認してください", ephemeral=True
    )

# === /承認 /拒否 ===
@bot.tree.command(name="承認")
async def approve(interaction: discord.Interaction, challenger: discord.User):
    challenger_id = str(challenger.id)
    opponent_id = str(interaction.user.id)
    key = (challenger_id, opponent_id)
    if key not in active_matches:
        await interaction.response.send_message("⚠️ マッチング申請が見つかりません", ephemeral=True)
        return
    await interaction.response.send_message(f"{interaction.user.mention} が申請を承認しました！", ephemeral=True)

@bot.tree.command(name="拒否")
async def reject(interaction: discord.Interaction, challenger: discord.User):
    challenger_id = str(challenger.id)
    opponent_id = str(interaction.user.id)
    key = (challenger_id, opponent_id)
    if key not in active_matches:
        await interaction.response.send_message("⚠️ マッチング申請が見つかりません", ephemeral=True)
        return
    del active_matches[key]
    await interaction.response.send_message(f"{interaction.user.mention} が申請を拒否しました", ephemeral=True)

# === /試合結果報告 ===
@bot.tree.command(name="試合結果報告")
async def report(interaction: discord.Interaction, opponent: discord.User):
    winner_id = str(interaction.user.id)
    loser_id = str(opponent.id)
    key = (winner_id, loser_id)
    if key not in active_matches:
        await interaction.response.send_message(
            "⚠️ 事前にマッチング申請・承認が完了していません。\n"
            "問題がある場合は <@kurosawa0118> までご報告ください", ephemeral=True
        )
        return

    winner = players.get(winner_id, {"pt":0, "challenge":False})
    loser = players.get(loser_id, {"pt":0, "challenge":False})

    winner_rank_idx = next(i for i,r in enumerate(RANKS) if winner["pt"] >= r["min_pt"])
    loser_rank_idx = next(i for i,r in enumerate(RANKS) if loser["pt"] >= r["min_pt"])

    pt_win, pt_lose = pt_change(winner["pt"], loser["pt"], winner_rank_idx, loser_rank_idx)

    winner["pt"] += pt_win
    loser["pt"] = max(0, loser["pt"] + pt_lose)

    # 昇級チャレンジの判定
    for pdata in [winner, loser]:
        rank = get_rank(pdata["pt"])
        pdata["challenge"] = False
        for r in RANKS:
            if pdata["pt"] == r["min_pt"] - 1 and r != RANKS[0]:
                pdata["challenge"] = True

    players[winner_id] = winner
    players[loser_id] = loser
    save_players(players)

    await update_member_display(winner_id)
    await update_member_display(loser_id)
    del active_matches[key]

    guild = bot.get_guild(GUILD_ID)
    ranking_channel = discord.utils.get(guild.channels, name="ランキング")
    challenge_icon = "🔥" if winner["challenge"] else ""
    rank = get_rank(winner["pt"])
    if ranking_channel:
        await ranking_channel.send(f"{challenge_icon} <@{winner_id}> が {rank['name']}{rank['emoji']} に昇級しました！")
    await interaction.response.send_message(f"勝敗を反映しました。", ephemeral=True)

# === Bot 起動 ===
bot.run(TOKEN)
