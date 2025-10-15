import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import datetime

# ==============================
# 環境変数
# ==============================
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

# ==============================
# 設定
# ==============================
RANKING_CHANNEL_ID = 1427542200614387846  # #ランキング
REPORT_CHANNEL_ID = 1427542280578928750  # #対戦結果報告
ADMIN_USER_ID = 753868743779811368  # @クロサワ®
ADMIN_MENTION = "<@kurosawa0118>"

EVENT_START = datetime.datetime(2025, 10, 14, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
EVENT_END = datetime.datetime(2025, 10, 20, 23, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))

# ==============================
# Bot初期化
# ==============================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================
# データ管理
# ==============================
players = {}  # user_id: {"pt": int, "challenge": bool}
pending_matches = {}  # user_id: opponent_id
awaiting_results = {}  # winner_id: {"loser_id": int, "timer_task": asyncio.Task}

# ==============================
# 階級定義
# ==============================
RANKS = [
    ("Beginner", 0, 4, "🔰"),
    ("Silver", 5, 9, "🥈"),
    ("Gold", 10, 14, "🥇"),
    ("Master", 15, 19, "⚔️"),
    ("GroundMaster", 20, 24, "🪽"),
    ("Challenger", 25, 9999, "😈"),
]


def get_rank(pt):
    for name, low, high, emoji in RANKS:
        if low <= pt <= high:
            return name, emoji
    return "Beginner", "🔰"


def rank_difference(pt1, pt2):
    r1, _ = get_rank(pt1)
    r2, _ = get_rank(pt2)
    idx1 = next(i for i, r in enumerate(RANKS) if r[0] == r1)
    idx2 = next(i for i, r in enumerate(RANKS) if r[0] == r2)
    return idx1 - idx2


def event_active():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return EVENT_START <= now <= EVENT_END


async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if not member:
        return
    data = players.get(user_id, {"pt": 0, "challenge": False})
    rank_name, emoji = get_rank(data["pt"])
    challenge = "🔥" if data.get("challenge") else ""
    new_nick = f"{emoji}{challenge} {member.name} - {data['pt']}pt"
    try:
        await member.edit(nick=new_nick)
    except discord.Forbidden:
        pass  # 権限不足は無視


# ==============================
# 起動
# ==============================
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    try:
        await bot.tree.sync()
    except Exception as e:
        print(e)
    ranking_task.start()


# ==============================
# ランキング自動投稿
# ==============================
@tasks.loop(minutes=1)
async def ranking_task():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if now.minute != 0 or now.hour not in [14, 22]:
        return
    channel = bot.get_channel(RANKING_CHANNEL_ID)
    if not channel or not players:
        return
    sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    msg = f"🏆 **ランキング（{now.strftime('%H:%M')}）** 🏆\n"
    for i, (uid, data) in enumerate(sorted_players, start=1):
        rank, emoji = get_rank(data["pt"])
        challenge = "🔥" if data.get("challenge") else ""
        member = bot.get_user(uid)
        msg += f"{i}. {emoji}{challenge} {member.display_name} - {data['pt']}pt\n"
    await channel.send(msg)


# ==============================
# /マッチング申請
# ==============================
@bot.tree.command(name="マッチング申請", description="対戦相手に申請する")
@app_commands.describe(opponent="対戦相手")
async def matching_request(interaction: discord.Interaction, opponent: discord.Member):
    if not event_active():
        await interaction.response.send_message(
            f"⚠️イベント期間外です。{ADMIN_MENTION} にご報告ください。", ephemeral=True
        )
        return
    if interaction.user.id in pending_matches:
        await interaction.response.send_message("⚠️すでに申請中の対戦があります。", ephemeral=True)
        return
    pending_matches[interaction.user.id] = opponent.id
    await interaction.response.send_message(
        f"⚔️ {interaction.user.mention} が {opponent.mention} に対戦申請しました。\n"
        f"{opponent.mention} は `/承認` または `/拒否` で回答してください。"
    )


# ==============================
# /承認
# ==============================
@bot.tree.command(name="承認", description="対戦申請を承認する / 試合結果を承認する")
async def approve(interaction: discord.Interaction):
    user = interaction.user
    # マッチング承認
    opponent_id = next((uid for uid, oid in pending_matches.items() if oid == user.id), None)
    if opponent_id:
        opponent = bot.get_user(opponent_id)
        del pending_matches[opponent_id]
        awaiting_results[opponent_id] = {"loser_id": user.id, "timer_task": None}
        await interaction.response.send_message(f"✅ {user.mention} が {opponent.mention} の対戦申請を承認しました。")
        return
    # 勝者報告後の承認
    winner_id = next((wid for wid, info in awaiting_results.items() if info["loser_id"] == user.id), None)
    if winner_id:
        await finalize_match(winner_id, user.id)
        await interaction.response.send_message("✅ 対戦結果を承認しました。")
        return
    await interaction.response.send_message("承認待ち申請はありません。", ephemeral=True)


# ==============================
# /拒否
# ==============================
@bot.tree.command(name="拒否", description="対戦申請を拒否する")
async def reject(interaction: discord.Interaction):
    user = interaction.user
    opponent_id = next((uid for uid, oid in pending_matches.items() if oid == user.id), None)
    if opponent_id:
        opponent = bot.get_user(opponent_id)
        del pending_matches[opponent_id]
        await interaction.response.send_message(f"❌ {user.mention} が {opponent.mention} の申請を拒否しました。")
        return
    await interaction.response.send_message("辞退対象の申請はありません。", ephemeral=True)


# ==============================
# /試合結果報告
# ==============================
@bot.tree.command(name="試合結果報告", description="勝者が結果報告")
@app_commands.describe(opponent="対戦相手")
async def report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    if winner.id not in awaiting_results or awaiting_results[winner.id]["loser_id"] != opponent.id:
        await interaction.response.send_message(
            f"この対戦は成立していません。{ADMIN_MENTION} へご報告ください。", ephemeral=True
        )
        return

    # 15分承認待ち
    async def auto_finalize():
        await asyncio.sleep(900)
        if winner.id in awaiting_results:
            await finalize_match(winner.id, opponent.id)
            chan = bot.get_channel(RANKING_CHANNEL_ID)
            if chan:
                await chan.send(f"⏰ {winner.mention} の試合が15分経過により自動承認されました。")

    task = asyncio.create_task(auto_finalize())
    awaiting_results[winner.id]["timer_task"] = task

    await interaction.response.send_message(
        f"勝者報告完了。敗者 {opponent.mention} が `/承認` するか、15分で自動承認されます。"
    )


# ==============================
# 対戦結果処理
# ==============================
async def finalize_match(winner_id, loser_id):
    # データ初期化
    data_w = players.setdefault(winner_id, {"pt": 0, "challenge": False})
    data_l = players.setdefault(loser_id, {"pt": 0, "challenge": False})

    winner_pt = data_w["pt"]
    loser_pt = data_l["pt"]

    # 階級差
    diff = abs(rank_difference(winner_pt, loser_pt))

    # Pt計算
    if diff == 0:
        winner_pt += 1
        loser_pt = max(loser_pt - 1, 0)
    else:
        if winner_pt < loser_pt:
            winner_pt += 1 + diff
            loser_pt = max(loser_pt - 1, 0)
        else:
            winner_pt += 1
            loser_pt = max(loser_pt - 1 - diff, 0)

    # Gold到達後の降格制限
    if data_l["pt"] >= 10 and loser_pt < 10:
        loser_pt = 10

    # Pt更新
    data_w["pt"] = winner_pt
    data_l["pt"] = loser_pt

    # 昇格チャレンジ判定
    for uid in [winner_id, loser_id]:
        pt = players[uid]["pt"]
        players[uid]["challenge"] = pt in [4, 9, 14, 19, 24]
        await update_member_display(uid
