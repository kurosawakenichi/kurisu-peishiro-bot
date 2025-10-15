import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True  # SERVER MEMBERS INTENT
bot = commands.Bot(command_prefix="!", intents=intents)

# ----- データ管理 -----
players = {}  # {user_id: {"pt": int, "challenge": bool}}
matches = {}  # { (challenger_id, opponent_id): {"approved": bool} }
event = {"start": None, "end": None, "active": False}

# 階級定義
RANKS = [
    {"name": "Beginner", "min": 0, "max": 4, "icon": "🔰"},
    {"name": "Silver", "min": 5, "max": 9, "icon": "🥈"},
    {"name": "Gold", "min": 10, "max": 14, "icon": "🥇"},
    {"name": "Master", "min": 15, "max": 19, "icon": "⚔️"},
    {"name": "GroundMaster", "min": 20, "max": 24, "icon": "🪽"},
    {"name": "Challenger", "min": 25, "max": 999, "icon": "😈"}
]

def get_rank(pt):
    for rank in RANKS:
        if rank["min"] <= pt <= rank["max"]:
            return rank
    return RANKS[0]

async def update_member_display(member: discord.Member):
    """ユーザー名にptと階級アイコンを反映"""
    info = players.get(member.id)
    if info:
        rank = get_rank(info["pt"])
        challenge_icon = "🔥" if info["challenge"] else ""
        new_name = f"{member.display_name.split(' ')[0]} {rank['icon']}{challenge_icon}{info['pt']}"
        try:
            await member.edit(nick=new_name)
        except:
            pass  # 権限がない場合は無視

# ----- コマンド同期 -----
@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    print("[INFO] ギルドコマンド同期完了")
    ranking_post.start()

# ----- イベント管理 -----
@bot.tree.command(name="イベント設定", description="イベント開始/終了日時を設定", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(start="開始日時 YYYY-MM-DD HH:MM", end="終了日時 YYYY-MM-DD HH:MM")
async def set_event(interaction: discord.Interaction, start: str, end: str):
    try:
        event["start"] = datetime.strptime(start, "%Y-%m-%d %H:%M")
        event["end"] = datetime.strptime(end, "%Y-%m-%d %H:%M")
        event["active"] = True
        await interaction.response.send_message(f"イベント設定完了:\n開始: {event['start']}\n終了: {event['end']}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"日時形式エラー: {e}", ephemeral=True)

# ----- マッチング申請 -----
@bot.tree.command(name="マッチング申請", description="対戦相手に申請", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="対戦相手")
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    if not event["active"]:
        await interaction.response.send_message("イベントが設定されていません。", ephemeral=True)
        return
    if (interaction.user.id, opponent.id) in matches:
        await interaction.response.send_message("すでに申請済みです。", ephemeral=True)
        return
    matches[(interaction.user.id, opponent.id)] = {"approved": False}
    # 承認ボタン付きメッセージ送信
    view = discord.ui.View()
    async def approve_callback(button_interaction: discord.Interaction):
        matches[(interaction.user.id, opponent.id)]["approved"] = True
        await button_interaction.response.send_message("承認されました！", ephemeral=True)
    button = discord.ui.Button(label="承認", style=discord.ButtonStyle.green)
    button.callback = approve_callback
    view.add_item(button)
    await interaction.response.send_message(f"{opponent.mention} にマッチング申請しました。承認を待ってください。", view=view, ephemeral=True)

# ----- 試合結果報告 -----
@bot.tree.command(name="試合結果報告", description="勝者が報告", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="対戦相手")
async def report_result(interaction: discord.Interaction, opponent: discord.Member):
    key = (interaction.user.id, opponent.id)
    match = matches.get(key)
    if not match or not match["approved"]:
        await interaction.response.send_message("マッチング承認が完了していません。", ephemeral=True)
        return
    await interaction.response.send_message("承認待ちです…", ephemeral=True)
    # バックグラウンドで pt 更新
    async def update_pt():
        # プレイヤー情報初期化
        for uid in [interaction.user.id, opponent.id]:
            if uid not in players:
                players[uid] = {"pt":0, "challenge":False}
        winner_id = interaction.user.id
        loser_id = opponent.id
        winner_info = players[winner_id]
        loser_info = players[loser_id]
        # 同階級判定
        winner_rank = get_rank(winner_info["pt"])
        loser_rank = get_rank(loser_info["pt"])
        diff = (loser_rank["min"] - winner_rank["min"]) // 5
        # Pt計算
        if diff >= 3:
            await interaction.followup.send(f"マッチング不可の差があります。", ephemeral=True)
            return
        # 勝者+1〜階級差補正
        gain = 1 + max(diff,0)
        # 敗者-1〜階級差補正
        loss = -1 - max(-diff,0)
        winner_info["pt"] += gain
        loser_info["pt"] = max(0, loser_info["pt"] + loss)
        # 昇級チャレンジ判定
        for uid in [winner_id, loser_id]:
            info = players[uid]
            rank = get_rank(info["pt"])
            info["challenge"] = False
            for r in RANKS[:-1]:
                if info["pt"] == r["max"]:
                    info["challenge"] = True
        # ユーザー名更新
        guild = bot.get_guild(GUILD_ID)
        for uid in [winner_id, loser_id]:
            member = guild.get_member(uid)
            if member:
                await update_member_display(member)
        # ランキング投稿
        await post_ranking()
    asyncio.create_task(update_pt())

# ----- ランキング投稿 -----
ranking_channel_id = 1427542200614387846  # #ランキングチャンネルIDをセット

async def post_ranking():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(ranking_channel_id)
    if not ch:
        return
    text = "**ランキング**\n"
    sorted_players = sorted(players.items(), key=lambda x:x[1]["pt"], reverse=True)
    for uid, info in sorted_players:
        rank = get_rank(info["pt"])
        challenge_icon = "🔥" if info["challenge"] else ""
        member = guild.get_member(uid)
        name = member.display_name if member else str(uid)
        text += f"{rank['icon']}{challenge_icon}{info['pt']} {name}\n"
    await ch.send(text)

@tasks.loop(minutes=5)
async def ranking_post():
    await post_ranking()

bot.run(TOKEN)
