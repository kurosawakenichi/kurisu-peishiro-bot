import os
import asyncio
from typing import Dict, List, Optional
import discord
from discord import app_commands
from discord.ext import tasks

# -----------------------
# 設定値
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

PT_ROLES = [
    (0, 9, "Beginner", "🔰"),
    (10, 19, "Silver", "🥈"),
    (20, 29, "Gold", "🥇"),
    (30, 39, "Master", "⚔️"),
    (40, 49, "GroundMaster", "🪽"),
    (50, 999, "Challenger", "😈")
]

# -----------------------
# データ管理
# -----------------------
user_data: Dict[int, int] = {}  # user_id -> pt
match_waiting: List[int] = []
in_match: Dict[int, int] = {}  # user_id -> opponent_id

# -----------------------
# Bot 初期化
# -----------------------
intents = discord.Intents.default()
intents.members = True

class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = MyBot()

# -----------------------
# ヘルパー関数
# -----------------------
async def update_member_role(member: discord.Member, pt: int):
    guild = member.guild
    # 現在のPTに応じたロール決定
    role_name, emoji = None, None
    for low, high, name, em in PT_ROLES:
        if low <= pt <= high:
            role_name, emoji = name, em
            break
    if not role_name:
        return
    # 既存のPTロールを削除
    role_ids = [discord.utils.get(guild.roles, name=r[2]) for r in PT_ROLES]
    for r in role_ids:
        if r in member.roles:
            await member.remove_roles(r)
    # 新しいロールを付与
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        await member.add_roles(role)
    # ニックネーム更新
    try:
        new_nick = f"{member.name} {emoji}({pt})"
        await member.edit(nick=new_nick)
    except:
        pass

async def change_user_pt(user_id: int, delta: int):
    pt = user_data.get(user_id, 0) + delta
    if pt < 0:
        pt = 0
    user_data[user_id] = pt
    guild = bot.get_guild(GUILD_ID)
    if guild:
        member = guild.get_member(user_id)
        if member:
            await update_member_role(member, pt)

# -----------------------
# スラッシュコマンド
# -----------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチを申請")
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in in_match or user_id in match_waiting:
        await interaction.response.send_message("すでにマッチ待機中です。", ephemeral=True)
        return
    match_waiting.append(user_id)
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)
    # マッチングチェック
    if len(match_waiting) >= 2:
        p1, p2 = match_waiting.pop(0), match_waiting.pop(0)
        in_match[p1] = p2
        in_match[p2] = p1
        msg = f"{bot.get_user(p1).mention} vs {bot.get_user(p2).mention} でマッチ成立！"
        await interaction.channel.send(msg)

@bot.tree.command(name="結果報告", description="勝者によるマッチ結果申告")
@app_commands.describe(winner="勝者のメンション")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = in_match.get(winner.id)
    if not loser_id:
        await interaction.response.send_message("このマッチは存在しません。", ephemeral=True)
        return
    loser = bot.get_user(loser_id)
    # 承認ボタンは敗者のみ
    await interaction.response.send_message(f"{winner.mention} が勝利を報告しました。{loser.mention} の承認を待ちます。", ephemeral=False)
    # 仮で勝者+1pt, 敗者-1pt
    await change_user_pt(winner.id, 1)
    await change_user_pt(loser_id, -1)
    # マッチ削除
    del in_match[winner.id]
    del in_match[loser_id]

@bot.tree.command(name="ランキング", description="全ユーザーのランキングを表示")
async def ranking(interaction: discord.Interaction):
    sorted_users = sorted(user_data.items(), key=lambda x: x[1], reverse=True)
    msg = "**ランキング**\n"
    for uid, pt in sorted_users[:20]:
        member = bot.get_user(uid)
        if member:
            msg += f"{member.name}: {pt}pt\n"
    await interaction.response.send_message(msg, ephemeral=False)

# -----------------------
# 管理者コマンド
# -----------------------
@bot.tree.command(name="admin_reset_all", description="全ユーザーPTをリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    for uid in user_data.keys():
        user_data[uid] = 0
        member = bot.get_guild(GUILD_ID).get_member(uid)
        if member:
            await update_member_role(member, 0)
    await interaction.response.send_message("全ユーザーPTをリセットしました。", ephemeral=True)

@bot.tree.command(name="admin_set_pt", description="指定ユーザーのPTを変更")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    user_data[user.id] = pt
    await update_member_role(user, pt)
    await interaction.response.send_message(f"{user.name} のPTを {pt} に設定しました。", ephemeral=True)

# -----------------------
# 起動
# -----------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    try:
        guild = discord.Object(id=GUILD_ID)
        await bot.tree.sync(guild=guild)
        print("Commands synced to guild.")
    except Exception as e:
        print("コマンド同期エラー:", e)

bot.run(DISCORD_TOKEN)
