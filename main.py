import os
import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

class ClearCommandsClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        # ①ローカルツリーから全部削除
        self.tree._guild_commands[guild.id] = []  # ← 強制的に空にする
        # ②そのまま同期（＝サーバーからも全部消える）
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        print(f"{self.user} is ready. Guild ID: {GUILD_ID}")
        print("ギルドコマンドを全て削除しました。")
        await self.close()

if __name__ == "__main__":
    client = ClearCommandsClient()
    client.run(TOKEN)
