import os
import discord
from discord import app_commands
from discord.ext import tasks, commands
from datetime import datetime, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)
guild = discord.Object(id=GUILD_ID)

# プレイヤー情報メモリ管理
players = {}  # {user_id: {"pt": int, "challenge": False, "highest_pt": int}}

# 階級情報
ranks = [
    {"name": "Beginner", "emoji": "🔰", "min": 0},
    {"name": "Silver", "emoji": "🥈", "min": 5},
    {"name": "Gold", "emoji": "🥇", "min": 10},
    {"name": "Master", "emoji": "⚔️", "min": 15},
    {"name": "GroundMaster", "emoji": "🪽", "min": 20},
    {"name": "Challenger", "emoji": "😈", "min": 25},
]

# マッチング待機中
pending_matches = {}  # {challenger_id: target_id}

# イベント期間
event_start = None
event_end = None

# ランキング投稿チャンネルID
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID", 0))

# --------------------- ヘルパー ---------------------
def get_rank(pt):
    for r in reversed(ranks):
        if pt >= r["min"]:
            return r
    return ranks[0]

async def update_member_display(user_id):
    member = guild.get_member(user_id)
    if not member:
        return
    pt = players[user_id]["pt"]
    rank = get_rank(pt)
    challenge = "🔥" if players[user_id]["challenge"] else ""
    try:
        await member.edit(nick=f"{member.name} {rank['emoji']}{challenge} ({pt}pt)")
        # ロール更新もここで可能
    except:
        pass

# --------------------- コマンド ---------------------
@bot.tree.command(guild=guild, name="イベント設定")
@app_commands.describe(start="開始日時(YYYY-MM-DD HH:MM)", end="終了日時(YYYY-MM-DD HH:MM)")
async def event_set(interaction: discord.Interaction, start: str, end: str):
    global event_start, event_end
    try:
        event_start = datetime.strptime(start, "%Y-%m-%d %H:%M")
        event_end = datetime.strptime(end, "%Y-%m-%d %H:%M")
        await interaction.response.send_message(f"イベント期間を設定しました。\n開始: {event_start}\n終了: {event_end}")
    except:
        await interaction.response.send_message("日時の形式が正しくありません。YYYY-MM-DD HH:MM で入力してください。")

@bot.tree.command(guild=guild, name="マッチング申請")
@app_commands.describe(target="対戦相手")
async def match_request(interaction: discord.Interaction, target: discord.User):
    if interaction.user.id in pending_matches:
        await interaction.response.send_message("既に申請中の相手がいます。取り下げてから再度申請してください。")
        return
    pending_matches[interaction.user.id] = target.id
    await interaction.response.send_message(f"{target.mention} さんにマッチング申請しました。承認を待ってください。")

@bot.tree.command(guild=guild, name="承認")
@app_commands.describe(challenger="承認する申請者")
async def approve_match(interaction: discord.Interaction, challenger: discord.User):
    if challenger.id not in pending_matches or pending_matches[challenger.id] != interaction.user.id:
        await interaction.response.send_message("承認できる申請がありません。")
        return
    await interaction.response.send_message(f"{challenger.mention} vs {interaction.user.mention} の対戦を開始しました！")
    # 対戦開始
    pending_matches.pop(challenger.id, None)

@bot.tree.command(guild=guild, name="試合結果報告")
@app_commands.describe(winner="勝者", loser="敗者")
async def report_match(interaction: discord.Interaction, winner: discord.User, loser: discord.User):
    for uid in (winner.id, loser.id):
        if uid not in players:
            players[uid] = {"pt": 0, "challenge": False, "highest_pt": 0}

    # Pt増減
    players[winner.id]["pt"] += 1
    players[loser.id]["pt"] = max(0, players[loser.id]["pt"] -1)

    # 昇級チャレンジ
    for uid in (winner.id, loser.id):
        pt = players[uid]["pt"]
        if pt in [4,9,14,19,24]:
            players[uid]["challenge"] = True
        else:
            players[uid]["challenge"] = False

    # 更新表示
    for uid in (winner.id, loser.id):
        await update_member_display(uid)

    # ランキング投稿
    if RANKING_CHANNEL_ID:
        ch = bot.get_channel(RANKING_CHANNEL_ID)
        if ch:
            rank = get_rank(players[winner.id]["pt"])
            challenge = "🔥" if players[winner.id]["challenge"] else ""
            await ch.send(f"{winner.mention} が昇級しました！ {rank['name']}{rank['emoji']}{challenge} ({players[winner.id]['pt']}pt)")

    await interaction.response.send_message("結果を反映しました。")

# --------------------- ランキング定期投稿 ---------------------
@tasks.loop(minutes=30)
async def post_ranking():
    if not RANKING_CHANNEL_ID:
        return
    ch = bot.get_channel(RANKING_CHANNEL_ID)
    if not ch:
        return
    msg = "ランキング\n"
    sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    for uid, info in sorted_players:
        rank = get_rank(info["pt"])
        challenge = "🔥" if info["challenge"] else ""
        member = guild.get_member(uid)
        name = member.name if member else str(uid)
        msg += f"{rank['emoji']} {name}{challenge} ({info['pt']}pt)\n"
    await ch.send(msg)

# --------------------- 起動処理 ---------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    try:
        await bot.tree.sync(guild=guild)
        print("ギルドコマンド同期完了")
    except Exception as e:
        print(f"ギルドコマンド同期エラー: {e}")
    if not post_ranking.is_running():
        post_ranking.start()

bot.run(TOKEN)
