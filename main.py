import os
import json
import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # 権限警告が出ますが一応必要

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# データ格納用
DATA_FILE = "players.json"
players = {}

# 階級設定
RANKS = [
    {"name": "Beginner", "emoji": "🔰", "min": 0, "max": 4},
    {"name": "Silver", "emoji": "🥈", "min": 5, "max": 9},
    {"name": "Gold", "emoji": "🥇", "min": 10, "max": 14},
    {"name": "Master", "emoji": "⚔️", "min": 15, "max": 19},
    {"name": "GroundMaster", "emoji": "🪽", "min": 20, "max": 24},
    {"name": "Challenger", "emoji": "😈", "min": 25, "max": 9999},
]

# 定期ランキング用
ranking_channel_id = None  # /イベント設定で指定予定

def load_data():
    global players
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            players = json.load(f)
    except FileNotFoundError:
        players = {}

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)

def get_rank_info(pt):
    for r in RANKS:
        if r["min"] <= pt <= r["max"]:
            return r
    return RANKS[-1]

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(int(user_id))
    if member:
        pt = players[user_id]["pt"]
        challenge = "🔥" if players[user_id].get("challenge", False) else ""
        rank = get_rank_info(pt)
        display_name = f"{rank['emoji']}{challenge} {member.name} ({pt}pt)"
        try:
            await member.edit(nick=display_name)
        except:
            pass

@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    guild = discord.Object(id=GUILD_ID)
    await tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    print("ギルドにコマンド強制同期完了")
    load_data()
    ranking_loop.start()

# --- /イベント設定 ---
@tree.command(name="イベント設定", description="イベント用のランキングチャンネルと開始日時を設定", guild=discord.Object(id=GUILD_ID))
async def set_event(interaction: discord.Interaction, channel: discord.TextChannel):
    global ranking_channel_id
    ranking_channel_id = channel.id
    await interaction.response.send_message(f"ランキング投稿チャンネルを {channel.mention} に設定しました。")

# --- ランキング自動投稿 ---
@tasks.loop(minutes=1)
async def ranking_loop():
    if not ranking_channel_id:
        return
    now = datetime.utcnow()
    if now.hour in [5, 13]:  # UTC 5:00/13:00 = JST 14:00/22:00
        channel = bot.get_channel(ranking_channel_id)
        if channel:
            sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
            msg = "🔥 **ランキング** 🔥\n"
            for uid, data in sorted_players[:10]:
                rank = get_rank_info(data["pt"])
                challenge = "🔥" if data.get("challenge", False) else ""
                member = bot.get_guild(GUILD_ID).get_member(int(uid))
                name = member.name if member else uid
                msg += f"{rank['emoji']}{challenge} {name} ({data['pt']}pt)\n"
            await channel.send(msg)

# --- JSON読み書きを反映する共通関数 ---
def add_player_if_missing(user_id):
    if user_id not in players:
        players[user_id] = {"pt": 0, "challenge": False}

# --- /マッチング申請 ---
@tree.command(name="マッチング申請", description="対戦相手とマッチング申請", guild=discord.Object(id=GUILD_ID))
async def matching(interaction: discord.Interaction, opponent: discord.Member):
    add_player_if_missing(str(interaction.user.id))
    add_player_if_missing(str(opponent.id))
    # 重複チェック
    if players[str(interaction.user.id)].get("pending_opponent") == str(opponent.id):
        await interaction.response.send_message("すでに申請済です。")
        return
    players[str(interaction.user.id)]["pending_opponent"] = str(opponent.id)
    save_data()
    await interaction.response.send_message(f"{interaction.user.mention} が {opponent.mention} にマッチング申請しました。相手が /承認 または /拒否 してください。")

# --- /承認 ---
@tree.command(name="承認", description="マッチング申請を承認", guild=discord.Object(id=GUILD_ID))
async def approve(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    for pid, pdata in players.items():
        if pdata.get("pending_opponent") == uid:
            pdata["match_approved"] = True
            pdata.pop("pending_opponent")
            save_data()
            await interaction.response.send_message(f"マッチング申請を承認しました。{bot.get_user(int(pid)).mention}と対戦可能です。")
            return
    await interaction.response.send_message("承認するマッチング申請がありません。")

# --- /試合結果報告 ---
@tree.command(name="試合結果報告", description="勝者が試合結果を報告", guild=discord.Object(id=GUILD_ID))
async def report(interaction: discord.Interaction, winner: discord.Member):
    winner_id = str(winner.id)
    loser_id = None
    # マッチング承認済みか確認
    for uid, pdata in players.items():
        if pdata.get("match_approved") and (winner_id in [uid, pdata.get("pending_opponent")]):
            loser_id = pdata.get("pending_opponent") if uid == winner_id else uid
            break
    if not loser_id:
        await interaction.response.send_message(f"事前承認されていない対戦です。@kurosawa0118 に報告してください。")
        return
    # Pt計算例（簡略）
    players[winner_id]["pt"] += 1
    # 昇級チャレンジ簡略化
    if players[winner_id]["pt"] in [4, 9, 14, 19, 24]:
        players[winner_id]["challenge"] = True
    save_data()
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    # ランキングチャンネルにアナウンス
    if ranking_channel_id:
        channel = bot.get_channel(ranking_channel_id)
        rank = get_rank_info(players[winner_id]["pt"])
        challenge = "🔥" if players[winner_id].get("challenge", False) else ""
        await channel.send(f"{challenge} <@{winner_id}> が {rank['name']}{rank['emoji']} に昇級しました！")

# --- 実行 ---
bot.run(TOKEN)
