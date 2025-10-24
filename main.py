# refresh_commands_safe.py
import os
import discord
from discord import app_commands

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))

class RefreshBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # setup_hook は bot が完全に初期化された後に呼ばれる
        guild = discord.Object(id=GUILD_ID)

        # ギルド単位で旧コマンドを削除
        await self.tree.clear_commands(guild=guild)
        print("旧コマンドをクリアしました")

        # 新しいコマンドをギルドに同期
        await self.tree.sync(guild=guild)
        print("新しいコマンドをギルドに同期しました")

        # 同期が終わったら bot を終了
        await self.close()

client = RefreshBot()
client.run(TOKEN)
