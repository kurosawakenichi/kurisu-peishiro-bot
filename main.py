import os
import discord
from discord import app_commands
import asyncio

# 環境変数から取得
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

class ClearCommandsClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # ギルド単位で削除
        guild = discord.Object(id=GUILD_ID)
        await self.tree.sync(guild=guild)  # 現状を同期
        await self.tree.clear_commands(guild=guild)
        print(f"ギルド {GUILD_ID} のコマンドをクリアしました")

        # グローバル削除
        await self.tree.sync()  # 現状を同期
        await self.tree.clear_commands(guild=None)
        print("グローバルコマンドをクリアしました")

        # 完了後にBot停止
        await self.close()

async def main():
    client = ClearCommandsClient()
    await client.start(TOKEN)

# Railwayでも安全に実行できるように asyncio.run で起動
asyncio.run(main())
