import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone
import random

# ----------------------------------------
# 環境変数
# ----------------------------------------
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
MATCHING_CHANNEL_ID = int(os.environ["MATCHING_CHANNEL_ID"])

# JSTタイムゾーン
JST = timezone(timedelta(hours=+9))
AUTO_APPROVE_SECONDS = 300  # 5分

# ----------------------------------------
# 内部データ
# ----------------------------------------
user_data = {}      # user_id -> {"pt": int}
matching = {}       # 現在マッチ中のプレイヤー組
waiting_list = {}   # user_id -> {"expires": datetime, "task": asyncio.Task, "interaction": discord.Interaction}

# ----------------------------------------
# ランク定義（表示用）6段階
# ----------------------------------------
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

rank_ranges_internal = {
    1: range(0, 5),
    2: range(5, 10),
    3: range(10, 15),
    4: range(15, 20),
    5: range(20, 25),
    6: range(25, 10000),
}

# ----------------------------------------
# ボット初期化
# ----------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ----------------------------------------
# ユーティリティ
# ----------------------------------------
def get_rank_info(pt: int):
    for start, end, role, icon in rank_roles:
        if start <= pt <= end:
            return role, icon
    return "Unknown", "❓"

def get_internal_rank(pt: int):
    for rank, rrange in rank_ranges_internal.items():
        if pt in rrange:
            return rank
    return 1

def calculate_pt(my_pt: int, opp_pt: int, result: str) -> int:
    delta = 1 if result == "win" else -1
    return max(my_pt + delta, 0)

async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    role_name, icon = get_rank_info(pt)
    try:
        await member.edit(nick=f"{member.display_name.split(' ')[0]} {icon} {pt}pt")
        guild = member.guild
        for r in rank_roles:
            role = discord.utils.get(guild.roles, name=r[2])
            if role and role in member.roles:
                await member.remove_roles(role)
        new_role = discord.utils.get(guild.roles, name=role_name)
        if new_role:
            await member.add_roles(new_role)
    except Exception as e:
        print(f"Error updating {member}: {e}")

def is_registered_match(a: int, b: int):
    return matching.get(a) == b and matching.get(b) == a

# ----------------------------------------
# マッチング処理
# ----------------------------------------
async def try_match_users():
    users = list(waiting_list.keys())
    random.shuffle(users)
    matched = set()
    for i in range(len(users)):
        if users[i] in matched:
            continue
        for j in range(i + 1, len(users)):
            if users[j] in matched:
                continue
            u1, u2 = users[i], users[j]
            pt1 = user_data.get(u1, {}).get("pt", 0)
            pt2 = user_data.get(u2, {}).get("pt", 0)
            rank1 = get_internal_rank(pt1)
            rank2 = get_internal_rank(pt2)
            if abs(rank1 - rank2) >= 3:
                continue
            # マッチ成立
            matching[u1] = u2
            matching[u2] = u1
            for uid in [u1, u2]:
                task = waiting_list[uid]["task"]
                task.cancel()
                waiting_list.pop(uid, None)
            ch = bot.get_channel(MATCHING_CHANNEL_ID)
            await ch.send(f"<@{u1}> と <@{u2}> のマッチングが成立しました。試合後、勝者が /結果報告 を行ってください。")
            matched.update([u1, u2])
            break

async def remove_waiting(user_id: int):
    if user_id in waiting_list:
        interaction = waiting_list[user_id]["interaction"]

        # 待機終了表示とリトライボタンに変更
        class RetryView(discord.ui.View):
            def __init__(self, user_id):
                super().__init__(timeout=None)
                self.user_id = user_id

            @discord.ui.button(label="リトライ", style=discord.ButtonStyle.primary)
            async def retry(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                await cmd_match_wish(button_interaction)
                self.stop()

        try:
            await interaction.edit_original_response(content=f"⏱ {interaction.user.mention}、マッチング相手が見つかりませんでした。", view=RetryView(user_id))
        except Exception:
            pass  # interaction が消えていた場合は通知せず

        waiting_list.pop(user_id, None)

async def waiting_timer(user_id: int):
    try:
        await asyncio.sleep(300)
        await remove_waiting(user_id)
    except asyncio.CancelledError:
        pass

# ----------------------------------------
# /マッチ希望コマンド
# ----------------------------------------
class CancelWaitingView(discord.ui.View):
    def __init__(self, user_id:int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user_id in waiting_list:
            waiting_list[self.user_id]["task"].cancel()
            waiting_list.pop(self.user_id, None)
            await interaction.response.send_message("待機リストから削除しました。", ephemeral=True)
        self.stop()

@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def cmd_match_wish(interaction: discord.Interaction):
    if interaction.channel.id != MATCHING_CHANNEL_ID:
        await interaction.response.send_message(f"このコマンドは <#{MATCHING_CHANNEL_ID}> でのみ使用可能です。", ephemeral=True)
        return
    uid = interaction.user.id
    if uid in matching:
        await interaction.response.send_message("すでにマッチ済みです。", ephemeral=True)
        return
    if uid in waiting_list:
        await interaction.response.send_message("すでに待機中です。", ephemeral=True)
        return
    task = asyncio.create_task(waiting_timer(uid))
    waiting_list[uid] = {"expires": datetime.now(JST)+timedelta(seconds=300), "task": task, "interaction": interaction}
    view = CancelWaitingView(uid)
    await interaction.response.send_message("マッチング中です…", ephemeral=True, view=view)
    # 待機タイマーリセット
    for uid2, info in waiting_list.items():
        info["task"].cancel()
        info["task"] = asyncio.create_task(waiting_timer(uid2))
        info["interaction"] = info.get("interaction", interaction)
    await asyncio.sleep(5)
    await try_match_users()

# ----------------------------------------
# 結果報告フロー
# ----------------------------------------
# ...（既存コードと同じ。省略可能）...

# ----------------------------------------
# ランキング表示
# ----------------------------------------
# ...（既存コードと同じ。省略可能）...

# ----------------------------------------
# 管理コマンド
# ----------------------------------------
@bot.tree.command(name="admin_reset_all", description="全ユーザーのPTを0にリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    guild = bot.get_guild(GUILD_ID)
    for member in guild.members:
        if member.bot:
            continue
        user_data.setdefault(member.id, {})["pt"] = 0
        await update_member_display(member)
    await interaction.response.send_message("全ユーザーのPTを0にリセットしました。", ephemeral=True)

# ----------------------------------------
# 起動処理
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    await bot.tree.sync()

bot.run(DISCORD_TOKEN)
