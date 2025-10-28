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

JST = timezone(timedelta(hours=+9))

# ----------------------------------------
# 内部データ
# ----------------------------------------
user_data = {}        # user_id -> {"pt": int}
matching = {}         # user_id -> opponent_id
waiting_list = {}     # user_id -> {"added_at": datetime, "task": asyncio.Task}
waiting_lock = asyncio.Lock()

# ----------------------------------------
# ランク定義（6段階）
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
# ランク取得・内部ランク
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
# マッチング処理
# ----------------------------------------
async def try_match(interaction: discord.Interaction):
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
                    u1, u2 = users[i], users[j]
                    matching[u1] = u2
                    matching[u2] = u1
                    # 待機リスト削除
                    for uid in (u1,u2):
                        task = waiting_list[uid]["task"]
                        task.cancel()
                        waiting_list.pop(uid,None)
                    # 両者通知：/マッチ希望 実行チャンネル
                    ch = interaction.channel
                    await ch.send(f"<@{u1}> と <@{u2}> のマッチングが成立しました。試合後、勝者が /結果報告 を行ってください。")
                    matched.update([u1,u2])
                    break

# ----------------------------------------
# 待機タスク
# ----------------------------------------
async def waiting_loop(user_id:int, interaction: discord.Interaction):
    try:
        await asyncio.sleep(5*60)  # 5分タイムアウト
        async with waiting_lock:
            if user_id in waiting_list:
                waiting_list.pop(user_id,None)
                await interaction.followup.send("マッチング相手が見つかりませんでした。", ephemeral=True)
    except asyncio.CancelledError:
        return

# ----------------------------------------
# /マッチ希望 コマンド
# ----------------------------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def cmd_random_match(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in matching or user_id in waiting_list:
        await interaction.response.send_message("すでにマッチ中または待機中です。", ephemeral=True)
        return

    await interaction.response.send_message("マッチング中です...", ephemeral=True)

    async with waiting_lock:
        task = asyncio.create_task(waiting_loop(user_id, interaction))
        waiting_list[user_id] = {"added_at": datetime.now(), "task": task}

    await asyncio.sleep(5)  # 5秒待機
    await try_match(interaction)

# ----------------------------------------
# 結果報告フロー
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
        # 待機リスト削除
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
# ランキング表示
# ----------------------------------------
def standard_competition_ranking():
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    result = []
    prev_pt = None
    rank = 0
    display_rank = 0
    for uid, data in sorted_users:
        pt = data.get("pt",0)
        display_rank += 1
        if pt != prev_pt:
            rank = display_rank
            prev_pt = pt
        result.append((rank, uid, pt))
    return result

@bot.tree.command(name="ランキング", description="PT順にランキング表示")
async def cmd_ranking(interaction: discord.Interaction):
    rankings = standard_competition_ranking()
    lines = []
    for rank, uid, pt in rankings:
        role, icon = get_rank_info(pt)
        member = interaction.guild.get_member(uid)
        if member:
            words = member.display_name.split()
            base_name = " ".join(words[:-2]) if len(words) > 2 else member.display_name
            lines.append(f"{rank}位 {base_name} {icon} {pt}pt")
    await interaction.response.send_message("🏆 ランキング\n" + "\n".join(lines))

# ----------------------------------------
# 管理コマンド
# ----------------------------------------
@bot.tree.command(name="admin_set_pt", description="指定ユーザーのPTを設定")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    user_data.setdefault(user.id, {})["pt"] = pt
    await update_member_display(user)
    await interaction.response.send_message(f"{user.display_name} のPTを {pt} に設定しました。", ephemeral=True)

@bot.tree.command(name="admin_reset_all", description="全ユーザーのPTを0にリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    for uid in user_data.keys():
        user_data[uid]["pt"] = 0
        member = interaction.guild.get_member(uid)
        if member:
            await update_member_display(member)
    await interaction.response.send_message("全ユーザーのPTを0にリセットしました。", ephemeral=True)

# ----------------------------------------
# スラッシュコマンド同期タスク
# ----------------------------------------
async def sync_commands():
    guild = discord.Object(id=GUILD_ID)
    await bot.wait_until_ready()
    await bot.tree.sync(guild=guild)
    print("ギルド限定スラッシュコマンドを同期しました。")

bot.loop.create_task(sync_commands())

# ----------------------------------------
# bot 起動
# ----------------------------------------
bot.run(DISCORD_TOKEN)
