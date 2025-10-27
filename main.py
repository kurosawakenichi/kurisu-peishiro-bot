import os
import discord
from discord import app_commands
from discord.ext import commands

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
intents.message_content = False  # ephemeralメッセージ前提なので不要

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=None
        )

    async def setup_hook(self):
        # ここでは self.tree は既存のものを使う
        guild = discord.Object(id=GUILD_ID)
        try:
            await self.tree.sync(guild=guild)
            print("Commands synced to guild.")
        except Exception as e:
            print("Command sync error:", e)

bot = MyBot()

@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")

bot.run(DISCORD_TOKEN)
