import os
import json
import asyncio
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

DATA_FILE = "players.json"

# 階級とアイコン
RANK_EMOJI = {
    "Beginner": "🔰",
    "Silver": "🥈",
    "Gold": "🥇",
    "Master": "⚔️",
    "GroundMaster": "🪽",
    "Challenger": "😈",
}

# プレイヤーデータ
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        players = json.load(f)
else:
    players = {}

def save_players():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)

def get_rank(pt):
    if pt >= 25:
        return "Challenger"
    elif pt >= 20:
        return "GroundMaster"
    elif pt >= 15:
        return "Master"
    elif pt >= 10:
        return "Gold"
    elif pt >= 5:
        return "Silver"
    else:
        return "Beginner"

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(int(user_id))
    if not member or str(user_id) not in players:
        return
    data = players[str(user_id)]
    rank = data["rank"]
    challenge_icon = "🔥" if data.get("challenge", False) else ""
    new_nick = f"{rank}{RANK_EMOJI[rank]}{challenge_icon} {member.name}"
    try:
        await member.edit(nick=new_nick)
    except Exception as e:
        print(f"ニックネーム更新失敗: {e}")

# イベント情報
event_start = None
event_end = None

# マッチング管理
# match_requests: { "勝者ID": {"敗者ID":承認済/未承認/拒否} }
match_requests = {}

# ランキング投稿チャンネル名
RANKING_CHANNEL_NAME = "ランキング"

# ----------------------------
# 起動時ギルドコマンド同期
# ----------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    guild = discord.Object(id=GUILD_ID)
    # 既存コマンド消す場合
    # await bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    print("ギルドにコマンド同期完了")
    ranking_auto_post.start()

# ----------------------------
# イベント設定コマンド
# ----------------------------
@bot.tree.command(name="イベント設定", description="イベント開始/終了日時設定", guild=discord.Object(id=GUILD_ID))
async def event_setup(interaction: discord.Interaction, start: str, end: str):
    global event_start, event_end
    try:
        event_start = datetime.strptime(start, "%Y-%m-%d %H:%M")
        event_end = datetime.strptime(end, "%Y-%m-%d %H:%M")
        await interaction.response.send_message(f"イベント開始: {event_start}, 終了: {event_end}")
    except Exception as e:
        await interaction.response.send_message(f"日時の形式が不正です。YYYY-MM-DD HH:MM で入力してください。\n{e}")

# ----------------------------
# マッチング申請コマンド
# ----------------------------
@bot.tree.command(name="マッチング申請", description="対戦相手にマッチング申請", guild=discord.Object(id=GUILD_ID))
async def matching_request(interaction: discord.Interaction, opponent: discord.Member):
    winner_id = str(interaction.user.id)
    loser_id = str(opponent.id)
    if winner_id == loser_id:
        await interaction.response.send_message("自分自身には申請できません。")
        return
    # 重複チェック
    if winner_id in match_requests and loser_id in match_requests[winner_id]:
        await interaction.response.send_message("すでに申請済みです。")
        return
    # 新規申請登録
    match_requests.setdefault(winner_id, {})[loser_id] = "未承認"
    await interaction.response.send_message(f"{opponent.mention} にマッチング申請しました。相手が承認すると試合を開始できます。")

# ----------------------------
# 承認コマンド
# ----------------------------
@bot.tree.command(name="承認", description="対戦申請承認", guild=discord.Object(id=GUILD_ID))
async def approve(interaction: discord.Interaction, requester: discord.Member):
    loser_id = str(interaction.user.id)
    winner_id = str(requester.id)
    if winner_id in match_requests and loser_id in match_requests[winner_id]:
        if match_requests[winner_id][loser_id] != "未承認":
            await interaction.response.send_message("すでに処理済みです。")
            return
        match_requests[winner_id][loser_id] = "承認"
        await interaction.response.send_message("承認しました。勝者が試合後に /試合結果報告 を入力してください。")
    else:
        await interaction.response.send_message("申請が見つかりません。")

# ----------------------------
# 拒否コマンド
# ----------------------------
@bot.tree.command(name="拒否", description="対戦申請拒否", guild=discord.Object(id=GUILD_ID))
async def deny(interaction: discord.Interaction, requester: discord.Member):
    loser_id = str(interaction.user.id)
    winner_id = str(requester.id)
    if winner_id in match_requests and loser_id in match_requests[winner_id]:
        match_requests[winner_id][loser_id] = "拒否"
        await interaction.response.send_message("拒否しました。")
    else:
        await interaction.response.send_message("申請が見つかりません。")

# ----------------------------
# 試合結果報告コマンド
# ----------------------------
@bot.tree.command(name="試合結果報告", description="勝者が試合結果を報告", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner_id = str(interaction.user.id)
    loser_id = str(opponent.id)
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name=RANKING_CHANNEL_NAME)
    if winner_id not in match_requests or loser_id not in match_requests[winner_id]:
        await interaction.response.send_message(f"事前にマッチング申請・承認が必要です。@kurosawa0118 に報告してください。")
        return
    if match_requests[winner_id][loser_id] != "承認":
        await interaction.response.send_message(f"まだ承認されていません。@kurosawa0118 に報告してください。")
        return
    # Pt計算例（単純化）
    winner_data = players.setdefault(winner_id, {"pt": 0, "rank": get_rank(0), "challenge": False})
    loser_data = players.setdefault(loser_id, {"pt": 0, "rank": get_rank(0), "challenge": False})
    # 階級差によるPt加減算
    rank_order = ["Beginner","Silver","Gold","Master","GroundMaster","Challenger"]
    winner_rank_idx = rank_order.index(winner_data["rank"])
    loser_rank_idx = rank_order.index(loser_data["rank"])
    diff = abs(winner_rank_idx - loser_rank_idx)
    # 勝者Pt
    winner_pt_add = 1 + diff if winner_rank_idx < loser_rank_idx else 1
    winner_data["pt"] += winner_pt_add
    winner_data["rank"] = get_rank(winner_data["pt"])
    # 昇格チャレンジ設定
    for threshold in [4,9,14,19,24]:
        if winner_data["pt"] == threshold:
            winner_data["challenge"] = True
    # 敗者Pt
    if loser_data["pt"] > 0:
        loser_pt_sub = 1 + diff if loser_rank_idx > winner_rank_idx else 1
        loser_data["pt"] = max(loser_data["pt"] - loser_pt_sub, 0)
        loser_data["rank"] = get_rank(loser_data["pt"])
        if loser_data["pt"] < 25:
            loser_data["challenge"] = False
    save_players()
    # ユーザー名更新
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    # 昇格アナウンス
    challenge_icon = "🔥" if winner_data.get("challenge", False) else ""
    if channel:
        await channel.send(f"🔥 <@{winner_id}> が昇級しました！ {winner_data['rank']}{RANK_EMOJI[winner_data['rank']]}{challenge_icon}")

    # 申請削除
    del match_requests[winner_id][loser_id]
    await interaction.response.send_message("結果を反映しました。")

# ----------------------------
# ランキング定期投稿
# ----------------------------
@tasks.loop(hours=8)
async def ranking_auto_post():
    if not event_start or not event_end:
        return
    now = datetime.now()
    if now < event_start or now > event_end:
        return
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name=RANKING_CHANNEL_NAME)
    if not channel:
        return
    sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    msg = "🏆 ランキング\n"
    for uid, data in sorted_players:
        challenge_icon = "🔥" if data.get("challenge", False) else ""
        msg += f"<@{uid}>: {data['pt']}pt {data['rank']}{RANK_EMOJI[data['rank']]}{challenge_icon}\n"
    await channel.send(msg)

# ----------------------------
bot.run(TOKEN)
