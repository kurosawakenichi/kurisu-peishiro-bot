import os
import discord
from discord.ext import tasks
from discord import app_commands
import asyncio
import random
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

GUILD_ID = int(os.environ.get("GUILD_ID"))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID"))
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

# 内部管理
players = {}  # user_id -> {"pt": int, "role_emoji": str}
match_waiting = {}  # user_id -> expiration_datetime
draw_list = []  # user_id
in_match = {}  # (player1_id, player2_id) -> {}
match_lock = asyncio.Lock()

# ランク表
pt_to_role = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 999, "Challenger", "😈")
]

def get_role_emoji(pt):
    for start, end, _, emoji in pt_to_role:
        if start <= pt <= end:
            return emoji
    return "🔰"

def get_player_data(user_id):
    if user_id not in players:
        players[user_id] = {"pt": 0, "role_emoji": get_role_emoji(0)}
    return players[user_id]

async def update_nickname(member: discord.Member, pt: int):
    try:
        role_emoji = get_role_emoji(pt)
        await member.edit(nick=f"{member.name} {role_emoji} {pt}pt")
    except discord.Forbidden:
        # 管理者権限のないユーザーは変更不可
        pass

@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)

# ---------- /マッチ希望 ----------
@tree.command(name="マッチ希望", description="ランダムマッチ希望を出します")
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    player_data = get_player_data(user_id)

    async with match_lock:
        now = datetime.utcnow()
        match_waiting[user_id] = now + timedelta(minutes=5)
        await interaction.response.send_message("マッチング中です...", ephemeral=True)

        # 抽選処理
        draw_list.append(user_id)
        await asyncio.sleep(5)

        available = list(draw_list)
        random.shuffle(available)
        paired = []
        while len(available) >= 2:
            p1 = available.pop()
            # 階級差制限チェック
            p1_pt = get_player_data(p1)["pt"]
            for i, p2 in enumerate(available):
                p2_pt = get_player_data(p2)["pt"]
                if abs(p1_pt - p2_pt) <= 2:  # 階級差制限
                    paired.append((p1, p2))
                    available.pop(i)
                    break

        for p1, p2 in paired:
            in_match[(p1, p2)] = {"start": datetime.utcnow()}
            draw_list.remove(p1)
            draw_list.remove(p2)
            if p1 in match_waiting:
                del match_waiting[p1]
            if p2 in match_waiting:
                del match_waiting[p2]
            user1 = await bot.fetch_user(p1)
            user2 = await bot.fetch_user(p2)
            await user1.send(f"{user1.name} vs {user2.name} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください")
            await user2.send(f"{user1.name} vs {user2.name} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください")

        # 余りユーザーは5分間マッチ希望リストに残す

# ---------- /マッチ希望取下げ ----------
@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます")
async def cancel_match(interaction: discord.Interaction):
    user_id = interaction.user.id
    async with match_lock:
        removed = False
        if user_id in match_waiting:
            del match_waiting[user_id]
            removed = True
        if user_id in draw_list:
            draw_list.remove(user_id)
            removed = True
        if removed:
            await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
        else:
            await interaction.response.send_message("マッチ希望は存在しません", ephemeral=True)

# ---------- /結果報告 ----------
@tree.command(name="結果報告", description="勝者が申告します")
@app_commands.describe(winner="勝者")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = None
    for (p1, p2), info in in_match.items():
        if winner.id in (p1, p2):
            loser_id = p2 if winner.id == p1 else p1
            match_key = (p1, p2)
            break
    else:
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします。", ephemeral=True)
        return

    # 審議チャンネルへの通知などの処理
    # 承認／異議ボタンの有効期限5分は内部で管理
    # 異議発生時は in_match から除外、pendingは不要

# ---------- 管理者コマンド ----------
@tree.command(name="admin_reset_all", description="全プレイヤーのptをリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    for uid in players:
        players[uid]["pt"] = 0
        players[uid]["role_emoji"] = get_role_emoji(0)
        member = await bot.fetch_user(uid)
        await update_nickname(member, 0)
    await interaction.response.send_message("全プレイヤーのptをリセットしました", ephemeral=False)

@tree.command(name="admin_set_pt", description="指定プレイヤーのptを設定")
@app_commands.describe(user="ユーザー", pt="pt値")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    pdata = get_player_data(user.id)
    pdata["pt"] = pt
    pdata["role_emoji"] = get_role_emoji(pt)
    await update_nickname(user, pt)
    await interaction.response.send_message(f"{user.name} のptを {pt} に設定しました", ephemeral=False)

# ---------- /ランキング ----------
@tree.command(name="ランキング", description="全プレイヤーのランキングを表示")
async def show_ranking(interaction: discord.Interaction):
    ranking_list = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    ranks = []
    last_pt = None
    last_rank = 0
    for i, (uid, pdata) in enumerate(ranking_list):
        if pdata["pt"] != last_pt:
            last_rank = i + 1
        last_pt = pdata["pt"]
        ranks.append(f"{last_rank}位 <@{uid}> {pdata['role_emoji']} {pdata['pt']}pt")
    await interaction.response.send_message("🏆 ランキング\n" + "\n".join(ranks), ephemeral=False)

bot.run(DISCORD_TOKEN)
