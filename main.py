import os
import discord
from discord import app_commands

# 環境変数から取得
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

class ClearCommandsClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f"{self.user} is ready. Guild ID: {GUILD_ID}")
        # ギルドオブジェクトを取得
        guild = discord.Object(id=GUILD_ID)
        try:
            # ギルド上の全コマンドをクリア
            await self.tree.clear_commands(guild=guild)
            print("ギルドコマンドを全て削除しました。")
        except Exception as e:
            print("clear_commands エラー:", e)
        # Botを終了
        await self.close()

if __name__ == "__main__":
    client = ClearCommandsClient()
    client.run(TOKEN)
