# main.py
# -*- coding: utf-8 -*-
import os
import asyncio
from datetime import datetime, time, timedelta
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
# データ構造（簡易）
# ───────────────────────────────
user_points = {}
promotion_state = {}

RANKS = [
    ("Beginner", 0, 4),
    ("Silver", 5, 9),
    ("Gold", 10, 14),
    ("Master", 15, 19),
    ("GroundMaster", 20, 24),
    ("Challenger", 25, 9999),
]

REPORT_CHANNEL = "対戦結果報告"
RANKING_CHANNEL = "ランキング"

# ───────────────────────────────
# 便利関数
# ───────────────────────────────
def get_rank_name(pt: int):
    for name, low, high in RANKS:
        if low <= pt <= high:
            return name
    return "Challenger"

async def update_roles(member: discord.Member, pt: int):
    guild = member.guild
    current_rank = get_rank_name(pt)
    for role_name, _, _ in RANKS:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            if role_name == current_rank:
                await member.add_roles(role)
            else:
                await member.remove_roles(role)

def format_ranking():
    sorted_members = sorted(user_points.items(), key=lambda x: x[1], reverse=True)
    lines = [f"🏆 **現在のランキング** 🏆"]
    for i, (uid, pt) in enumerate(sorted_members[:20], start=1):
        lines.append(f"{i}. <@{uid}> — {pt}pt")
    return "\n".join(lines) if len(lines) > 1 else "まだ試合結果がありません。"

# ───────────────────────────────
# Bot 起動
# ───────────────────────────────
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    await bot.tree.sync()
    post_ranking.start()

# ───────────────────────────────
# コマンド群
# ───────────────────────────────
@bot.tree.command(name="試合報告", description="対戦結果を報告します。")
async def report(interaction: discord.Interaction, 相手: discord.Member, 勝敗: str):
    reporter = interaction.user
    winner, loser = (reporter, 相手) if 勝敗 == "勝ち" else (相手, reporter)
    user_points.setdefault(winner.id, 0)
    user_points.setdefault(loser.id, 0)

    winner_pt = user_points[winner.id]
    loser_pt = user_points[loser.id]
    rank_diff = abs((winner_pt // 5) - (loser_pt // 5))
    gain = max(1, rank_diff + 1)

    # 負けた側が降格不可条件なら減点なし
    if loser_pt in [0, 15]:
        lose_change = 0
    else:
        lose_change = -gain

    user_points[winner.id] += gain
    user_points[loser.id] += lose_change

    await update_roles(winner, user_points[winner.id])
    await update_roles(loser, user_points[loser.id])

    channel = discord.utils.get(interaction.guild.text_channels, name=REPORT_CHANNEL)
    msg = f"✅ {winner.mention} の勝利！ (+{gain}pt)\n❌ {loser.mention} の敗北 ({lose_change}pt)"
    await channel.send(msg)
    await interaction.response.send_message("報告を受け付けました！", ephemeral=True)

@bot.tree.command(name="ランキング", description="現在のランキングを表示します。")
async def ranking_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(format_ranking())

# ───────────────────────────────
# 定期ランキング投稿
# ───────────────────────────────
@tasks.loop(minutes=1)
async def post_ranking():
    now = datetime.now()
    if now.minute == 0 and now.hour in [15, 22]:
        guild = bot.get_guild(GUILD_ID)
        channel = discord.utils.get(guild.text_channels, name=RANKING_CHANNEL)
        if channel:
            await channel.send(format_ranking())
        await asyncio.sleep(60)  # 重複防止

# ───────────────────────────────
# 起動
# ───────────────────────────────
bot.run(TOKEN)
