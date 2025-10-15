import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, time
from zoneinfo import ZoneInfo

# 環境変数から
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = 1427542200614387846

# タイムゾーン
JST = ZoneInfo("Asia/Tokyo")

# 階級設定
RANKS = [
    (0, 4, "Beginner🔰"),
    (5, 9, "Silver🥈"),
    (10, 14, "Gold🥇"),
    (15, 19, "Master⚔️"),
    (20, 24, "GroundMaster🪽"),
    (25, float("inf"), "Challenger😈")
]

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# データ保持（JSONやDBは使わずメモリ上）
players = {}  # user_id: {"pt": int, "rank": str, "challenge": bool}

# --- ユーティリティ関数 ---
def get_rank(pt: int) -> str:
    for low, high, name in RANKS:
        if low <= pt <= high:
            return name
    return "Unknown"

async def update_member_display(user_id: int):
    """ユーザー名の隣に階級・ポイント・チャレンジ🔥を表示"""
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if not member:
        return
    data = players.get(user_id)
    if not data:
        return
    suffix = f"{data['rank']} | {data['pt']}pt"
    if data.get("challenge"):
        suffix += " 🔥"
    try:
        await member.edit(nick=f"{member.name} {suffix}")
    except discord.Forbidden:
        pass  # 権限がなければスキップ

def generate_ranking_text() -> str:
    sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    lines = ["🏆 ランキング 🏆"]
    for idx, (uid, pdata) in enumerate(sorted_players, start=1):
        user = bot.get_user(uid)
        uname = user.name if user else f"<@{uid}>"
        lines.append(f"{idx}. {uname} {pdata['rank']} | {pdata['pt']}pt")
    return "\n".join(lines)

# --- 自動投稿タスク ---
@tasks.loop(time=[time(13, 0, tzinfo=JST), time(22, 0, tzinfo=JST)])
async def post_ranking():
    channel = bot.get_channel(RANKING_CHANNEL_ID)
    if channel:
        text = generate_ranking_text()
        await channel.send(text)

# --- Botイベント ---
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    await bot.wait_until_ready()
    post_ranking.start()
    print("ランキング自動投稿タスク開始")

# --- コマンド ---
@tree.command(name="イベント設定", description="イベントの開始・終了日時を設定")
@app_commands.checks.has_permissions(administrator=True)
async def event_setting(interaction: discord.Interaction, start: str, end: str):
    await interaction.response.send_message(f"イベント設定完了: {start} ～ {end}")

@tree.command(name="マッチング申請", description="他プレイヤーにマッチング申請")
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    # 承認ボタンを相手のみ表示
    if opponent.bot or opponent.id == interaction.user.id:
        await interaction.response.send_message("無効な相手です。")
        return

    class ApproveView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="承認", style=discord.ButtonStyle.green)
        async def approve(self, button: discord.ui.Button, i: discord.Interaction):
            if i.user.id != opponent.id:
                await i.response.send_message("あなたは承認できません", ephemeral=True)
                return
            # 承認処理
            await i.response.edit_message(content=f"{opponent.name}が承認しました。", view=None)

    view = ApproveView()
    await interaction.response.send_message(f"{interaction.user.name}さんにマッチング申請しました。承認を待ってください。", view=view)

@tree.command(name="試合結果報告", description="勝者を報告")
async def match_report(interaction: discord.Interaction, winner: discord.Member):
    loser = None  # 簡略化
    await interaction.response.send_message(f"勝者: {winner.name}, 敗者: {loser.name if loser else '未設定'}")

@tree.command(name="pt操作", description="管理者がプレイヤーのptを操作")
@app_commands.checks.has_permissions(administrator=True)
async def modify_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    uid = member.id
    if uid not in players:
        players[uid] = {"pt": 0, "rank": get_rank(0), "challenge": False}
    players[uid]["pt"] = pt
    players[uid]["rank"] = get_rank(pt)
    await update_member_display(uid)
    await interaction.response.send_message(f"{member.name} のPTを {pt} に設定しました。")

# --- 同期エラー回避 ---
@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
    elif isinstance(error, app_commands.errors.CommandSignatureMismatch):
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        await interaction.response.send_message("コマンド同期しました。再度実行してください。", ephemeral=True)
    else:
        await interaction.response.send_message(f"エラー: {error}", ephemeral=True)

# --- Bot起動 ---
bot.run(TOKEN)
