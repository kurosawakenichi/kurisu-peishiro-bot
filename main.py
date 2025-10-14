# -*- coding: utf-8 -*-
import os
import asyncio
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks

# ───────────────────────────────
# 基本設定
# ───────────────────────────────
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ───────────────────────────────
# データ構造
# ───────────────────────────────
user_points = {}          # ユーザーごとのPt
promotion_state = {}      # 昇格チャレンジ情報: {challenge: bool, start_pt: int, accumulated: int}
pending_matches = {}      # 勝者: { 'loser_id':..., 'approved': False, 'timestamp':... }

# 階級情報
RANKS = [
    ("Beginner", 0, 4, "🔰"),
    ("Silver", 5, 9, "🪙"),
    ("Gold", 10, 14, "🥇"),
    ("Master", 15, 19, "🪽"),
    ("GroundMaster", 20, 24, "😈"),
    ("Challenger", 25, 9999, "👹"),
]

REPORT_CHANNEL = "対戦結果報告"
RANKING_CHANNEL = "ランキング"

# ───────────────────────────────
# 便利関数
# ───────────────────────────────
def get_rank(pt: int):
    for name, low, high, emoji in RANKS:
        if low <= pt <= high:
            return name, emoji
    return "Challenger", "👹"

def get_rank_emoji(pt: int, promotion: dict):
    _, emoji = get_rank(pt)
    return emoji + ("🔥" if promotion and promotion.get('challenge', False) else "")

async def update_roles(member: discord.Member, pt: int):
    guild = member.guild
    rank_name, _ = get_rank(pt)
    for name, _, _, _ in RANKS:
        role = discord.utils.get(guild.roles, name=name)
        if role:
            if name == rank_name:
                await member.add_roles(role)
            else:
                await member.remove_roles(role)

def format_ranking():
    sorted_members = sorted(user_points.items(), key=lambda x: x[1], reverse=True)
    lines = ["🏆 現在のランキング 🏆"]
    for i, (uid, pt) in enumerate(sorted_members[:20], start=1):
        promotion = promotion_state.get(uid, {'challenge': False})
        lines.append(f"{i}. <@{uid}> — {get_rank_emoji(pt, promotion)} ({pt}pt)")
    return "\n".join(lines) if len(lines) > 1 else "まだ試合結果がありません。"

def start_promotion_if_needed(user_id):
    """Ptが5pt刻みで昇給チャンスの場合に昇格チャレンジ状態を開始"""
    pt = user_points[user_id]
    if pt > 0 and pt % 5 == 0:
        promotion_state[user_id] = {'challenge': True, 'start_pt': pt, 'accumulated': 0}

def update_promotion_after_win(user_id, gain):
    """昇格チャレンジ中のPt増加処理"""
    state = promotion_state.get(user_id)
    if state and state.get('challenge', False):
        state['accumulated'] += gain
        # チャレンジクリア判定（2pt以上稼ぐと昇格）
        if state['accumulated'] >= 2:
            # 昇格確定: Ptを1階級分プラス
            user_points[user_id] += 1  # 実際の階級はPt更新時にロールで反映
            state['challenge'] = False
            state['accumulated'] = 0
            return True
    return False

def fail_promotion(user_id):
    state = promotion_state.get(user_id)
    if state and state.get('challenge', False):
        # 1敗したらチャレンジ開始時-1ptに戻す
        start_pt = state.get('start_pt', 0)
        user_points[user_id] = start_pt - 1 if start_pt > 0 else 0
        state['challenge'] = False
        state['accumulated'] = 0

# ───────────────────────────────
# Bot 起動
# ───────────────────────────────
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    await bot.tree.sync()
    post_ranking.start()
    check_pending.start()

# ───────────────────────────────
# コマンド群
# ───────────────────────────────
@bot.tree.command(name="対戦開始", description="対戦する相手と承認しあう")
async def start_match(interaction: discord.Interaction, 相手: discord.Member):
    user_id = interaction.user.id
    opponent_id = 相手.id
    key = tuple(sorted([user_id, opponent_id]))
    if key in pending_matches:
        await interaction.response.send_message("既に承認済みの対戦です。", ephemeral=True)
        return
    pending_matches[key] = {
        'winner_id': None,
        'loser_id': None,
        'approved': False,
        'timestamp': datetime.now()
    }
    await interaction.response.send_message(
        f"{interaction.user.mention} と {相手.mention} の対戦承認が登録されました。"
        "お互い `/試合報告 @相手` で試合報告が可能になります。",
        ephemeral=True
    )

@bot.tree.command(name="試合報告", description="対戦結果を報告します（勝者が実行）")
async def report(interaction: discord.Interaction, 相手: discord.Member):
    winner = interaction.user
    loser = 相手
    key = tuple(sorted([winner.id, loser.id]))
    if key not in pending_matches:
        await interaction.response.send_message("事前承認がされていません。", ephemeral=True)
        return

    match = pending_matches[key]
    match['winner_id'] = winner.id
    match['loser_id'] = loser.id
    match['approved'] = False
    match['timestamp'] = datetime.now()

    channel = discord.utils.get(interaction.guild.text_channels, name=REPORT_CHANNEL)
    await channel.send(
        f"{winner.mention} が {loser.mention} に勝利を報告しました！\n"
        "敗者は `/承認` または `/拒否` で承認してください。（30分経過で自動承認）"
    )
    await interaction.response.send_message("勝利報告を受け付けました。敗者の承認待ちです。", ephemeral=True)

@bot.tree.command(name="承認", description="敗者が勝者報告を承認")
async def approve(interaction: discord.Interaction):
    user_id = interaction.user.id
    for key, match in pending_matches.items():
        if match['loser_id'] == user_id and not match['approved']:
            match['approved'] = True
            winner_id = match['winner_id']
            loser_id = match['loser_id']
            # Pt更新
            user_points.setdefault(winner_id, 0)
            user_points.setdefault(loser_id, 0)
            gain = 1
            user_points[winner_id] += gain
            user_points[loser_id] -= 1 if user_points[loser_id] > 0 else 0

            # 昇格判定
            start_promotion_if_needed(winner_id)
            if update_promotion_after_win(winner_id, gain):
                await interaction.channel.send(f"<@{winner_id}> が昇格チャレンジ成功！🔥")

            fail_promotion(loser_id)

            promotion_state[winner_id] = promotion_state.get(winner_id, {'challenge': False})
            promotion_state[loser_id] = promotion_state.get(loser_id, {'challenge': False})

            # ロール更新
            guild = interaction.guild
            await update_roles(guild.get_member(winner_id), user_points[winner_id])
            await update_roles(guild.get_member(loser_id), user_points[loser_id])

            await interaction.response.send_message("勝者報告が承認されました！Ptと階級を更新しました。", ephemeral=True)
            del pending_matches[key]
            return
    await interaction.response.send_message("承認できる勝者報告が見つかりません。", ephemeral=True)

@bot.tree.command(name="拒否", description="敗者が勝者報告を拒否")
async def reject(interaction: discord.Interaction):
    user_id = interaction.user.id
    for key, match in pending_matches.items():
        if match['loser_id'] == user_id and not match['approved']:
            channel = discord.utils.get(interaction.guild.text_channels, name=REPORT_CHANNEL)
            await channel.send(f"{interaction.user.mention} が勝者報告を拒否しました。運営が確認してください。")
            del pending_matches[key]
            await interaction.response.send_message("勝者報告を拒否しました。運営が確認します。", ephemeral=True)
            return
    await interaction.response.send_message("拒否できる勝者報告が見つかりません。", ephemeral=True)

@bot.tree.command(name="ランキング", description="現在のランキングを表示します")
async def ranking_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(format_ranking())

# ───────────────────────────────
# 定期処理
# ───────────────────────────────
@tasks.loop(minutes=1)
async def post_ranking():
    now = datetime.now()
    if now.minute == 0 and now.hour in [15, 22]:
        guild = bot.get_guild(GUILD_ID)
        channel = discord.utils.get(guild.text_channels, name=RANKING_CHANNEL)
        if channel:
            await channel.send(format_ranking())
        await asyncio.sleep(60)

@tasks.loop(minutes=1)
async def check_pending():
    now = datetime.now()
    to_auto = []
    for key, match in pending_matches.items():
        if not match['approved'] and (now - match['timestamp']) > timedelta(minutes=30):
            to_auto.append(key)
    for key in to_auto:
        match = pending_matches[key]
        winner_id = match['winner_id']
        loser_id = match['loser_id']
        user_points.setdefault(winner_id, 0)
        user_points.setdefault(loser_id, 0)
        gain = 1
        user_points[winner_id] += gain
        user_points[loser_id] -= 1 if user_points[loser_id] > 0 else 0

        start_promotion_if_needed(winner_id)
        if update_promotion_after_win(winner_id, gain):
            channel = discord.utils.get(bot.get_guild(GUILD_ID).text_channels, name=REPORT_CHANNEL)
            await channel.send(f"<@{winner_id}> が昇格チャレンジ成功！🔥")

        fail_promotion(loser_id)

        guild = bot.get_guild(GUILD_ID)
        await update_roles(guild.get_member(winner_id), user_points[winner_id])
        await update_roles(guild.get_member(loser_id), user_points[loser_id])

        channel = discord.utils.get(guild.text_channels, name=REPORT_CHANNEL)
        await channel.send(f"<@{loser_id}> の承認がありませんでした
