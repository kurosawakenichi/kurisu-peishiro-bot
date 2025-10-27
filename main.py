# main.py ランダム 完全版
import os
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
from typing import Dict, List, Optional
from datetime import datetime, timedelta

# -----------------------
# 設定値
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

PT_ROLES = {
    (0, 4): "🔰",
    (5, 9): "🥈",
    (10, 14): "🥇",
    (15, 19): "⚔️",
    (20, 24): "🪽",
    (25, 10000): "😈"
}

# -----------------------
# Bot & Tree
# -----------------------
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.in_match: Dict[int, dict] = {}  # user_id -> match info
        self.pending: List[int] = []  # waiting user_ids
        self.user_pts: Dict[int, int] = {}  # user_id -> pt
        self.lock = asyncio.Lock()

bot = MyBot()
tree = bot.tree

# -----------------------
# Helper functions
# -----------------------
def get_role_from_pt(pt: int) -> str:
    for (low, high), role in PT_ROLES.items():
        if low <= pt <= high:
            return role
    return "🔰"

async def update_member_role(member: discord.Member, pt: int):
    new_role_name = get_role_from_pt(pt)
    try:
        # remove old pt roles
        for role in member.roles:
            if role.name in PT_ROLES.values():
                await member.remove_roles(role)
        # add new pt role (create if not exists)
        guild = member.guild
        role = discord.utils.get(guild.roles, name=new_role_name)
        if not role:
            role = await guild.create_role(name=new_role_name)
        await member.add_roles(role)
        # update nickname
        await member.edit(nick=f"{new_role_name} {member.name}")
    except Exception as e:
        print(f"update_member_role error for {member.name}: {e}")

# -----------------------
# Data loading/saving
# -----------------------
DATA_FILE = "data.json"
import json
def load_data():
    global bot, DATA_FILE
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            bot.user_pts = data.get("user_pts", {})
    except Exception:
        bot.user_pts = {}

def save_data():
    global bot, DATA_FILE
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"user_pts": bot.user_pts}, f, ensure_ascii=False)
    except Exception as e:
        print(f"save_data error: {e}")

# -----------------------
# Cleanup Task
# -----------------------
@tasks.loop(seconds=60)
async def cleanup_task():
    async with bot.lock:
        now = datetime.utcnow()
        removed = []
        for uid in bot.pending:
            # pending timeout 5 minutes
            removed.append(uid)
        for uid in removed:
            bot.pending.remove(uid)

# -----------------------
# Commands
# -----------------------
@tree.command(name="マッチ希望", description="ランダムマッチ希望を登録", guild=discord.Object(id=GUILD_ID))
async def match_request(interaction: discord.Interaction):
    async with bot.lock:
        uid = interaction.user.id
        if uid in bot.in_match or uid in bot.pending:
            await interaction.response.send_message("すでに待機中か対戦中です。", ephemeral=True)
            return
        bot.pending.append(uid)
        await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)
        # try to pair
        if len(bot.pending) >= 2:
            u1 = bot.pending.pop(0)
            u2 = bot.pending.pop(0)
            bot.in_match[u1] = {"opponent": u2}
            bot.in_match[u2] = {"opponent": u1}
            guild = interaction.guild
            m1 = guild.get_member(u1)
            m2 = guild.get_member(u2)
            msg = f"マッチ成立: {m1.mention} vs {m2.mention}"
            await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="結果報告", description="勝者が申告する", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    async with bot.lock:
        loser_id = None
        if winner.id not in bot.in_match:
            await interaction.response.send_message("このユーザーはマッチ中ではありません。", ephemeral=True)
            return
        loser_id = bot.in_match[winner.id]["opponent"]
        loser = interaction.guild.get_member(loser_id)
        # PT更新
        bot.user_pts[winner.id] = bot.user_pts.get(winner.id, 0) + 1
        bot.user_pts[loser_id] = max(bot.user_pts.get(loser_id, 0) - 1, 0)
        await update_member_role(interaction.user, bot.user_pts[winner.id])
        await update_member_role(loser, bot.user_pts[loser_id])
        save_data()
        # マッチ削除
        bot.in_match.pop(winner.id, None)
        bot.in_match.pop(loser_id, None)
        # 公開報告
        await interaction.response.send_message(f"{winner.mention} の勝利！ PT更新済み。")

@tree.command(name="ランキング", description="全ユーザーのPTランキングを見る", guild=discord.Object(id=GUILD_ID))
async def ranking(interaction: discord.Interaction):
    items = sorted(bot.user_pts.items(), key=lambda x: -x[1])
    lines = []
    for uid, pt in items:
        member = interaction.guild.get_member(uid)
        if member:
            lines.append(f"{member.display_name}: {pt}pt")
    if not lines:
        lines.append("データなし")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@tree.command(name="admin_reset_all", description="全ユーザーのPTをリセット（管理者のみ）", guild=discord.Object(id=GUILD_ID))
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    async with bot.lock:
        for uid in list(bot.user_pts.keys()):
            bot.user_pts[uid] = 0
            member = interaction.guild.get_member(uid)
            if member:
                await update_member_role(member, 0)
        save_data()
        await interaction.response.send_message("全ユーザーPTリセット完了。", ephemeral=True)

@tree.command(name="admin_set_pt", description="ユーザーのPTを設定（管理者のみ）", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="対象ユーザー", pt="設定するPT")
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    async with bot.lock:
        bot.user_pts[member.id] = max(pt, 0)
        await update_member_role(member, pt)
        save_data()
        await interaction.response.send_message(f"{member.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# -----------------------
# on_ready
# -----------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    try:
        guild = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild)
        print("Commands synced to guild.")
    except Exception as e:
        print("コマンド同期エラー:", e)
    load_data()
    try:
        if not cleanup_task.is_running():
            cleanup_task.start()
    except Exception as e:
        print("cleanup_task start error:", e)

# -----------------------
# Run Bot
# -----------------------
bot.run(DISCORD_TOKEN)
