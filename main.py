# main.py

import os
import json
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta

# ------------------------------
# 環境変数と基本設定
# ------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))  # 管理者のDiscordユーザーID（整数）
GUILD_ID = int(os.getenv("GUILD_ID"))  # サーバーID（整数）

DATA_FILE = "user_data.json"
RANKING_CHANNEL_ID = int(os.getenv("RANKING_CHANNEL_ID"))  # ランキング自動投稿チャンネル

AUTO_APPROVE_MINUTES = 15  # 自動承認時間（指定通り15分）

# ------------------------------
# ランク設定
# ------------------------------
RANKS = [
    {"name": "Beginner", "icon": "🔰"},
    {"name": "Bronze", "icon": "🥉"},
    {"name": "Silver", "icon": "🥈"},
    {"name": "Gold", "icon": "🥇"},
    {"name": "Platinum", "icon": "💎"},
    {"name": "Master", "icon": "🔥"}
]

# ------------------------------
# データ管理
# ------------------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ------------------------------
# 階級判定
# ------------------------------
def get_rank(pt):
    if pt < 5:
        return 0
    elif pt < 10:
        return 1
    elif pt < 15:
        return 2
    elif pt < 20:
        return 3
    elif pt < 25:
        return 4
    else:
        return 5

# ------------------------------
# Discordクライアント設定
# ------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------------------
# ポイント・ロール更新
# ------------------------------
async def update_rank_and_role(member, data):
    pt = data[str(member.id)]["pt"]
    rank_index = get_rank(pt)
    rank_info = RANKS[rank_index]
    role_name = rank_info["name"]

    guild = member.guild
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        print(f"ロール {role_name} が見つかりません。")
        return

    # 旧ランクロール削除
    for r in guild.roles:
        if r.name in [r["name"] for r in RANKS] and r in member.roles:
            await member.remove_roles(r)

    await member.add_roles(role)
    icon = rank_info["icon"]

    # 昇級チャレンジ中なら🔥を追加
    if data[str(member.id)].get("challenge", False):
        icon += "🔥"

    # ニックネーム更新
    new_name = f"{member.name} {icon} {pt}pt"
    try:
        await member.edit(nick=new_name)
    except discord.Forbidden:
        pass  # 権限不足の場合はスキップ

# ------------------------------
# PvPロジック
# ------------------------------
def calc_point_change(p1_pt, p2_pt):
    """階級差を考慮した増減量を返す"""
    r1 = get_rank(p1_pt)
    r2 = get_rank(p2_pt)
    diff = abs(r1 - r2)

    if diff >= 3:
        return None  # 3階級以上は対戦不可

    # 同階級同士
    if diff == 0:
        return (+1, -1)

    # 階級差あり
    if r1 < r2:  # p1が低階級
        return (1 + diff, -1)
    else:  # p1が高階級
        return (+1, -1 - diff)

# ------------------------------
# 昇級チャレンジ処理
# ------------------------------
def check_promotion(member_id, data):
    user = data[str(member_id)]
    pt = user["pt"]
    rank_index = get_rank(pt)

    # 昇級チャレンジ突入判定
    if pt in [4, 9, 14, 19, 24] and not user.get("challenge", False):
        user["challenge"] = True
        user["challenge_progress"] = 0

    # チャレンジ中処理
    if user.get("challenge", False):
        if user["challenge_progress"] >= 2:
            user["challenge"] = False
            user["challenge_progress"] = 0
        elif pt < [4, 9, 14, 19, 24][rank_index] - 1:
            user["challenge"] = False
            user["pt"] = [4, 9, 14, 19, 24][rank_index] - 1

# ------------------------------
# マッチングシステム
# ------------------------------
pending_matches = {}

class ApproveView(discord.ui.View):
    def __init__(self, requester, opponent):
        super().__init__(timeout=None)
        self.requester = requester
        self.opponent = opponent

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("あなたはこの申請の対象ではありません。", ephemeral=True)
            return

        await interaction.response.send_message(f"{self.opponent.mention} が承認しました！対戦を開始します。")
        data = load_data()
        for user in [self.requester, self.opponent]:
            if str(user.id) not in data:
                data[str(user.id)] = {"pt": 0, "challenge": False, "challenge_progress": 0}
        save_data(data)

        pending_matches.pop(self.opponent.id, None)

# ------------------------------
# コマンド登録
# ------------------------------
@bot.event
async def on_ready():
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"{bot.user} is ready.")
    auto_ranking.start()

# 管理者専用：ポイント操作
@bot.tree.command(name="pt操作", description="指定したユーザーのポイントを変更します", guild=discord.Object(id=GUILD_ID))
async def pt操作(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return

    data = load_data()
    if str(user.id) not in data:
        data[str(user.id)] = {"pt": 0, "challenge": False, "challenge_progress": 0}
    data[str(user.id)]["pt"] = pt
    check_promotion(user.id, data)
    save_data(data)
    await update_rank_and_role(user, data)
    await interaction.response.send_message(f"{user.display_name} のポイントを {pt}pt に設定しました。")

# 管理者専用：マッチ指定
@bot.tree.command(name="強制マッチ", description="指定した2人を強制的にマッチングさせます", guild=discord.Object(id=GUILD_ID))
async def 強制マッチ(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return

    await interaction.response.send_message(f"{user1.display_name} と {user2.display_name} のマッチを設定しました。")

# 一般ユーザー：マッチ申請
@bot.tree.command(name="マッチ申請", description="相手に対戦を申し込む", guild=discord.Object(id=GUILD_ID))
async def マッチ申請(interaction: discord.Interaction, opponent: discord.Member):
    requester = interaction.user
    if requester.id == opponent.id:
        await interaction.response.send_message("自分自身には申請できません。", ephemeral=True)
        return

    pending_matches[opponent.id] = requester.id
    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)
    await opponent.send(f"{requester.display_name} からマッチング申請が届きました！", view=ApproveView(requester, opponent))

# ------------------------------
# ランキング自動投稿
# ------------------------------
@tasks.loop(minutes=30)
async def auto_ranking():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(RANKING_CHANNEL_ID)
    if not channel:
        return

    data = load_data()
    ranking = sorted(data.items(), key=lambda x: x[1]["pt"], reverse=True)
    lines = ["🏆 **ランキング** 🏆\n"]

    for i, (uid, info) in enumerate(ranking[:10], start=1):
        member = guild.get_member(int(uid))
        if member:
            rank = get_rank(info["pt"])
            icon = RANKS[rank]["icon"]
            if info.get("challenge", False):
                icon += "🔥"
            lines.append(f"{i}. {member.display_name} {icon} {info['pt']}pt")

    await channel.send("\n".join(lines))

# ------------------------------
# 実行
# ------------------------------
bot.run(TOKEN)
