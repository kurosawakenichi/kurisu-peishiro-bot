import os
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks, commands
from datetime import datetime, timedelta, timezone
import random

# ----------------------------------------
# 環境変数
# ----------------------------------------
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])

# JSTタイムゾーン
JST = timezone(timedelta(hours=+9))

# ----------------------------------------
# 内部データ
# ----------------------------------------
user_data = {}       # user_id -> {"pt": int}
matching = {}        # user_id -> opponent_id
waiting_list = {}    # user_id -> {"task": asyncio.Task, "added_at": datetime}
waiting_lock = asyncio.Lock()  # 待機リスト操作の排他用

# ----------------------------------------
# ランク定義（6段階）
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

rank_ranges_internal = {
    1: range(0,5),
    2: range(5,10),
    3: range(10,15),
    4: range(15,20),
    5: range(20,25),
    6: range(25,10000),
}

# ----------------------------------------
# ボット初期化
# ----------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ----------------------------------------
# ランク取得
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

# ----------------------------------------
# PT計算（±1のみ）
# ----------------------------------------
def calculate_pt(my_pt:int, opp_pt:int, result:str) -> int:
    delta = 1 if result=="win" else -1
    return max(my_pt+delta, 0)

# ----------------------------------------
# メンバー表示更新
# ----------------------------------------
async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id,{}).get("pt",0)
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

# ----------------------------------------
# マッチチェック
# ----------------------------------------
def is_registered_match(a:int, b:int):
    return matching.get(a) == b and matching.get(b) == a

# ----------------------------------------
# 待機リストタイムアウト
# ----------------------------------------
async def waiting_timeout(user_id:int, interaction: discord.Interaction):
    try:
        await asyncio.sleep(5*60)
        async with waiting_lock:
            if user_id in waiting_list:
                waiting_list.pop(user_id,None)
                await interaction.followup.send("マッチング相手が見つかりませんでした。", ephemeral=True)
    except asyncio.CancelledError:
        return

# ----------------------------------------
# マッチング処理（内部関数）
# ----------------------------------------
async def try_match():
    async with waiting_lock:
        users = list(waiting_list.keys())
        random.shuffle(users)
        matched = set()
        for i in range(len(users)):
            if users[i] in matched:
                continue
            for j in range(i+1, len(users)):
                if users[j] in matched:
                    continue
                rank_i = get_internal_rank(user_data.get(users[i],{}).get("pt",0))
                rank_j = get_internal_rank(user_data.get(users[j],{}).get("pt",0))
                if abs(rank_i - rank_j) < 3:
                    u1 = users[i]
                    u2 = users[j]
                    matching[u1] = u2
                    matching[u2] = u1
                    # 待機リストから削除
                    for uid in (u1,u2):
                        task = waiting_list[uid]["task"]
                        task.cancel()
                        waiting_list.pop(uid,None)
                    # 両者通知
                    guild = bot.get_guild(GUILD_ID)
                    ch = guild.get_channel(RANKING_CHANNEL_ID)
                    if ch:
                        await ch.send(f"<@{u1}> と <@{u2}> のマッチングが成立しました。試合後、勝者が /結果報告 を行ってください。")
                    matched.update([u1,u2])
                    break

# ----------------------------------------
# /マッチ希望 コマンド
# ----------------------------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def cmd_random_match(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in matching:
        await interaction.response.send_message("すでにマッチ中です。", ephemeral=True)
        return
    if user_id in waiting_list:
        await interaction.response.send_message("すでに待機中です。", ephemeral=True)
        return

    await interaction.response.send_message("マッチング中です...", ephemeral=True)

    async def timeout_task():
        await waiting_timeout(user_id, interaction)

    async with waiting_lock:
        waiting_list[user_id] = {"added_at": datetime.now(), "task": asyncio.create_task(timeout_task())}

    # 待機5秒後に抽選
    await asyncio.sleep(5)
    await try_match()

# ----------------------------------------
# 結果報告 / 結果承認フローはライト版と同様
# ----------------------------------------
class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id:int, loser_id:int):
        super().__init__(timeout=None)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.processed = False

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        await interaction.response.edit_message(content="承認されました。結果を反映します。", view=None)
        await handle_approved_result(self.winner_id, self.loser_id, interaction.channel)

    @discord.ui.button(label="異議", style=discord.ButtonStyle.danger)
    async def dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        await interaction.response.edit_message(content="異議が申立てられました。審判チャンネルへ通知します。", view=None)
        judge_ch = interaction.guild.get_channel(RANKING_CHANNEL_ID)
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。@<@{ADMIN_ID}> に連絡してください。")
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)

async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    if not is_registered_match(winner_id, loser_id):
        await channel.send("このマッチングは登録されていません。")
        return
    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt  = user_data.get(loser_id, {}).get("pt", 0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new  = calculate_pt(loser_pt, winner_pt, "lose")
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new

    for g in bot.guilds:
        w_member = g.get_member(winner_id)
        l_member = g.get_member(loser_id)
        if w_member:
            await update_member_display(w_member)
        if l_member:
            await update_member_display(l_member)
    matching.pop(winner_id, None)
    matching.pop(loser_id, None)
    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

# ----------------------------------------
# on_ready と bot 実行
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    await bot.tree.sync()

bot.run(DISCORD_TOKEN)
