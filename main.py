import os
import asyncio
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True  # SERVER MEMBERS INTENT
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# プレイヤーデータ格納
players = {}  # user_id : {"pt":int, "challenge":bool}

# 階級定義
RANKS = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, 9999, "Challenger", "😈")
]

EVENT_START = None
EVENT_END = None

# ---------- ヘルパー ----------

def get_rank(pt):
    for low, high, name, emoji in RANKS:
        if low <= pt <= high:
            return name, emoji
    return "Unknown", ""

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member is None:
        return
    pt = players[user_id]["pt"]
    rank_name, rank_emoji = get_rank(pt)
    challenge = "🔥" if players[user_id]["challenge"] else ""
    try:
        # ニックネームに pt と階級を表示
        await member.edit(nick=f"{member.name} {rank_emoji}{rank_name} {pt}{challenge}")
    except:
        pass
    # ロール更新
    rank_role = discord.utils.get(guild.roles, name=rank_name)
    if rank_role:
        # 既存のランクロール削除
        for low, high, name, emoji in RANKS:
            old_role = discord.utils.get(guild.roles, name=name)
            if old_role and old_role in member.roles and old_role != rank_role:
                await member.remove_roles(old_role)
        if rank_role not in member.roles:
            await member.add_roles(rank_role)

def calc_pt(winner_id, loser_id):
    winner = players[winner_id]
    loser = players[loser_id]
    winner_rank_name, _ = get_rank(winner["pt"])
    loser_rank_name, _ = get_rank(loser["pt"])
    winner_low, winner_high, _, _ = next(r for r in RANKS if r[2]==winner_rank_name)
    loser_low, loser_high, _, _ = next(r for r in RANKS if r[2]==loser_rank_name)
    diff = loser_low - winner_low
    # 勝者
    if diff >= 3:
        winner["pt"] += 1
    else:
        winner["pt"] += max(1, diff)
    # 敗者
    if loser["pt"] > 0:
        loser["pt"] = max(0, loser["pt"] - 1)
    # 昇級チャレンジ
    for user in [winner_id, loser_id]:
        user_data = players[user]
        for low, high, name, emoji in RANKS:
            if user_data["pt"] in [low-1 for low,_h,_n,_e in RANKS[1:]]:  # 昇級チャレンジ開始pt
                user_data["challenge"] = True
                break
            else:
                user_data["challenge"] = False

# ---------- コマンド ----------

@tree.command(name="イベント設定", description="イベント開始・終了日時設定", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(start="開始日時 YYYY-MM-DD HH:MM", end="終了日時 YYYY-MM-DD HH:MM")
async def event_setup(interaction: discord.Interaction, start: str, end: str):
    global EVENT_START, EVENT_END
    try:
        EVENT_START = datetime.strptime(start, "%Y-%m-%d %H:%M")
        EVENT_END = datetime.strptime(end, "%Y-%m-%d %H:%M")
        await interaction.response.send_message(f"イベント設定完了: {EVENT_START}〜{EVENT_END}")
    except Exception as e:
        await interaction.response.send_message(f"日時形式エラー: {e}")

@tree.command(name="マッチング申請", description="試合申請", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="対戦相手")
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    if interaction.user.id not in players:
        players[interaction.user.id] = {"pt":0,"challenge":False}
    if opponent.id not in players:
        players[opponent.id] = {"pt":0,"challenge":False}
    await interaction.response.send_message(f"{interaction.user.mention} が {opponent.mention} にマッチング申請を送りました。")

@tree.command(name="試合結果報告", description="勝者が報告", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(winner="勝者", loser="敗者")
async def report_result(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    if winner.id not in players or loser.id not in players:
        await interaction.response.send_message("試合申請が承認されていません。")
        return
    calc_pt(winner.id, loser.id)
    await update_member_display(winner.id)
    await update_member_display(loser.id)
    await interaction.response.send_message(f"結果反映完了: {winner.mention} 勝利、{loser.mention} 敗北")

# ---------- 定期ランキング投稿 ----------

@tasks.loop(minutes=30)
async def post_ranking():
    channel = bot.get_channel(int(os.environ.get("RANKING_CHANNEL_ID", 0)))
    if channel is None:
        return
    msg = "=== ランキング ===\n"
    for uid, data in sorted(players.items(), key=lambda x: -x[1]["pt"]):
        rank_name, rank_emoji = get_rank(data["pt"])
        challenge = "🔥" if data["challenge"] else ""
        member = bot.get_guild(GUILD_ID).get_member(uid)
        if member:
            msg += f"{member.display_name}: {rank_emoji}{rank_name} {data['pt']}{challenge}\n"
    await channel.send(msg)

# ---------- on_ready ----------

@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("[ERROR] ギルドが取得できません")
        return
    try:
        print("[INFO] ギルドコマンド全削除＆再同期中...")
        await tree.clear_commands(guild=guild)
        # 同期
        await tree.sync(guild=guild)
        print("[INFO] ギルドコマンド同期完了")
    except Exception as e:
        print("[ERROR] コマンド同期中にエラー発生:", e)
    post_ranking.start()
    print(f"✅ {bot.user} が起動しました。")

bot.run(TOKEN)
