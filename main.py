import os
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
from typing import Dict

# -----------------------
# 設定値
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
# Optional channels
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

# -----------------------
# Bot 定義
# -----------------------
intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)
bot.tree = app_commands.CommandTree(bot)

# -----------------------
# データ管理
# -----------------------
user_data = {}  # ptやrankなどを保持
interaction_store: Dict[int, object] = {}  # 最小限の管理

# -----------------------
# タスク
# -----------------------
@tasks.loop(seconds=60)
async def cleanup_task():
    pass  # 既存処理のまま

# -----------------------
# ユーティリティ
# -----------------------
async def update_member_role(member: discord.Member, pt: int):
    # pt帯に応じて既存ロールを付与
    if pt < 20:
        role_name = "Beginner"
    elif pt < 40:
        role_name = "Silver"
    elif pt < 60:
        role_name = "Gold"
    elif pt < 80:
        role_name = "Master"
    elif pt < 100:
        role_name = "GroundMaster"
    else:
        role_name = "Challenger"
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role:
        await member.add_roles(role)
        # 他のランクロールは外す
        for r in member.roles:
            if r.name in ["Beginner","Silver","Gold","Master","GroundMaster","Challenger"] and r != role:
                await member.remove_roles(r)
        # ニックネーム更新
        try:
            await member.edit(nick=f"{member.name} ({pt}pt)")
        except:
            pass

# -----------------------
# データロード
# -----------------------
def load_data():
    pass  # 既存処理のまま

# -----------------------
# Discord Event Handlers / Commands
# -----------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"Commands synced to guild. Total synced: {len(synced)}")
    except Exception as e:
        print("コマンド同期エラー:", e)
    load_data()
    try:
        if not cleanup_task.is_running():
            cleanup_task.start()
    except Exception as e:
        print("cleanup_task start error:", e)

# -----------------------
# /マッチ希望 例
# -----------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def match_request(interaction: discord.Interaction):
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)

# -----------------------
# /結果報告 例
# -----------------------
@bot.tree.command(name="結果報告", description="勝者が報告")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    await interaction.response.send_message(f"{winner.display_name} の勝利が報告されました。")

# -----------------------
# /ランキング 例
# -----------------------
@bot.tree.command(name="ランキング", description="全ユーザーのランキングを表示")
async def ranking(interaction: discord.Interaction):
    await interaction.response.send_message("ランキング一覧です。", ephemeral=True)

# -----------------------
# 管理者コマンド例
# -----------------------
@bot.tree.command(name="admin_reset_all", description="全ユーザーPTリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    # 全ユーザーPTリセット処理
    await interaction.response.send_message("全ユーザーのPTをリセットしました。")

@bot.tree.command(name="admin_set_pt", description="ユーザーPT設定")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    user_data[user.id] = pt
    await update_member_role(user, pt)
    await interaction.response.send_message(f"{user.display_name} のPTを {pt} に設定しました。")

# -----------------------
# Bot 起動
# -----------------------
bot.run(DISCORD_TOKEN)
