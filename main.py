import os
import asyncio
from typing import Dict, List
import discord
from discord.ext import tasks
from discord import app_commands

# -----------------------
# 設定値
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

# -----------------------
# 定数
# -----------------------
PT_ROLES = {
    range(0, 10): 'Beginner',
    range(10, 20): 'Silver',
    range(20, 30): 'Gold',
    range(30, 40): 'Master',
    range(40, 50): 'GroundMaster',
    range(50, 1000): 'Challenger'
}

# -----------------------
# Bot初期化
# -----------------------
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = MyBot()
interaction_store: Dict[int, discord.Interaction] = {}

# -----------------------
# データロード
# -----------------------
def load_data():
    if not os.path.exists('user_data.json'):
        with open('user_data.json', 'w') as f:
            f.write('{}')

# -----------------------
# ユーティリティ
# -----------------------
async def update_member_role(member: discord.Member, pt: int):
    role_name = None
    for r in PT_ROLES:
        if pt in r:
            role_name = PT_ROLES[r]
            break
    if role_name is None:
        return

    guild = member.guild
    target_role = discord.utils.get(guild.roles, name=role_name)
    if target_role is None:
        print(f"Role {role_name} not found.")
        return

    # 古いPTロールを削除
    for r in PT_ROLES.values():
        old_role = discord.utils.get(guild.roles, name=r)
        if old_role in member.roles and old_role != target_role:
            await member.remove_roles(old_role)

    if target_role not in member.roles:
        await member.add_roles(target_role)

    # ニックネーム更新
    try:
        await member.edit(nick=f'{member.name} [{pt}pt]')
    except discord.Forbidden:
        print(f"権限不足で {member} のニックネームを更新できません")

# -----------------------
# タスク
# -----------------------
@tasks.loop(minutes=5)
async def cleanup_task():
    # 定期処理が必要な場合ここに記述
    pass

# -----------------------
# Discordイベント / コマンド
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

    load_data()
    if not cleanup_task.is_running():
        cleanup_task.start()

# /マッチ希望
@bot.tree.command(name="マッチ希望", description="マッチングを希望します")
async def match_request(interaction: discord.Interaction):
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)

# /結果報告
@bot.tree.command(name="結果報告", description="勝者が報告します")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    # 公開メッセージ
    await interaction.response.send_message(f"{winner.display_name} が勝者として報告されました。")

# /ランキング
@bot.tree.command(name="ランキング", description="現在のランキングを表示します")
async def show_ranking(interaction: discord.Interaction):
    # 仮表示
    await interaction.response.send_message("ランキングを表示します。")

# 管理者コマンド
@bot.tree.command(name="admin_reset_all", description="全ユーザーPTリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await interaction.response.send_message("全ユーザーPTをリセットしました。", ephemeral=True)

@bot.tree.command(name="admin_set_pt", description="ユーザーのPTを設定")
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await update_member_role(member, pt)
    await interaction.response.send_message(f"{member.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# -----------------------
# Bot起動
# -----------------------
bot.run(DISCORD_TOKEN)
