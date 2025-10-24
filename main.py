import os
import asyncio
import discord
from discord import app_commands

# Variables に登録済み
GUILD_ID = int(os.environ["GUILD_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ------------------------
# ここに main.py ランダムで使用している全スラッシュコマンド定義
# 省略せずに書きます
# ------------------------

@tree.command(name="マッチ希望", description="ランダムマッチを希望します")
async def match_request(interaction: discord.Interaction):
    await interaction.response.send_message("マッチ希望を受け付けました", ephemeral=True)

@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます")
async def cancel_match_request(interaction: discord.Interaction):
    await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)

@tree.command(name="結果報告", description="試合結果を申告します")
async def report_result(interaction: discord.Interaction):
    await interaction.response.send_message("結果報告を受け付けました", ephemeral=True)

@tree.command(name="admin_reset_all", description="全プレイヤーのptをリセットします")
@app_commands.checks.has_permissions(administrator=True)
async def admin_reset_all(interaction: discord.Interaction):
    await interaction.response.send_message("全プレイヤーのptをリセットしました", ephemeral=False)

@tree.command(name="admin_set_pt", description="指定プレイヤーのptを設定します")
@app_commands.checks.has_permissions(administrator=True)
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    await interaction.response.send_message(f"{user.display_name} のptを {pt} に設定しました", ephemeral=False)

@tree.command(name="ランキング", description="現在のランキングを表示します")
async def show_ranking(interaction: discord.Interaction):
    await interaction.response.send_message("ランキングを表示します", ephemeral=False)

# ---------------------------------
# on_ready でギルドに全コマンド同期
# ---------------------------------
@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    print(f"{bot.user} is ready. Guild: {GUILD_ID}")
    # 既存ギルドコマンドを全削除
    await tree.clear_commands(guild=guild)
    print("Existing commands cleared")
    # 最新定義で同期
    await tree.sync(guild=guild)
    print("Commands synced to guild")
    await bot.close()  # 実行後は終了

# -------------------------
# 実行
# -------------------------
bot.run(DISCORD_TOKEN)
