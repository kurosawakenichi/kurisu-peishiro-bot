import os
import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    print(f"{client.user} is ready. Guild ID: {GUILD_ID}")
    
    # ギルド同期だけ
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print("コマンドをギルドに同期しました")

# ここから下は【基本 main.py ランダム】のコマンド定義やロジックをそのまま書く
# 例:
/*
@tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def match_request(interaction: discord.Interaction):
    ...
*/

client.run(TOKEN)
