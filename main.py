# refresh_commands.py
import os
import discord
from discord import app_commands
import asyncio

# 環境変数から取得
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))

class RefreshBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        # CommandTreeを明示的に作成
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f"{self.user} is ready. Guild ID: {GUILD_ID}")
        guild = discord.Object(id=GUILD_ID)

        # ギルド単位で旧コマンドを削除
        await self.tree.clear_commands(guild=guild)
        print("旧コマンドをクリアしました")

        # 新しいコマンドを同期
        await self.tree.sync(guild=guild)
        print("新しいコマンドをギルドに同期しました")

        # 終了
        await self.close()

# 実行
client = RefreshBot()
client.run(TOKEN)
