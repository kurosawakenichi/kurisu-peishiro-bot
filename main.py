import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta

# Variables
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])

# Bot
intents = discord.Intents.default()
client = commands.Bot(command_prefix="/", intents=intents)
tree = client.tree

# マッチング用データ
match_waiting = {}  # user_id -> request_time
in_match = {}       # match_id -> {"players": [...], "start_time": ..., "winner": None, "pending_dispute": False}
lock = asyncio.Lock()

# PT管理
user_pt = {}  # user_id -> pt

# UIボタン期限
BUTTON_TIMEOUT = 300  # 5分

# Cleanup intervals
MATCH_CLEANUP_INTERVAL = 60
EXPIRED_MATCH_TIME = 900  # 15分

# Helper
def get_pt_emoji(pt):
    if pt < 10:
        return "🔰"
    elif pt < 20:
        return "🥈"
    elif pt < 30:
        return "🥇"
    elif pt < 40:
        return "⚔️"
    elif pt < 50:
        return "🪽"
    else:
        return "😈"

async def send_judge_notice(content):
    channel = client.get_channel(JUDGE_CHANNEL_ID)
    if channel:
        await channel.send(content)

async def cleanup_matches():
    while True:
        async with lock:
            now = datetime.utcnow()
            expired_matches = [mid for mid, m in in_match.items() 
                               if (m["winner"] is None and (now - m["start_time"]).total_seconds() > EXPIRED_MATCH_TIME)]
            for mid in expired_matches:
                del in_match[mid]
        await asyncio.sleep(MATCH_CLEANUP_INTERVAL)

@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    await tree.sync(guild=guild)
    print(f"{client.user} is ready. Guild ID: {GUILD_ID}")

# ------------------------
# Slash Commands
# ------------------------
@tree.command(name="マッチ希望", description="ランダムマッチに参加", guild=discord.Object(id=GUILD_ID))
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    async with lock:
        match_waiting[user_id] = datetime.utcnow()
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)
    await try_matching()

@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り消す", guild=discord.Object(id=GUILD_ID))
async def match_cancel(interaction: discord.Interaction):
    user_id = interaction.user.id
    async with lock:
        if user_id in match_waiting:
            del match_waiting[user_id]
            await interaction.response.send_message("マッチ希望を取り消しました。", ephemeral=True)
        else:
            await interaction.response.send_message("マッチ希望は登録されていません。", ephemeral=True)

@tree.command(name="結果報告", description="マッチの勝者を報告", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    async with lock:
        match = next((m for m in in_match.values() if winner.id in m["players"] and loser.id in m["players"]), None)
        if not match:
            await interaction.response.send_message("対象のマッチが存在しません。", ephemeral=True)
            return
        if match.get("pending_dispute"):
            await interaction.response.send_message("審議中のマッチです。", ephemeral=True)
            return
        match["winner"] = winner.id
        # PT加減算
        user_pt[winner.id] = user_pt.get(winner.id, 0) + 1
        user_pt[loser.id] = max(user_pt.get(loser.id, 0) - 1, 0)
        await interaction.response.send_message(f"勝者: {winner.display_name}\nPTを更新しました。")

@tree.command(name="ランキング", description="PTランキングを表示", guild=discord.Object(id=GUILD_ID))
async def show_ranking(interaction: discord.Interaction):
    sorted_users = sorted(user_pt.items(), key=lambda x: -x[1])
    msg = "ランキング\n"
    for uid, pt in sorted_users:
        user = interaction.guild.get_member(uid)
        if user:
            msg += f"{get_pt_emoji(pt)} {user.display_name} : {pt}\n"
    await interaction.response.send_message(msg)

# ------------------------
# Admin Commands
# ------------------------
@tree.command(name="admin_reset_all", description="全ユーザーのPTをリセット", guild=discord.Object(id=GUILD_ID))
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    async with lock:
        user_pt.clear()
    await interaction.response.send_message("全ユーザーのPTをリセットしました。")

@tree.command(name="admin_set_pt", description="ユーザーのPTを設定", guild=discord.Object(id=GUILD_ID))
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    async with lock:
        user_pt[member.id] = pt
    await interaction.response.send_message(f"{member.display_name} のPTを {pt} に設定しました。")

# ------------------------
# Matching Logic
# ------------------------
async def try_matching():
    async with lock:
        users = list(match_waiting.keys())
        while len(users) >= 2:
            p1, p2 = users[0], users[1]
            match_id = f"{p1}_{p2}_{int(datetime.utcnow().timestamp())}"
            in_match[match_id] = {"players": [p1, p2], "start_time": datetime.utcnow(), "winner": None, "pending_dispute": False}
            del match_waiting[p1]
            del match_waiting[p2]
            # Notify users
            u1 = client.get_user(p1)
            u2 = client.get_user(p2)
            if u1:
                await u1.send(f"マッチが成立しました: {u1.display_name} vs {u2.display_name}")
            if u2:
                await u2.send(f"マッチが成立しました: {u1.display_name} vs {u2.display_name}")
            users = list(match_waiting.keys())

# ------------------------
# Background Task
# ------------------------
@tasks.loop(seconds=MATCH_CLEANUP_INTERVAL)
async def background_cleanup():
    async with lock:
        now = datetime.utcnow()
        expired_matches = [mid for mid, m in in_match.items() 
                           if (now - m["start_time"]).total_seconds() > EXPIRED_MATCH_TIME]
        for mid in expired_matches:
            del in_match[mid]

# ------------------------
# Start Bot
# ------------------------
async def main():
    background_cleanup.start()
    await client.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
