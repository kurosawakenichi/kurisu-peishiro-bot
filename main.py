import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta, time
import pytz

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = 1427542200614387846

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# データ構造
players = {}  # {user_id: {"pt": int, "challenge": bool}}

# 階級定義
RANKS = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, float('inf'), "Challenger", "😈")
]

ADMIN_ID = 141  # サーバー管理者のDiscord IDに変更してください

# ユーザー表示更新
async def update_member_display(user_id: int):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if not member:
        print(f"[WARN] member {user_id} not found")
        return
    player = players.get(user_id)
    if not player:
        return
    # 階級取得
    pt = player["pt"]
    challenge = player.get("challenge", False)
    rank_name, rank_emoji = "", ""
    for low, high, name, emoji in RANKS:
        if low <= pt <= high:
            rank_name = name
            rank_emoji = emoji
            break
        elif pt >= 25:
            rank_name, rank_emoji = "Challenger", "😈"
            break
    suffix = f"{rank_emoji} {pt}"
    if challenge:
        suffix += " 🔥"
    try:
        await member.edit(nick=f"{member.name} | {suffix}")
        print(f"[INFO] Updated {member.name} -> {suffix}")
    except Exception as e:
        print(f"[ERROR] Failed to update {member.name}: {e}")

# イベント設定
@tree.command(name="イベント設定", description="イベント開始/終了日時設定（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def set_event(interaction: discord.Interaction, start: str, end: str):
    await interaction.response.send_message(f"イベント設定: {start} ~ {end}", ephemeral=True)

# マッチング申請
match_requests = {}  # {challenger_id: target_id}

@tree.command(name="マッチング申請", description="対戦申請")
async def match_request(interaction: discord.Interaction, target: discord.Member):
    challenger = interaction.user
    if target.id == challenger.id:
        await interaction.response.send_message("自分には申請できません", ephemeral=True)
        return
    match_requests[challenger.id] = target.id
    view = discord.ui.View()
    button = discord.ui.Button(label="承認", style=discord.ButtonStyle.green)
    async def button_callback(btn_interaction):
        if btn_interaction.user.id != target.id:
            await btn_interaction.response.send_message("申請されたユーザーのみ承認できます", ephemeral=True)
            return
        await btn_interaction.response.send_message("承認されました", ephemeral=True)
        # ここで対戦登録やpt処理
        del match_requests[challenger.id]
    button.callback = button_callback
    view.add_item(button)
    await interaction.response.send_message(f"{target.mention} にマッチング申請しました。承認を待ってください。", view=view)

# 試合結果報告
@tree.command(name="試合結果報告", description="勝者が試合結果を報告")
async def report_result(interaction: discord.Interaction, loser: discord.Member):
    winner_id = interaction.user.id
    loser_id = loser.id
    # 承認済みか確認
    if match_requests.get(winner_id) != loser_id:
        await interaction.response.send_message("この試合のマッチング承認がありません", ephemeral=True)
        return
    # pt計算 (簡略化例)
    players.setdefault(winner_id, {"pt": 0, "challenge": False})
    players.setdefault(loser_id, {"pt": 0, "challenge": False})
    players[winner_id]["pt"] += 1
    players[loser_id]["pt"] = max(0, players[loser_id]["pt"] - 1)
    # 表示更新
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    await interaction.response.send_message(f"結果反映完了: {interaction.user.mention}が勝利")

# 管理者用 pt操作
@tree.command(name="pt操作", description="管理者が任意のユーザーptを操作")
async def pt_adjust(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用可", ephemeral=True)
        return
    players.setdefault(target.id, {"pt": 0, "challenge": False})
    players[target.id]["pt"] = pt
    await update_member_display(target.id)
    await interaction.response.send_message(f"{target.display_name} のptを {pt} に設定しました", ephemeral=True)

# ランキング投稿タスク
@tasks.loop(time=[time(13,0,0,tzinfo=pytz.timezone("Asia/Tokyo")), time(22,0,0,tzinfo=pytz.timezone("Asia/Tokyo"))])
async def post_ranking():
    guild = bot.get_guild(GUILD_ID)
    ch = guild.get_channel(RANKING_CHANNEL_ID)
    if not ch:
        print("[WARN] ランキングチャンネル取得失敗")
        return
    ranking = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    msg = "**ランキング**\n"
    for uid, data in ranking:
        member = guild.get_member(uid)
        if member:
            pt = data["pt"]
            rank_name, rank_emoji = "", ""
            for low, high, name, emoji in RANKS:
                if low <= pt <= high or pt >=25:
                    rank_name, rank_emoji = name, emoji
                    break
            challenge = data.get("challenge", False)
            msg += f"{member.display_name}: {rank_emoji} {pt}"
            if challenge:
                msg += " 🔥"
            msg += "\n"
    await ch.send(msg)

@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("[INFO] ギルドコマンド同期完了")
    except Exception as e:
        print(f"[ERROR] コマンド同期エラー: {e}")
    post_ranking.start()

bot.run(TOKEN)
