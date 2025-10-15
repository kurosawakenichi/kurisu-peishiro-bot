# -*- coding: utf-8 -*-
import os
import asyncio
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands, tasks

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

# 日本時間（UTC+9）
JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ---------------------------
# 階級定義
# ---------------------------
RANKS = [
    ("Beginner", 0, 4, "🔰"),
    ("Silver", 5, 9, "🥈"),
    ("Gold", 10, 14, "🥇"),
    ("Master", 15, 19, "⚔️"),
    ("GroundMaster", 20, 24, "🪽"),
    ("Challenger", 25, 9999, "😈"),
]

REPORT_CHANNEL = "対戦結果報告"
RANKING_CHANNEL = "ランキング"

user_points = {}
promotion_state = {}
pending_matches = {}
event_start = None
event_end = None

# ---------------------------
# ヘルパー関数
# ---------------------------
def get_rank(pt: int):
    for name, low, high, emoji in RANKS:
        if low <= pt <= high:
            return name, emoji
    return "Challenger", "😈"

def get_rank_emoji(pt: int, promotion: dict):
    _, emoji = get_rank(pt)
    return emoji + ("🔥" if promotion and promotion.get("challenge", False) else "")

def ensure_user_initialized(uid):
    if uid not in user_points:
        user_points[uid] = 0
    if uid not in promotion_state:
        promotion_state[uid] = {"challenge": False, "start_pt": 0, "accumulated": 0}

async def update_roles(member: discord.Member, pt: int):
    if not member:
        return
    guild = member.guild
    rank_name, _ = get_rank(pt)
    for name, _, _, _ in RANKS:
        role = discord.utils.get(guild.roles, name=name)
        if not role:
            continue
        if name == rank_name:
            await member.add_roles(role)
        else:
            await member.remove_roles(role)

async def announce_promotion(member: discord.Member, new_rank: str, emoji: str):
    channel = discord.utils.get(member.guild.text_channels, name=RANKING_CHANNEL)
    if channel:
        await channel.send(f"🔥 {member.mention} が昇級しました！ 次の階級：{emoji}{new_rank}")

def format_ranking():
    sorted_members = sorted(user_points.items(), key=lambda x: x[1], reverse=True)
    lines = ["🏆 現在のランキング 🏆"]
    for i, (uid, pt) in enumerate(sorted_members[:20], start=1):
        promo = promotion_state.get(uid, {"challenge": False})
        lines.append(f"{i}. <@{uid}> — {get_rank_emoji(pt, promo)} ({pt}pt)")
    return "\n".join(lines)

def rank_index(name):
    return {r[0]: i for i, r in enumerate(RANKS)}.get(name, 0)

# ---------------------------
# イベント期間管理
# ---------------------------
def is_event_active():
    if not event_start or not event_end:
        return True
    now = datetime.now(JST)
    return event_start <= now <= event_end

# ---------------------------
# BOT起動時
# ---------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    await bot.tree.sync()
    post_ranking.start()
    check_pending.start()

# ---------------------------
# イベント設定
# ---------------------------
@bot.tree.command(name="イベント設定", description="イベント開始・終了日時を設定（管理者専用）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def event_setting(interaction: discord.Interaction, 開始: str, 終了: str):
    global event_start, event_end
    try:
        event_start = datetime.strptime(開始, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        event_end = datetime.strptime(終了, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        await interaction.response.send_message(
            f"イベント期間を設定しました。\n開始：{event_start}\n終了：{event_end}", ephemeral=True
        )
    except ValueError:
        await interaction.response.send_message("日時形式が不正です。YYYY-MM-DD HH:MM 形式で指定してください。", ephemeral=True)

# ---------------------------
# 試合開始
# ---------------------------
@bot.tree.command(name="対戦開始", description="対戦相手を指定して試合を開始")
async def start_match(interaction: discord.Interaction, opponent: discord.Member):
    if not is_event_active():
        await interaction.response.send_message(
            "⚠️このイベントは終了しています。新しいマッチングはできません。\n@kurosawa0118 へのメンション付きメッセージにてご報告ください。",
            ephemeral=True)
        return

    user1, user2 = interaction.user, opponent
    ensure_user_initialized(user1.id)
    ensure_user_initialized(user2.id)

    # 重複申請防止
    if user1.id in pending_matches or user2.id in [m['loser_id'] for m in pending_matches.values()]:
        await interaction.response.send_message("⚠️既に承認待ちの試合があります。取り下げてから再度申請してください。", ephemeral=True)
        return

    # 3階級差チェック
    r1, _ = get_rank(user_points[user1.id])
    r2, _ = get_rank(user_points[user2.id])
    if abs(rank_index(r1) - rank_index(r2)) > 2:
        await interaction.response.send_message("⚠️階級差が3以上あるためマッチング不可です。", ephemeral=True)
        return

    pending_matches[user1.id] = {
        "loser_id": user2.id,
        "approved": False,
        "timestamp": datetime.now(JST),
    }
    await interaction.response.send_message(
        f"{user1.mention} vs {user2.mention} の試合を開始しました！\n勝者は {user1.mention} です。敗者は /承認 または /拒否 で結果を承認してください。"
    )

# ---------------------------
# 試合結果承認・拒否・取り下げ
# ---------------------------
@bot.tree.command(name="承認", description="試合結果を承認する")
async def approve(interaction: discord.Interaction):
    loser = interaction.user
    for winner_id, match in list(pending_matches.items()):
        if match["loser_id"] == loser.id and not match["approved"]:
            pending_matches[winner_id]["approved"] = True
            await process_result(interaction.guild, winner_id, loser.id)
            del pending_matches[winner_id]
            await interaction.response.send_message(f"{loser.mention} が試合結果を承認しました。")
            return
    await interaction.response.send_message("承認する試合がありません。", ephemeral=True)

@bot.tree.command(name="拒否", description="試合結果を拒否する")
async def reject(interaction: discord.Interaction):
    user = interaction.user
    for winner_id, match in list(pending_matches.items()):
        if match["loser_id"] == user.id:
            del pending_matches[winner_id]
            await interaction.response.send_message(f"{user.mention} が試合結果を拒否しました。")
            return
    await interaction.response.send_message("拒否する試合がありません。", ephemeral=True)

@bot.tree.command(name="申請取り下げ", description="自分が申請した試合を取り下げる")
async def cancel(interaction: discord.Interaction):
    user = interaction.user
    if user.id in pending_matches:
        del pending_matches[user.id]
        await interaction.response.send_message("試合申請を取り下げました。", ephemeral=True)
    else:
        await interaction.response.send_message("取り下げる申請がありません。", ephemeral=True)

# ---------------------------
# 試合結果反映
# ---------------------------
async def process_result(guild, winner_id, loser_id):
    ensure_user_initialized(winner_id)
    ensure_user_initialized(loser_id)

    winner_pt = user_points[winner_id]
    loser_pt = user_points[loser_id]
    winner_rank, _ = get_rank(winner_pt)
    loser_rank, _ = get_rank(loser_pt)
    diff = abs(rank_index(winner_rank) - rank_index(loser_rank))

    # Pt計算
    if rank_index(winner_rank) < rank_index(loser_rank):
        win_gain = 1 + diff
        lose_loss = 1
    elif rank_index(winner_rank) > rank_index(loser_rank):
        win_gain = 1
        lose_loss = 1 + diff
    else:
        win_gain = lose_loss = 1

    user_points[winner_id] += win_gain
    if loser_pt > 0:
        user_points[loser_id] -= lose_loss

    # Gold以上は下限保護
    if get_rank(loser_pt)[0] in ["Gold", "Master", "GroundMaster", "Challenger"]:
        user_points[loser_id] = max(user_points[loser_id], 10)

    # 昇級チャレンジ処理
    promoted = update_promotion_after_win(guild, winner_id, win_gain)
    fail_promotion(loser_id)

    # ロール更新
    winner = guild.get_member(winner_id)
    loser = guild.get_member(loser_id)
    await update_roles(winner, user_points[winner_id])
    await update_roles(loser, user_points[loser_id])

    if promoted:
        new_rank, emoji = get_rank(user_points[winner_id])
        await announce_promotion(winner, new_rank, emoji)

def start_promotion_if_needed(uid):
    pt = user_points[uid]
    if pt in [4, 9, 14, 19, 24]:
        promotion_state[uid] = {"challenge": True, "start_pt": pt, "accumulated": 0}

def update_promotion_after_win(guild, uid, gain):
    start_promotion_if_needed(uid)
    state = promotion_state.get(uid)
    if state and state.get("challenge"):
        state["accumulated"] += gain
        if state["accumulated"] >= 2:
            state["challenge"] = False
            return True
    return False

def fail_promotion(uid):
    state = promotion_state.get(uid)
    if state and state.get("challenge"):
        start_pt = state.get("start_pt", 0)
        user_points[uid] = max(0, start_pt - 1)
        state["challenge"] = False
        state["accumulated"] = 0

# ---------------------------
# 自動承認
# ---------------------------
@tasks.loop(seconds=60)
async def check_pending():
    now = datetime.now(JST)
    for winner_id, match in list(pending_matches.items()):
        if not match["approved"] and (now - match["timestamp"]).total_seconds() > 900:
            guild = bot.get_guild(GUILD_ID)
            await process_result(guild, winner_id, match["loser_id"])
            del pending_matches[winner_id]

# ---------------------------
# ランキング自動投稿
# ---------------------------
@tasks.loop(minutes=1)
async def post_ranking():
    now = datetime.now(JST)
    if now.hour in [14, 22] and now.minute == 0:
        channel = discord.utils.get(bot.get_guild(GUILD_ID).text_channels, name=RANKING_CHANNEL)
        if channel:
            await channel.send(format_ranking())

# ---------------------------
# 管理者コマンド
# ---------------------------
@bot.tree.command(name="pt操作", description="管理者用：特定ユーザーのPtを増減")
@discord.app_commands.checks.has_permissions(administrator=True)
async def pt_operate(interaction: discord.Interaction, ユーザー: discord.Member, 増減: int):
    ensure_user_initialized(ユーザー.id)
    old_pt = user_points[ユーザー.id]
    user_points[ユーザー.id] += 増減
    await update_roles(ユーザー, user_points[ユーザー.id])
    await interaction.response.send_message(
        f"{ユーザー.mention} のPtを {増減:+} しました。({old_pt} → {user_points[ユーザー.id]})",
        ephemeral=True)

@bot.tree.command(name="ランキングリセット", description="管理者用：全ユーザーのPtをリセット")
@discord.app_commands.checks.has_permissions(administrator=True)
async def reset_ranking(interaction: discord.Interaction):
    for uid in user_points.keys():
        user_points[uid] = 0
        promotion_state[uid] = {"challenge": False, "start_pt": 0, "accumulated": 0}
    await interaction.response.send_message("全ユーザーのPtをリセットしました。", ephemeral=True)

bot.run(TOKEN)
