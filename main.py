import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
from datetime import datetime, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
DATA_FILE = "players.json"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

players = {}
event_start = None

RANKS = [(0, 4, "Beginner🔰"), (5, 9, "Silver🥈"), (10, 14, "Gold🥇"),
         (15, 19, "Master⚔️"), (20, 24, "GroundMaster🪽"), (25, float('inf'), "Challenger😈")]

MATCH_CHANNELS = ["beginner", "silver", "gold", "master", "groundmaster", "challenger"]

# ----- データ読み書き -----

def load_data():
    global players
    try:
        with open(DATA_FILE, "r") as f:
            players = json.load(f)
    except FileNotFoundError:
        players = {}


def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(players, f)

# ----- ユーザー階級計算 -----

def get_rank(pt):
    for low, high, name in RANKS:
        if low <= pt <= high:
            return name

# ----- メンション付きユーザー名更新 -----

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member is None:
        return
    pt = players[user_id]['pt']
    challenge = "🔥" if players[user_id].get("challenge") else ""
    rank = get_rank(pt)
    new_name = f"{rank}{challenge} {member.name}"
    try:
        await member.edit(nick=new_name)
    except discord.Forbidden:
        pass

# ----- コマンド同期＋古いコマンド削除 -----

@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    guild = discord.Object(id=GUILD_ID)
    await tree.clear_commands(guild=guild)
    print("古いコマンドを全削除しました。")
    await tree.sync(guild=guild)
    print("ギルドにコマンド同期完了")
    load_data()
    ranking_loop.start()

# ----- ランキング自動投稿 -----

@tasks.loop(minutes=1)
async def ranking_loop():
    now = datetime.utcnow()
    if now.hour in [6, 13] and now.minute == 0:  # UTCで15:00/22:00 JSTに対応
        await post_ranking()

async def post_ranking():
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="ランキング")
    if channel is None:
        return
    msg = "**ランキング**\n"
    sorted_players = sorted(players.items(), key=lambda x: x[1]['pt'], reverse=True)
    for uid, pdata in sorted_players:
        member = guild.get_member(int(uid))
        if member:
            rank = get_rank(pdata['pt'])
            challenge = "🔥" if pdata.get('challenge') else ""
            msg += f"{member.display_name}: {pdata['pt']}pt ({rank}{challenge})\n"
    await channel.send(msg)

# ----- イベント設定 -----

@tree.command(name="イベント設定", description="イベントの開始・終了日時を設定")
@app_commands.describe(start="開始日時(YYYY-MM-DD HH:MM)", end="終了日時(YYYY-MM-DD HH:MM)")
async def set_event(interaction: discord.Interaction, start: str, end: str):
    global event_start
    event_start = datetime.strptime(start, "%Y-%m-%d %H:%M")
    await interaction.response.send_message(f"イベント開始: {start} 終了: {end}")

# ----- マッチング申請 -----

@tree.command(name="マッチング申請", description="対戦相手に申請")
@app_commands.describe(opponent="対戦相手")
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    await interaction.response.send_message(f"{interaction.user.mention} vs {opponent.mention} の申請を送信しました。承認待ちです。")

# ----- 試合結果報告 -----

@tree.command(name="試合結果報告", description="勝者が結果報告")
@app_commands.describe(opponent="対戦相手")
async def report(interaction: discord.Interaction, opponent: discord.Member):
    winner_id = str(interaction.user.id)
    loser_id = str(opponent.id)
    if winner_id not in players:
        players[winner_id] = {"pt":0}
    if loser_id not in players:
        players[loser_id] = {"pt":0}
    # PT計算
    players[winner_id]['pt'] += 1
    players[loser_id]['pt'] = max(0, players[loser_id]['pt']-1)
    save_data()
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="ランキング")
    await channel.send(f"🔥 {interaction.user.mention} が昇級しました！")
    await interaction.response.send_message(f"対戦結果を反映しました。@kurosawa0118 へご報告ください")

bot.run(TOKEN)
