import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands

# Variables
GUILD_ID = int(os.environ["GUILD_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# 内部リスト管理
match_request_list = {}  # {user_id: timestamp}
drawing_list = []        # [user_id]
in_match = {}            # {user_id: opponent_id}

# ランク制限用
RANKS = [(0, 4, "Beginner", "🔰"),
         (5, 9, "Silver", "🥈"),
         (10, 14, "Gold", "🥇"),
         (15, 19, "Master", "⚔️"),
         (20, 24, "GrandMaster", "🪽"),
         (25, 999, "Challenger", "😈")]

# PT管理
user_pt = {}  # {user_id: pt}

# Utility functions
def get_rank(pt: int):
    for low, high, name, emoji in RANKS:
        if low <= pt <= high:
            return name, emoji
    return "Unknown", "❓"

def can_match(user1_id, user2_id):
    pt1 = user_pt.get(user1_id, 0)
    pt2 = user_pt.get(user2_id, 0)
    # 階級差チェック
    for low, high, _, _ in RANKS:
        if low <= pt1 <= high:
            rank1 = (low, high)
        if low <= pt2 <= high:
            rank2 = (low, high)
    # 同rank帯のみマッチ可能
    return rank1 == rank2

# コマンド同期処理
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")

    # ギルド単位で旧コマンドをクリア
    guild = discord.Object(id=GUILD_ID)
    await tree.clear_commands(guild=guild)

    # 新コマンド同期
    await tree.sync(guild=guild)
    print("Commands synced")

# /マッチ希望
@tree.command(name="マッチ希望", description="ランダムマッチ希望", guild=discord.Object(id=GUILD_ID))
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in in_match:
        await interaction.response.send_message("既に対戦中です。", ephemeral=True)
        return
    if user_id in match_request_list:
        await interaction.response.send_message("既にマッチ希望リストに登録されています。", ephemeral=True)
        return

    match_request_list[user_id] = asyncio.get_event_loop().time()
    await interaction.response.send_message("マッチング中です", ephemeral=True)

    # 抽選処理 5秒
    drawing_list.append(user_id)
    await asyncio.sleep(5)

    # ランダム組み合わせ
    paired = set()
    import random
    random.shuffle(drawing_list)
    for i in range(0, len(drawing_list) - 1, 2):
        u1 = drawing_list[i]
        u2 = drawing_list[i+1]
        if can_match(u1, u2):
            in_match[u1] = u2
            in_match[u2] = u1
            paired.add(u1)
            paired.add(u2)
            user1 = await bot.fetch_user(u1)
            user2 = await bot.fetch_user(u2)
            await user1.send(f"{user1.name} vs {user2.name} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください")
            await user2.send(f"{user1.name} vs {user2.name} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください")
            # 希望リストから削除
            if u1 in match_request_list: del match_request_list[u1]
            if u2 in match_request_list: del match_request_list[u2]

    # 余りは残す
    drawing_list.clear()

# /マッチ希望取下げ
@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げる", guild=discord.Object(id=GUILD_ID))
async def cancel_match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in match_request_list:
        del match_request_list[user_id]
        await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望リストにありません", ephemeral=True)

# /結果報告
@tree.command(name="結果報告", description="勝者申告", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = in_match.get(winner.id)
    if not loser_id:
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします。", ephemeral=True)
        return
    # 審議無しで承認処理
    winner_pt = user_pt.get(winner.id, 0)
    loser_pt = user_pt.get(loser_id, 0)
    user_pt[winner.id] = winner_pt + 1
    user_pt[loser_id] = max(loser_pt - 1, 0)
    # in_matchから除外
    del in_match[winner.id]
    del in_match[loser_id]
    await interaction.response.send_message(f"勝者 {winner.name} の勝利が登録されました。", ephemeral=False)

# /admin_set_pt
@tree.command(name="admin_set_pt", description="指定ユーザーのPTを設定", guild=discord.Object(id=GUILD_ID))
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != int(ADMIN_ID):
        await interaction.response.send_message("管理者のみ実行可能です", ephemeral=True)
        return
    user_pt[member.id] = pt
    await interaction.response.send_message(f"{member.name} のPTを {pt} に設定しました", ephemeral=False)

# /admin_reset_all
@tree.command(name="admin_reset_all", description="全ユーザーのPTをリセット", guild=discord.Object(id=GUILD_ID))
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != int(ADMIN_ID):
        await interaction.response.send_message("管理者のみ実行可能です", ephemeral=True)
        return
    for k in user_pt.keys():
        user_pt[k] = 0
    await interaction.response.send_message("全プレイヤーのPTをリセットしました", ephemeral=False)

# /ランキング
@tree.command(name="ランキング", description="全ユーザーのランキング表示", guild=discord.Object(id=GUILD_ID))
async def ranking(interaction: discord.Interaction):
    # 標準競争順位
    sorted_users = sorted(user_pt.items(), key=lambda x: -x[1])
    result_lines = []
    last_pt = None
    rank = 0
    display_rank = 0
    for idx, (user_id, pt) in enumerate(sorted_users):
        if pt != last_pt:
            display_rank = idx + 1
            last_pt = pt
        member = await bot.fetch_user(user_id)
        _, emoji = get_rank(pt)
        result_lines.append(f"{display_rank}位 {member.name} {emoji} {pt}pt")
    text = "🏆 ランキング\n" + "\n".join(result_lines)
    await interaction.response.send_message(text, ephemeral=False)

bot.run(DISCORD_TOKEN)
