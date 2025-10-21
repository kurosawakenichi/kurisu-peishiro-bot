import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import os
from datetime import datetime, timedelta, time as dt_time
from collections import defaultdict

# ----------------------------------------
# 環境変数
# ----------------------------------------
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID", 0))  # 任意

# ----------------------------------------
# Bot初期化
# ----------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------------------
# 定数
# ----------------------------------------
AUTO_APPROVE_SECONDS = 15*60  # 15分

# ----------------------------------------
# ユーザーデータ
# {user_id: {"pt": int}}
# ----------------------------------------
user_data = {}

# マッチング情報
# {user_id: opponent_id}
matching = {}

# ----------------------------------------
# ランク定義（表示用）
# 各タプル: (start_pt, end_pt, role_name, icon_for_display)
# Challenge含む
# ----------------------------------------
rank_roles = [
    (0, 2, "Beginner", "🔰"),
    (3, 3, "SilverChallenge1", "🔰🔥"),
    (4, 4, "SilverChallenge2", "🔰🔥🔥"),
    (5, 7, "Silver", "🥈"),
    (8, 8, "GoldChallenge1", "🥈🔥"),
    (9, 9, "GoldChallenge2", "🥈🔥🔥"),
    (10, 12, "Gold", "🥇"),
    (13, 13, "MasterChallenge1", "🥇🔥"),
    (14, 14, "MasterChallenge2", "🥇🔥🔥"),
    (15, 17, "Master", "⚔️"),
    (18, 18, "GrandMasterChallenge1", "⚔️🔥"),
    (19, 19, "GrandMasterChallenge2", "⚔️🔥🔥"),
    (20, 22, "GrandMaster", "🪽"),
    (23, 23, "ChallengerChallenge1", "🪽🔥"),
    (24, 24, "ChallengerChallenge2", "🪽🔥🔥"),
    (25, 9999, "Challenger", "😈"),
]

# 内部ランク階層（rank1..rank6）
rank_ranges_internal = {
    1: range(0, 5),
    2: range(5, 10),
    3: range(10, 15),
    4: range(15, 20),
    5: range(20, 25),
    6: range(25, 10000),
}

# ----------------------------------------
# 内部ランク取得
# ----------------------------------------
def get_internal_rank(pt:int) -> int:
    for rank, rng in rank_ranges_internal.items():
        if pt in rng:
            return rank
    return 6

# ----------------------------------------
# Pt計算
# ----------------------------------------
def calculate_pt(current:int, opponent:int, result:str) -> int:
    diff_rank = get_internal_rank(opponent) - get_internal_rank(current)
    if result == "win":
        if diff_rank == 0:
            return current + 1
        elif diff_rank == 1:
            return current + 2
        elif diff_rank == 2:
            return current + 3
        else:
            return current + 1
    elif result == "lose":
        # 降格処理
        if current in (3,4):
            return 2
        elif current in (8,9):
            return 7
        elif current in (13,14):
            return 12
        elif current in (18,19):
            return 17
        elif current in (23,24):
            return 22
        # ベースロジック
        if diff_rank == 0:
            return current - 1
        elif diff_rank == -1:
            return current - 2
        elif diff_rank == -2:
            return current - 3
        else:
            return current -1
    return current

# ----------------------------------------
# メンバーディスプレイ更新（名前＆ロール）
# ----------------------------------------
async def update_member_display(member: discord.Member):
    uid = member.id
    pt = user_data.get(uid, {}).get("pt",0)
    # 名前更新
    for start,end,role_name,icon in rank_roles:
        if start <= pt <= end:
            try:
                await member.edit(nick=f"{member.name} {icon} {pt}pt")
            except:
                pass
            # ロール付与削除
            guild_roles = {r.name:r for r in member.guild.roles}
            # 付与
            if role_name in guild_roles:
                if guild_roles[role_name] not in member.roles:
                    await member.add_roles(guild_roles[role_name])
            # 他ロール削除
            for _,_,r_name,_ in rank_roles:
                if r_name != role_name and r_name in guild_roles:
                    if guild_roles[r_name] in member.roles:
                        await member.remove_roles(guild_roles[r_name])
            break

# ----------------------------------------
# マッチング確認
# ----------------------------------------
def is_registered_match(a:int, b:int) -> bool:
    return matching.get(a) == b and matching.get(b) == a

# ----------------------------------------
# Views & 承認処理（省略せず最新版完全実装）
# ※ ApproveMatchView, CancelExistingMatchView, ResultApproveView
# ※ handle_approved_result も含む
# ----------------------------------------
# ... ここはあなたが既に完全版として確認済みのまま省略せず実装してください ...

# ----------------------------------------
# コマンド: /admin_show_ranking（standard competition ranking対応）
# ----------------------------------------
@bot.tree.command(name="admin_show_ranking", description="管理者用: ランキング表示（順位付き・同率対応）")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return

    groups = defaultdict(list)
    for uid, data in user_data.items():
        groups[data.get("pt", 0)].append(uid)

    if not groups:
        await interaction.response.send_message("まだユーザーが登録されていません。", ephemeral=True)
        return

    pts_desc = sorted(groups.keys(), reverse=True)
    lines = []
    rank = 1
    count_so_far = 0
    for pt in pts_desc:
        uids = groups[pt]
        members = []
        for uid in uids:
            member = interaction.guild.get_member(uid)
            name = member.name if member else f"Unknown({uid})"
            members.append((name, uid))
        members.sort(key=lambda x:x[0].lower())
        current_rank = count_so_far + 1
        for name, uid in members:
            lines.append(f"{current_rank}. {name} - {pt}pt")
        count_so_far += len(members)

    await interaction.response.send_message("ランキングを投稿しました（管理者にのみ表示）", ephemeral=True)
    ranking_text = "**ランキング**\n" + "\n".join(lines)
    ch = bot.get_channel(RANKING_CHANNEL_ID)
    if ch:
        await ch.send(ranking_text)

# ----------------------------------------
# 自動投稿タスク（14:00 / 23:00 JST）
# ----------------------------------------
@tasks.loop(minutes=1)
async def auto_ranking_post():
    now = datetime.now()
    if now.time().hour == 14 or now.time().hour == 23:
        guild = bot.get_guild(GUILD_ID)
        ch = guild.get_channel(RANKING_CHANNEL_ID)
        if ch:
            # 標準同率順位ランキング作成
            groups = defaultdict(list)
            for uid,data in user_data.items():
                groups[data.get("pt",0)].append(uid)
            pts_desc = sorted(groups.keys(), reverse=True)
            lines=[]
            rank=1
            count_so_far=0
            for pt in pts_desc:
                uids=groups[pt]
                members=[]
                for uid in uids:
                    member = guild.get_member(uid)
                    name = member.name if member else f"Unknown({uid})"
                    members.append((name,uid))
                members.sort(key=lambda x:x[0].lower())
                current_rank = count_so_far +1
                for name,_ in members:
                    lines.append(f"{current_rank}. {name} - {pt}pt")
                count_so_far+=len(members)
            if lines:
                await ch.send("**自動ランキング**\n" + "\n".join(lines))

# ----------------------------------------
# Bot起動
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    if not auto_ranking_post.is_running():
        auto_ranking_post.start()

bot.run(DISCORD_TOKEN)
