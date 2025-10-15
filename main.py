import os
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import datetime, timezone, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# プレイヤーデータ
players = {}

# 階級定義
RANKS = [
    (0, 4, "Beginner", ""),
    (5, 9, "Silver", ""),
    (10, 14, "Gold", ""),
    (15, 19, "Master", ""),
    (20, 24, "GroundMaster", ""),
    (25, 9999, "Challenger", ""),
]

# イベント情報
event_start = None
event_end = None

# マッチング申請
matches = {}  # {challenger_id: opponent_id}

def get_rank(pt):
    for low, high, name, _ in RANKS:
        if low <= pt <= high:
            return name
    return "Unknown"

async def update_member_display(uid):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(uid)
    if member is None or uid not in players:
        return
    pt = players[uid]["pt"]
    rank = get_rank(pt)
    # ロール更新
    role = discord.utils.get(guild.roles, name=rank)
    if role:
        try:
            await member.edit(roles=[role])
        except:
            pass

# 定期ランキング投稿
@tasks.loop(minutes=60)
async def post_ranking():
    channel = discord.utils.get(bot.get_guild(GUILD_ID).channels, name="ランキング")
    if channel and players:
        sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
        msg = "\n".join(f"{bot.get_guild(GUILD_ID).get_member(uid).display_name}: {data['pt']}pt ({get_rank(data['pt'])})"
                        for uid, data in sorted_players)
        await channel.send(f"ランキング\n{msg}")

@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    guild = discord.Object(id=GUILD_ID)
    try:
        await bot.tree.sync(guild=guild)
        print("ギルドコマンド同期完了")
    except Exception as e:
        print("コマンド同期エラー:", e)
    post_ranking.start()

@tree.command(name="イベント設定", description="イベントの開始・終了日時を設定", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(start="開始日時(YYYY-MM-DD HH:MM)", end="終了日時(YYYY-MM-DD HH:MM)")
async def set_event(interaction: discord.Interaction, start: str, end: str):
    global event_start, event_end
    try:
        event_start = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        event_end = datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        await interaction.response.send_message(f"イベント設定完了\n開始: {event_start}\n終了: {event_end}")
    except:
        await interaction.response.send_message("日時の形式が不正です。YYYY-MM-DD HH:MM で指定してください。")

@tree.command(name="マッチング申請", description="対戦相手に申請", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="対戦相手")
async def request_match(interaction: discord.Interaction, opponent: discord.Member):
    challenger = interaction.user
    if challenger.id in matches:
        await interaction.response.send_message("既に申請中です。取り下げてから再度申請してください。")
        return
    matches[challenger.id] = opponent.id
    await interaction.response.send_message(f"{opponent.mention} に対戦申請しました。承認/拒否を待ってください。")

@tree.command(name="承認", description="マッチング申請承認", guild=discord.Object(id=GUILD_ID))
async def approve_match(interaction: discord.Interaction):
    uid = interaction.user.id
    challenger_id = None
    for c, o in matches.items():
        if o == uid:
            challenger_id = c
            break
    if not challenger_id:
        await interaction.response.send_message("承認できる申請がありません。")
        return
    await interaction.response.send_message(f"マッチング承認: <@{challenger_id}> vs <@{uid}>")
    # 試合開始の処理はここに追加可能

@tree.command(name="試合結果報告", description="勝者が報告", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(winner="勝者", loser="敗者")
async def report_result(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    # マッチング承認済み確認
    if matches.get(winner.id) != loser.id:
        await interaction.response.send_message(f"申請が承認されていません。<@kurosawa0118> へご報告ください。")
        return
    # Pt 計算
    for uid in [winner.id, loser.id]:
        if uid not in players:
            players[uid] = {"pt": 0}
    players[winner.id]["pt"] += 1
    if players[loser.id]["pt"] > 0:
        players[loser.id]["pt"] -= 1
    # ロール更新
    await update_member_display(winner.id)
    await update_member_display(loser.id)
    # 承認フロー完了
    del matches[winner.id]
    await interaction.response.send_message(f"結果反映済: {winner.display_name} の勝利。")

bot.run(TOKEN)
