import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
from datetime import datetime, timedelta

# --- 設定 ---
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID"))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- ランク・ロール定義 ---
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

def get_role_icon(pt):
    for start, end, role, icon in rank_roles:
        if start <= pt <= end:
            return role, icon
    return "Unknown", "❓"

# --- データ管理 ---
player_data = {}  # user_id -> {'pt': int}
match_requests = {}  # user_id -> request_time
draw_list = set()  # user_id
in_match = {}  # user_id -> opponent_id

MATCH_TIMEOUT = 5 * 60
DRAW_WAIT = 5  # 秒

# --- 起動時コマンド同期 ---
@bot.event
async def on_ready():
    print(f"{bot.user} is ready")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    ranking_task.start()

# --- ユーティリティ ---
async def update_nickname(member, pt):
    role_name, icon = get_role_icon(pt)
    try:
        await member.edit(nick=f"{member.name} {icon} {pt}pt")
    except:
        pass  # 管理者や権限不足で更新できない場合は無視

def standard_ranking():
    sorted_players = sorted(player_data.items(), key=lambda x: x[1]['pt'], reverse=True)
    ranking = []
    last_pt = None
    last_rank = 0
    for idx, (user_id, data) in enumerate(sorted_players, start=1):
        if data['pt'] == last_pt:
            rank = last_rank
        else:
            rank = idx
            last_pt = data['pt']
            last_rank = rank
        ranking.append((rank, user_id, data['pt']))
    return ranking

async def post_ranking(channel):
    ranking = standard_ranking()
    lines = ["🏆 ランキング"]
    for rank, user_id, pt in ranking:
        member = channel.guild.get_member(user_id)
        role, icon = get_role_icon(pt)
        if member:
            lines.append(f"{rank}位 {member.display_name} {icon} {pt}pt")
    await channel.send("\n".join(lines))

# --- 自動ランキングタスク ---
@tasks.loop(hours=9)  # JST 14:00 / 23:00に合わせるには外部調整
async def ranking_task():
    channel = bot.get_channel(JUDGE_CHANNEL_ID)
    if channel:
        await post_ranking(channel)

# --- 管理者コマンド ---
@bot.tree.command(name="admin_reset_all", description="全プレイヤーptリセット")
async def admin_reset_all(interaction: discord.Interaction):
    for user_id in player_data:
        player_data[user_id]['pt'] = 0
        member = interaction.guild.get_member(user_id)
        if member:
            await update_nickname(member, 0)
    await interaction.response.send_message("全プレイヤーのptをリセットしました", ephemeral=True)

@bot.tree.command(name="admin_set_pt", description="プレイヤーpt設定")
@app_commands.describe(member="対象ユーザー", pt="設定するpt")
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if member.id not in player_data:
        player_data[member.id] = {'pt': 0}
    player_data[member.id]['pt'] = pt
    await update_nickname(member, pt)
    await interaction.response.send_message(f"{member.display_name} のptを {pt} に設定しました", ephemeral=True)

@bot.tree.command(name="ランキング", description="ランキング表示")
async def ranking(interaction: discord.Interaction):
    await post_ranking(interaction.channel)
    await interaction.response.send_message("ランキングを表示しました", ephemeral=True)

# --- マッチ希望 ---
@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望を出す")
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    now = datetime.utcnow()
    match_requests[user_id] = now
    draw_list.add(user_id)
    await interaction.response.send_message("マッチング中です...", ephemeral=True)

    await asyncio.sleep(DRAW_WAIT)

    # 抽選処理
    ready_players = list(draw_list)
    random.shuffle(ready_players)
    matched = set()
    for i in range(0, len(ready_players) - 1, 2):
        a, b = ready_players[i], ready_players[i + 1]
        # 階級差制限チェック
        pt_a, pt_b = player_data.get(a, {'pt':0})['pt'], player_data.get(b, {'pt':0})['pt']
        role_a, _ = get_role_icon(pt_a)
        role_b, _ = get_role_icon(pt_b)
        start_a = next(start for start,end,r,icon in rank_roles if r==role_a)
        start_b = next(start for start,end,r,icon in rank_roles if r==role_b)
        if abs(start_a - start_b) >= 5:
            continue  # マッチ不可
        in_match[a] = b
        in_match[b] = a
        draw_list.discard(a)
        draw_list.discard(b)
        match_requests.pop(a, None)
        match_requests.pop(b, None)
        guild = interaction.guild
        member_a = guild.get_member(a)
        member_b = guild.get_member(b)
        if member_a and member_b:
            msg = f"{member_a.mention} vs {member_b.mention} のマッチが成立しました。試合後、勝者が【/結果報告】を行なってください"
            await interaction.channel.send(msg)

    # 余りプレイヤーは希望リストに残る
    draw_list.clear()

# --- キャンセル ---
@bot.tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げる")
async def cancel_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in in_match:
        await interaction.response.send_message("対戦中のため取り下げできません", ephemeral=True)
        return
    if user_id in match_requests:
        match_requests.pop(user_id, None)
        draw_list.discard(user_id)
        await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望がありません", ephemeral=True)

# --- 結果報告 ---
@bot.tree.command(name="結果報告", description="試合結果を報告する")
@app_commands.describe(winner="勝者", loser="敗者")
async def report_result(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    if winner.id not in in_match or in_match[winner.id] != loser.id:
        await interaction.response.send_message("この組み合わせはマッチ中ではありません", ephemeral=True)
        return
    # pt計算ライト仕様
    player_data[winner.id]['pt'] = player_data.get(winner.id, {'pt':0})['pt'] + 1
    player_data[loser.id]['pt'] = max(player_data.get(loser.id, {'pt':0})['pt'] - 1, 0)
    await update_nickname(winner, player_data[winner.id]['pt'])
    await update_nickname(loser, player_data[loser.id]['pt'])
    # リストから削除
    in_match.pop(winner.id, None)
    in_match.pop(loser.id, None)
    await interaction.response.send_message(f"結果を記録しました: {winner.display_name} +1pt, {loser.display_name} -1pt", ephemeral=True)

bot.run(TOKEN)
