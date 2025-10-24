import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import random
from datetime import datetime, timedelta

# Variables from Railway
GUILD_ID = int(os.environ["GUILD_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# Bot setup
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# Data storage
players = {}  # user_id: {"pt": int, "last_update": datetime}
match_requests = {}  # user_id: datetime of request
drawing_list = set()
in_match = {}  # user_id: opponent_id

# Rank thresholds for roles
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

# --- Utility Functions ---
def get_rank_role(pt):
    for low, high, name, emoji in rank_roles:
        if low <= pt <= high:
            return f"{emoji} {name}"
    return "🔰 Beginner"

async def update_nickname(member):
    pt = players.get(member.id, {}).get("pt", 0)
    role_str = get_rank_role(pt)
    nickname = f"{member.name} {role_str} {pt}pt"
    try:
        await member.edit(nick=nickname)
    except discord.Forbidden:
        # Bot cannot change this member's nickname
        pass

def remove_from_lists(user_id):
    match_requests.pop(user_id, None)
    drawing_list.discard(user_id)
    opponent = in_match.pop(user_id, None)
    if opponent:
        in_match.pop(opponent, None)

# --- Events ---
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await tree.clear_commands(guild=guild)
        await tree.sync(guild=guild)
        print("Commands cleared and synced for guild")
    else:
        print("指定したGUILD_IDのギルドが取得できません")

# --- Commands ---
@tree.command(name="マッチ希望", description="ランダムマッチに参加")
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    now = datetime.utcnow()
    if user_id in in_match:
        await interaction.response.send_message("既にマッチ中です", ephemeral=True)
        return
    match_requests[user_id] = now
    await interaction.response.send_message("マッチング中です", ephemeral=True)

    drawing_list.add(user_id)

    await asyncio.sleep(5)  # 待機時間（抽選演出なし）

    # ランダムマッチング
    candidates = list(drawing_list)
    random.shuffle(candidates)
    paired = set()
    for i in range(0, len(candidates)-1, 2):
        a, b = candidates[i], candidates[i+1]
        # check pt difference <= 4
        pt_a = players.get(a, {}).get("pt", 0)
        pt_b = players.get(b, {}).get("pt", 0)
        if abs(pt_a - pt_b) <= 4:
            in_match[a] = b
            in_match[b] = a
            paired.update([a,b])
            drawing_list.discard(a)
            drawing_list.discard(b)
            user_a = interaction.guild.get_member(a)
            user_b = interaction.guild.get_member(b)
            msg = f"{user_a.mention} vs {user_b.mention} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください"
            await user_a.send(msg)
            await user_b.send(msg)

    # 余りは希望リストに残すが抽選リストから削除
    drawing_list.difference_update(paired)

@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます")
async def cancel_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in match_requests:
        match_requests.pop(user_id)
        drawing_list.discard(user_id)
        await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望は存在しません", ephemeral=True)

@tree.command(name="結果報告", description="勝者申告")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = in_match.get(winner.id)
    if loser_id is None:
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします。", ephemeral=True)
        return
    loser = interaction.guild.get_member(loser_id)

    # 結果を待つボタン付きメッセージ
    embed = discord.Embed(title="マッチ結果確認", description=f"{winner.mention} が勝利しました。\n{loser.mention} は異議がある場合【異議】を押してください。")
    # 実際はボタン付きUIをここに追加
    await interaction.response.send_message(embed=embed, ephemeral=False)

    # 5分後、異議がなければ勝者決定
    await asyncio.sleep(300)
    # 実装上は異議フラグをチェック。フラグなしならpt加減算
    players[winner.id] = {"pt": players.get(winner.id, {}).get("pt",0)+1, "last_update": datetime.utcnow()}
    players[loser.id] = {"pt": max(players.get(loser.id, {}).get("pt",0)-1,0), "last_update": datetime.utcnow()}

    remove_from_lists(winner.id)
    remove_from_lists(loser.id)

@tree.command(name="admin_reset_all", description="全プレイヤーptリセット（管理者専用）")
async def admin_reset_all(interaction: discord.Interaction):
    if str(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    for uid in players.keys():
        players[uid]["pt"] = 0
    await interaction.response.send_message("全プレイヤーのptをリセットしました", ephemeral=False)

@tree.command(name="admin_set_pt", description="プレイヤーのpt設定（管理者専用）")
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if str(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    players[member.id] = {"pt": pt, "last_update": datetime.utcnow()}
    await update_nickname(member)
    await interaction.response.send_message(f"{member.display_name} のptを {pt} に設定しました", ephemeral=False)

@tree.command(name="ランキング", description="全ユーザーランキング表示")
async def show_ranking(interaction: discord.Interaction):
    ranking = sorted(players.items(), key=lambda x: -x[1]["pt"])
    result_lines = []
    rank_number = 1
    prev_pt = None
    for i, (uid, pdata) in enumerate(ranking, start=1):
        if pdata["pt"] != prev_pt:
            rank_number = i
        member = interaction.guild.get_member(uid)
        role_str = get_rank_role(pdata["pt"])
        result_lines.append(f"{rank_number}位 {member.display_name} {role_str} {pdata['pt']}pt")
        prev_pt = pdata["pt"]
    embed = discord.Embed(title="🏆 ランキング", description="\n".join(result_lines))
    await interaction.response.send_message(embed=embed, ephemeral=False)

# --- Run bot ---
bot.run(DISCORD_TOKEN)
