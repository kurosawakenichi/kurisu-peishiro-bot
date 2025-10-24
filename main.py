# refresh_commands.py
import os
import asyncio
import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

class RefreshClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f"{self.user} is ready. Guild ID: {GUILD_ID}")

        guild = discord.Object(id=GUILD_ID)

        # ギルド単位で旧コマンドを削除
        try:
            await self.tree.clear_commands(guild=guild)
            print("旧コマンドをクリアしました")
        except Exception as e:
            print("clear_commands エラー:", e)

        # ギルドに新しいコマンドを同期
        try:
            await self.tree.sync(guild=guild)
            print("新しいコマンドをギルドに同期しました")
        except Exception as e:
            print("sync エラー:", e)

        # 終了
        await self.close()

client = RefreshClient()
asyncio.run(client.start(TOKEN))
