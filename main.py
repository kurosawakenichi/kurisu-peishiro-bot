import discord
from discord.ext import tasks
from discord import app_commands
import os
import asyncio
import random
from datetime import datetime, timedelta

# -----------------------------
# 設定
# -----------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# プレイヤーデータ
# -----------------------------
players_pt = {}  # {user_id: pt}

# -----------------------------
# ランク定義
# -----------------------------
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

# -----------------------------
# マッチングデータ
# -----------------------------
match_request_list = {}  # {user_id: timestamp}
match_draw_list = set()  # 現在抽選中
in_match = {}  # {user_id: opponent_id}

MATCH_WAIT_SEC = 5
REQUEST_EXPIRE_MIN = 5

# -----------------------------
# 起動時イベント
# -----------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready")
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)  # ギルド単位で最新コマンド同期
    print("Commands synced")
    ranking_auto_post.start()

# -----------------------------
# 管理者コマンド
# -----------------------------
@tree.command(name="admin_set_pt", description="プレイヤーpt設定")
@app_commands.describe(member="対象ユーザー", pt="設定するpt")
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    players_pt[member.id] = max(pt, 0)
    await update_member_nickname(member)
    await interaction.response.send_message(f"{member.display_name} のptを {pt} に設定しました", ephemeral=False)

@tree.command(name="admin_reset_all", description="全プレイヤーptリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    for user_id in players_pt.keys():
        players_pt[user_id] = 0
    for member in interaction.guild.members:
        await update_member_nickname(member)
    await interaction.response.send_message("全プレイヤーのptをリセットしました", ephemeral=False)

# -----------------------------
# ランキング表示
# -----------------------------
@tree.command(name="ランキング", description="ランキング表示")
async def show_ranking(interaction: discord.Interaction):
    ranked = sorted(players_pt.items(), key=lambda x: x[1], reverse=True)
    output = "🏆 ランキング\n"
    last_pt = None
    rank = 0
    skip = 1
    for user_id, pt in ranked:
        member = interaction.guild.get_member(user_id)
        if member is None:
            continue
        if pt != last_pt:
            rank += skip
            skip = 1
        else:
            skip += 1
        last_pt = pt
        role_icon = get_role_icon(pt)
        output += f"{rank}位 {member.display_name} {role_icon} {pt}pt\n"
    await interaction.response.send_message(output, ephemeral=False)

# -----------------------------
# マッチ希望
# -----------------------------
@tree.command(name="マッチ希望", description="マッチ希望を出す")
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    now = datetime.utcnow()
    # 登録
    match_request_list[user_id] = now
    await interaction.response.send_message("マッチング中です", ephemeral=True)
    await try_match_users(interaction.guild)

async def try_match_users(guild: discord.Guild):
    # 期限切れは削除
    now = datetime.utcnow()
    expired = [uid for uid, ts in match_request_list.items() if now - ts > timedelta(minutes=REQUEST_EXPIRE_MIN)]
    for uid in expired:
        member = guild.get_member(uid)
        if member:
            await member.send("マッチング相手が見つかりませんでした")
        match_request_list.pop(uid)

    # 抽選
    waiting_users = list(match_request_list.keys())
    if len(waiting_users) < 2:
        return
    random.shuffle(waiting_users)
    draw_list = waiting_users[:]
    await asyncio.sleep(MATCH_WAIT_SEC)  # 待機
    # ペア作成
    while len(draw_list) >= 2:
        a = draw_list.pop()
        b = draw_list.pop()
        member_a = guild.get_member(a)
        member_b = guild.get_member(b)
        if member_a and member_b:
            in_match[a] = b
            in_match[b] = a
            match_request_list.pop(a)
            match_request_list.pop(b)
            msg = f"{member_a.mention} vs {member_b.mention} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください"
            await guild.get_channel(JUDGE_CHANNEL_ID).send(msg)

# -----------------------------
# マッチ希望取り下げ
# -----------------------------
@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げる")
async def cancel_match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in match_request_list:
        match_request_list.pop(user_id)
        await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望はありません", ephemeral=True)

# -----------------------------
# 結果報告
# -----------------------------
@tree.command(name="結果報告", description="勝者を報告")
@app_commands.describe(winner="勝者")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = in_match.pop(winner.id, None)
    if loser_id is None:
        await interaction.response.send_message("マッチが見つかりません", ephemeral=True)
        return
    loser = interaction.guild.get_member(loser_id)
    # 勝敗pt更新（ライト仕様: ±1）
    players_pt[winner.id] = players_pt.get(winner.id, 0) + 1
    players_pt[loser.id] = max(players_pt.get(loser.id, 0) - 1, 0)
    # ユーザー名更新
    await update_member_nickname(winner)
    await update_member_nickname(loser)
    # メッセージ
    await interaction.response.send_message(f"{winner.display_name} の勝利です", ephemeral=False)

# -----------------------------
# ユーザー名更新
# -----------------------------
async def update_member_nickname(member: discord.Member):
    pt = players_pt.get(member.id, 0)
    icon = get_role_icon(pt)
    try:
        await member.edit(nick=f"{member.name} {icon} {pt}pt")
    except:
        pass

def get_role_icon(pt: int):
    for start, end, _, icon in rank_roles:
        if start <= pt <= end:
            return icon
    return "🔰"

# -----------------------------
# ランキング自動投稿
# -----------------------------
@tasks.loop(time=[datetime.strptime("14:00","%H:%M").time(), datetime.strptime("23:00","%H:%M").time()])
async def ranking_auto_post():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    channel = guild.get_channel(JUDGE_CHANNEL_ID)
    if channel is None:
        return
    ranked = sorted(players_pt.items(), key=lambda x: x[1], reverse=True)
    output = "🏆 ランキング\n"
    last_pt = None
    rank = 0
    skip = 1
    for user_id, pt in ranked:
        member = guild.get_member(user_id)
        if member is None:
            continue
        if pt != last_pt:
            rank += skip
            skip = 1
        else:
            skip += 1
        last_pt = pt
        role_icon = get_role_icon(pt)
        output += f"{rank}位 {member.display_name} {role_icon} {pt}pt\n"
    await channel.send(output)

# -----------------------------
# Bot起動
# -----------------------------
bot.run(TOKEN)
