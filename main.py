import discord
import os

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

class ClearCommandsClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        # ギルド内の全コマンド取得
        commands = await self.tree.fetch_commands(guild=guild)
        if commands:
            for cmd in commands:
                await self.tree.delete_command(cmd.id, guild=guild)
            print(f"ギルド内の {len(commands)} 件のコマンドを削除しました")
        else:
            print("削除するコマンドはありません")
        await self.close()  # 終了

client = ClearCommandsClient()
client.run(TOKEN)
