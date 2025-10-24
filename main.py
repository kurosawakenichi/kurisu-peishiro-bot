import discord
from discord import app_commands
from discord.ext import tasks
import asyncio
import os

# ------------------------------
# 環境変数
# ------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID", 0))
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID", 0))
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID", 0))

if not TOKEN:
    raise ValueError("DISCORD_TOKEN が設定されていません")

# ------------------------------
# Bot 初期化
# ------------------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ------------------------------
# データ管理
# ------------------------------
user_data = {}  # {user_id: {"pt": int, "role": discord.Role}}
match_waiting = []  # マッチ希望ユーザー
in_match = []       # 対戦中ユーザー

# ------------------------------
# ランク帯絵文字
# ------------------------------
def pt_to_rank_emoji(pt):
    if pt < 10:
        return "🔰"
    elif pt < 50:
        return "🥈"
    elif pt < 100:
        return "🥇"
    elif pt < 200:
        return "⚔️"
    elif pt < 500:
        return "🪽"
    else:
        return "😈"

# ------------------------------
# ユーザーPT更新
# ------------------------------
async def update_user_pt(user: discord.Member, delta: int):
    data = user_data.setdefault(user.id, {"pt": 0})
    data["pt"] = max(0, data["pt"] + delta)
    rank_emoji = pt_to_rank_emoji(data["pt"])
    try:
        # ロール名に絵文字を反映
        if user.top_role.name != rank_emoji:
            # 既存のPTロールがあれば変更
            await user.edit(nick=f"{user.name} {rank_emoji}")
    except Exception:
        pass

# ------------------------------
# /ランキング コマンド
# ------------------------------
@tree.command(name="ランキング", description="全ユーザーのPTランキング", guild=discord.Object(id=GUILD_ID))
async def ranking(interaction: discord.Interaction):
    sorted_users = sorted(user_data.items(), key=lambda x: x[1]["pt"], reverse=True)
    lines = []
    for user_id, data in sorted_users:
        member = interaction.guild.get_member(user_id)
        if member:
            lines.append(f"{member.name}: {data['pt']}pt")
    await interaction.response.send_message("\n".join(lines) or "データなし")

# ------------------------------
# /マッチ希望 コマンド
# ------------------------------
@tree.command(name="マッチ希望", description="ランダムマッチに参加", guild=discord.Object(id=GUILD_ID))
async def request_match(interaction: discord.Interaction):
    user = interaction.user
    if user.id in match_waiting or user.id in in_match:
        await interaction.response.send_message("既にマッチ中または待機中です")
        return
    match_waiting.append(user.id)
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。")
    # 抽選処理
    if len(match_waiting) >= 2:
        p1 = match_waiting.pop(0)
        p2 = match_waiting.pop(0)
        in_match.extend([p1, p2])
        guild = interaction.guild
        member1 = guild.get_member(p1)
        member2 = guild.get_member(p2)
        if member1 and member2:
            await guild.system_channel.send(f"マッチング成立: {member1.mention} vs {member2.mention}")

# ------------------------------
# 管理者コマンド
# ------------------------------
@tree.command(name="admin_reset_all", description="全ユーザーPTリセット", guild=discord.Object(id=GUILD_ID))
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません")
        return
    for uid in user_data:
        user_data[uid]["pt"] = 0
    await interaction.response.send_message("全ユーザーPTをリセットしました")

@tree.command(name="admin_set_pt", description="特定ユーザーPT設定", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません")
        return
    user_data[user.id] = {"pt": max(0, pt)}
    await update_user_pt(user, 0)
    await interaction.response.send_message(f"{user.name} のPTを {pt} に設定しました")

# ------------------------------
# 起動時処理
# ------------------------------
@client.event
async def on_ready():
    print(f"{client.user} is ready. Guild ID: {GUILD_ID}")
    await tree.sync(guild=discord.Object(id=GUILD_ID))

# ------------------------------
# タスク（例: マッチクリア等）
# ------------------------------
@tasks.loop(seconds=60)
async def cleanup_task():
    # 5分以上経過したマッチ希望をクリアするなどの処理
    pass

@cleanup_task.before_loop
async def before_cleanup():
    await client.wait_until_ready()

cleanup_task.start()

# ------------------------------
# Bot 起動
# ------------------------------
client.run(TOKEN)
