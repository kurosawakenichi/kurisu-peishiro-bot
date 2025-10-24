import discord
from discord.ext import tasks
from discord import app_commands
import asyncio
import datetime
import os

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
JUDGE_CHANNEL_ID = int(os.getenv("JUDGE_CHANNEL_ID"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))
RANKING_CHANNEL_ID = int(os.getenv("RANKING_CHANNEL_ID"))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ユーザー管理
users = {}  # {user_id: {"pt": int, "match": None or match_id, "role_emoji": str}}
in_match = {}  # {match_id: {"users": [user_id, user_id], "timestamp": datetime, "status": "waiting"|"pending"}}
match_requests = set()  # 抽選希望者 user_id
match_counter = 0

PT_ROLES = [
    (0, 2, "🔰"),
    (3, 7, "🥈"),
    (8, 12, "🥇"),
    (13, 17, "⚔️"),
    (18, 22, "🪽"),
    (23, 9999, "😈")
]

# ------------------------
# ユーティリティ関数
# ------------------------
def get_role_emoji(pt):
    for low, high, emoji in PT_ROLES:
        if low <= pt <= high:
            return emoji
    return "🔰"

async def update_user_display(member: discord.Member, pt):
    emoji = get_role_emoji(pt)
    users[member.id]["role_emoji"] = emoji
    try:
        await member.edit(nick=f"{emoji} {member.display_name}")
    except:
        pass  # 権限不足時は無視

def can_match(pt1, pt2):
    # チャレンジ帯制限
    challenge_ranges = [(3,4),(8,9),(13,14),(18,19),(23,24)]
    for low, high in challenge_ranges:
        if pt1 in range(low,high+1) and pt2 not in range(low,high+1):
            return False
        if pt2 in range(low,high+1) and pt1 not in range(low,high+1):
            return False
    return True

def match_users():
    global match_counter
    matched = []
    waiting = list(match_requests)
    while len(waiting) >=2:
        u1 = waiting.pop(0)
        for i, u2 in enumerate(waiting):
            if can_match(users[u1]["pt"], users[u2]["pt"]):
                waiting.pop(i)
                match_counter += 1
                mid = match_counter
                in_match[mid] = {
                    "users": [u1,u2],
                    "timestamp": datetime.datetime.now(),
                    "status": "waiting"
                }
                matched.append((mid,u1,u2))
                break
    # 残ったユーザーは再度登録
    match_requests.clear()
    for u in waiting:
        match_requests.add(u)
    return matched

async def notify_match(mid,u1,u2):
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    member1 = guild.get_member(u1)
    member2 = guild.get_member(u2)
    msg = f"{member1.mention} vs {member2.mention} のマッチが成立しました！"
    try:
        channel = guild.get_channel(RANKING_CHANNEL_ID)
        if channel:
            await channel.send(msg)
    except:
        pass

async def apply_pt_change(winner_id, loser_id):
    users[winner_id]["pt"] += 1
    users[loser_id]["pt"] -= 1
    if users[loser_id]["pt"] < 0:
        users[loser_id]["pt"] = 0
    guild = client.get_guild(GUILD_ID)
    if guild:
        for uid in [winner_id, loser_id]:
            member = guild.get_member(uid)
            if member:
                await update_user_display(member, users[uid]["pt"])

# ------------------------
# スラッシュコマンド
# ------------------------
@tree.command(name="マッチ希望", description="ランダムマッチ希望を登録", guild=discord.Object(id=GUILD_ID))
async def match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in users:
        users[uid] = {"pt":0, "match":None, "role_emoji":"🔰"}
        await update_user_display(interaction.user,0)
    if uid in match_requests:
        await interaction.response.send_message("すでにマッチ希望済みです", ephemeral=True)
        return
    match_requests.add(uid)
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)
    # 抽選
    matched_pairs = match_users()
    for mid,u1,u2 in matched_pairs:
        await notify_match(mid,u1,u2)

@tree.command(name="結果報告", description="マッチの勝者を報告", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    w_id = winner.id
    l_id = loser.id
    if not any(w_id in m["users"] and l_id in m["users"] for m in in_match.values()):
        await interaction.response.send_message("このマッチングは存在しません", ephemeral=True)
        return
    await apply_pt_change(w_id,l_id)
    await interaction.response.send_message(f"{winner.display_name}の勝利を記録しました")

@tree.command(name="admin_reset_all", description="全ユーザーPTをリセット", guild=discord.Object(id=GUILD_ID))
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    for uid in users.keys():
        users[uid]["pt"] = 0
        guild = client.get_guild(GUILD_ID)
        if guild:
            member = guild.get_member(uid)
            if member:
                await update_user_display(member,0)
    await interaction.response.send_message("全ユーザーPTをリセットしました")

@tree.command(name="admin_set_pt", description="特定ユーザーのPTを設定", guild=discord.Object(id=GUILD_ID))
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    if member.id not in users:
        users[member.id] = {"pt":0, "match":None, "role_emoji":"🔰"}
    users[member.id]["pt"] = pt
    await update_user_display(member,pt)
    await interaction.response.send_message(f"{member.display_name} のPTを {pt} に設定しました")

# ------------------------
# Bot起動処理
# ------------------------
@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if guild:
        await tree.sync(guild=guild)
    print(f"{client.user} is ready. Guild: {GUILD_ID}")

# ------------------------
# 自動クリアタスク
# ------------------------
@tasks.loop(minutes=1)
async def cleanup_task():
    now = datetime.datetime.now()
    to_delete = []
    for mid,mdata in in_match.items():
        if mdata["status"]=="waiting" and (now - mdata["timestamp"]).total_seconds()>15*60:
            to_delete.append(mid)
    for mid in to_delete:
        del in_match[mid]

cleanup_task.start()
client.run(TOKEN)
