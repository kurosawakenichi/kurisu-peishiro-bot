import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta
import random

# -----------------------
# 環境変数
# -----------------------
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])

# -----------------------
# Bot 設定
# -----------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

# -----------------------
# 内部データ
# -----------------------
user_data = {}  # {user_id: {"pt": int}}
matching = {}   # 現在マッチ中 {user_id: opponent_id}
waiting_list = {}  # マッチ希望 {user_id: expire_datetime}
抽選リスト = []  # 現在抽選中
in_match = {}   # {user_id: opponent_id}

# -----------------------
# ランク表示（ライト版）
# -----------------------
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

def get_rank(pt):
    for start, end, name, icon in rank_roles:
        if start <= pt <= end:
            return name, icon
    return "Unknown", "❓"

def get_internal_rank(pt):
    # ランク1..6対応
    if 0 <= pt <= 4: return 1
    elif 5 <= pt <= 9: return 2
    elif 10 <= pt <= 14: return 3
    elif 15 <= pt <= 19: return 4
    elif 20 <= pt <= 24: return 5
    else: return 6

async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    rank, icon = get_rank(pt)
    try:
        await member.edit(nick=f"{member.display_name.split()[0]} {icon} {pt}pt")
    except Exception:
        pass
    # ロール管理
    guild = member.guild
    for _, _, role_name, _ in rank_roles:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            if role_name == rank:
                if role not in member.roles:
                    await member.add_roles(role)
            else:
                if role in member.roles:
                    await member.remove_roles(role)

# -----------------------
# ユーティリティ
# -----------------------
def is_registered_match(a, b):
    return matching.get(a) == b and matching.get(b) == a

def calculate_pt(current_pt, opponent_pt, result):
    # ライト版: ±1
    if result == "win":
        return current_pt + 1
    else:
        return max(0, current_pt - 1)

# -----------------------
# マッチ希望 / ランダム抽選
# -----------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチを希望します")
async def cmd_match_request(interaction: discord.Interaction):
    user = interaction.user
    now = datetime.utcnow()
    expire_time = now + timedelta(minutes=5)
    
    # 重複防止
    if user.id in waiting_list or user.id in in_match:
        await interaction.response.send_message("既にマッチ希望中、または対戦中です", ephemeral=True)
        return
    
    waiting_list[user.id] = expire_time
    await interaction.response.send_message("マッチ希望を受け付けました。マッチング中です...", ephemeral=True)

    # 抽選処理
    async def lottery():
        nonlocal user
        抽選リスト.append(user.id)
        wait_seconds = 5
        while wait_seconds > 0:
            await asyncio.sleep(1)
            wait_seconds -= 1
        # 抽選完了
        players = list(抽選リスト)
        random.shuffle(players)
        抽選リスト.clear()
        # ペア作成
        paired = set()
        for i in range(0, len(players)-1, 2):
            a = players[i]
            b = players[i+1]
            # 階級差制限あり
            if abs(get_internal_rank(user_data.get(a, {}).get("pt",0)) - get_internal_rank(user_data.get(b, {}).get("pt",0))) >= 3:
                continue
            matching[a] = b
            matching[b] = a
            in_match[a] = b
            in_match[b] = a
            # 希望リストから削除
            waiting_list.pop(a, None)
            waiting_list.pop(b, None)
            # 成立通知
            channel = interaction.channel
            await channel.send(f"<@{a}> vs <@{b}> のマッチが成立しました。試合後、勝者が /結果報告 を行ってください。")

        # 余りは残す（希望リストに残す）
        for p in players:
            if p not in in_match:
                # 希望リストに残る
                pass
    bot.loop.create_task(lottery())

# -----------------------
# マッチ希望取下げ
# -----------------------
@bot.tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます")
async def cmd_cancel_request(interaction: discord.Interaction):
    user = interaction.user
    if user.id in waiting_list:
        waiting_list.pop(user.id, None)
        await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望中ではありません", ephemeral=True)

# -----------------------
# 結果報告
# -----------------------
@bot.tree.command(name="結果報告", description="試合結果を報告します")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent

    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチは登録されていません", ephemeral=True)
        return

    winner_pt = user_data.get(winner.id, {}).get("pt", 0)
    loser_pt = user_data.get(loser.id, {}).get("pt", 0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new = calculate_pt(loser_pt, winner_pt, "lose")

    user_data.setdefault(winner.id, {})["pt"] = winner_new
    user_data.setdefault(loser.id, {})["pt"] = loser_new

    for g in bot.guilds:
        w_member = g.get_member(winner.id)
        l_member = g.get_member(loser.id)
        if w_member:
            await update_member_display(w_member)
        if l_member:
            await update_member_display(l_member)

    matching.pop(winner.id, None)
    matching.pop(loser.id, None)
    in_match.pop(winner.id, None)
    in_match.pop(loser.id, None)

    await interaction.response.send_message(f"✅ <@{winner.id}> +1pt / <@{loser.id}> -1pt が反映されました", ephemeral=False)

# -----------------------
# 管理者コマンド
# -----------------------
@bot.tree.command(name="admin_set_pt", description="管理者用: 任意のユーザーのptを設定")
@app_commands.describe(target="対象メンバー", pt="設定するpt")
async def admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    user_data.setdefault(target.id, {})["pt"] = pt
    await update_member_display(target)
    await interaction.response.send_message(f"{target.display_name} のptを {pt} に設定しました", ephemeral=True)

@bot.tree.command(name="admin_reset_all", description="管理者用: 全ユーザーptリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    for uid in user_data:
        user_data[uid]["pt"] = 0
        member = interaction.guild.get_member(uid)
        if member:
            await update_member_display(member)
    await interaction.response.send_message("全ユーザーのptをリセットしました", ephemeral=True)

# -----------------------
# ランキング表示
# -----------------------
@bot.tree.command(name="ランキング", description="現在のランキングを表示します")
async def cmd_ranking(interaction: discord.Interaction):
    guild = interaction.guild
    # standard competition ranking
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    ranks = []
    last_pt = None
    rank_no = 0
    skip = 1
    for uid, data in sorted_users:
        pt = data.get("pt",0)
        if pt != last_pt:
            rank_no += skip
            skip = 1
        else:
            skip += 1
        last_pt = pt
        member = guild.get_member(uid)
        rank_name, icon = get_rank(pt)
        display_name = member.display_name if member else f"<@{uid}>"
        ranks.append(f"{rank_no}位 {display_name} {icon} {pt}pt")
    await interaction.response.send_message("🏆 ランキング\n" + "\n".join(ranks), ephemeral=False)

# -----------------------
# 自動ランキング投稿タスク
# -----------------------
@tasks.loop(time=[datetime.strptime("14:00","%H:%M").time(), datetime.strptime("23:00","%H:%M").time()])
async def auto_post_ranking():
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(RANKING_CHANNEL_ID)
    if not channel:
        return
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    ranks = []
    last_pt = None
    rank_no = 0
    skip = 1
    for uid, data in sorted_users:
        pt = data.get("pt",0)
        if pt != last_pt:
            rank_no += skip
            skip = 1
        else:
            skip += 1
        last_pt = pt
        member = guild.get_member(uid)
        rank_name, icon = get_rank(pt)
        display_name = member.display_name if member else f"<@{uid}>"
        ranks.append(f"{rank_no}位 {display_name} {icon} {pt}pt")
    await channel.send("🏆 ランキング\n" + "\n".join(ranks))

@bot.event
async def on_ready():
    print(f"{bot.user} is ready")
    auto_post_ranking.start()

bot.run(DISCORD_TOKEN)
