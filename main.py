import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import asyncio

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = 1427542200614387846  # #ランキング

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

tree = bot.tree

# 階級定義
RANKS = [
    {"name": "Beginner", "emoji": "🔰", "min_pt": 0},
    {"name": "Silver", "emoji": "🥈", "min_pt": 5},
    {"name": "Gold", "emoji": "🥇", "min_pt": 10},
    {"name": "Master", "emoji": "⚔️", "min_pt": 15},
    {"name": "GroundMaster", "emoji": "🪽", "min_pt": 20},
    {"name": "Challenger", "emoji": "😈", "min_pt": 25},
]

# ユーザー情報管理
players = {}  # {user_id: {"pt": int, "rank_index": int, "challenge": bool, "challenge_progress": int}}

# マッチング管理
pending_matches = {}  # {challenger_id: opponent_id}
pending_approvals = {}  # {winner_id: {"loser_id": int, "msg": discord.Message}}

# ------------------ ヘルパー関数 ------------------ #
def get_rank_index(pt):
    for i in reversed(range(len(RANKS))):
        if pt >= RANKS[i]["min_pt"]:
            return i
    return 0

def rank_display(user_id):
    info = players.get(user_id, {"pt":0, "rank_index":0, "challenge":False})
    rank = RANKS[info["rank_index"]]
    fire = "🔥" if info.get("challenge", False) else ""
    return f"{rank['emoji']} {info['pt']}pt {fire}"

async def update_member_display(user_id):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member:
        rank_text = rank_display(user_id)
        new_name = f"{member.name} {rank_text}"
        try:
            await member.edit(nick=new_name)
        except discord.Forbidden:
            pass  # 権限がない場合は無視

def calculate_pt(winner_id, loser_id):
    winner = players[winner_id]
    loser = players[loser_id]
    diff = winner["rank_index"] - loser["rank_index"]
    # 同階級
    if diff == 0:
        winner["pt"] += 1
        loser["pt"] -= 1
    else:
        # 低い側
        if diff < 0:
            winner["pt"] += 1 + abs(diff)
            loser["pt"] -= 1
        # 高い側
        else:
            winner["pt"] += 1
            loser["pt"] -= (1 + diff)
    # ランク更新
    for uid in (winner_id, loser_id):
        info = players[uid]
        old_rank = info["rank_index"]
        info["rank_index"] = get_rank_index(info["pt"])
        # 昇級チャレンジ判定
        if info["pt"] in [4,9,14,19,24]:
            info["challenge"] = True
            info["challenge_progress"] = 0
        elif info.get("challenge", False):
            info["challenge_progress"] += 1
            # 無敗で条件達成
            if info["pt"] >= RANKS[info["rank_index"]]["min_pt"] + 2 or info["rank_index"] > old_rank:
                info["challenge"] = False

# ------------------ イベント・コマンド ------------------ #
@tree.command(guild=discord.Object(id=GUILD_ID), name="イベント設定", description="イベントを設定")
async def event_setup(interaction: discord.Interaction):
    if interaction.user.id != int(os.environ.get("ADMIN_ID", 0)):
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    await interaction.response.send_message("イベントを設定しました。", ephemeral=True)

@tree.command(guild=discord.Object(id=GUILD_ID), name="マッチング申請", description="マッチング申請")
@app_commands.describe(opponent="対戦相手")
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    challenger_id = interaction.user.id
    opponent_id = opponent.id
    if abs(players.get(challenger_id, {"rank_index":0})["rank_index"] - players.get(opponent_id, {"rank_index":0})["rank_index"]) >= 3:
        await interaction.response.send_message("3階級以上離れた相手とはマッチできません", ephemeral=True)
        return
    pending_matches[challenger_id] = opponent_id
    # 承認ボタン
    class ApproveButton(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="承認", style=discord.ButtonStyle.green)
        async def approve(self, interaction2: discord.Interaction, button: discord.ui.Button):
            if interaction2.user.id != opponent_id:
                await interaction2.response.send_message("あなたは承認できません", ephemeral=True)
                return
            pending_approvals[challenger_id] = {"loser_id": opponent_id, "msg": interaction2.message}
            await interaction2.response.send_message(f"{opponent.display_name}が承認しました。/試合結果報告で勝者を報告してください。", ephemeral=True)

    await interaction.response.send_message(f"{opponent.display_name}にマッチング申請しました。承認を待ってください。", view=ApproveButton())

@tree.command(guild=discord.Object(id=GUILD_ID), name="試合結果報告", description="試合結果報告")
@app_commands.describe(winner="勝者")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = pending_approvals.get(winner.id, {}).get("loser_id")
    if not loser_id:
        await interaction.response.send_message("承認されていません", ephemeral=True)
        return
    calculate_pt(winner.id, loser_id)
    await update_member_display(winner.id)
    await update_member_display(loser_id)
    pending_approvals.pop(winner.id)
    await interaction.response.send_message(f"{winner.display_name}の勝利が記録されました。")

@tree.command(guild=discord.Object(id=GUILD_ID), name="pt操作", description="管理者用pt操作")
@app_commands.describe(target="対象ユーザー", pt="変更pt")
async def pt_modify(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != int(os.environ.get("ADMIN_ID", 0)):
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    uid = target.id
    if uid not in players:
        players[uid] = {"pt":0, "rank_index":0, "challenge":False, "challenge_progress":0}
    players[uid]["pt"] = pt
    players[uid]["rank_index"] = get_rank_index(pt)
    await update_member_display(uid)
    await interaction.response.send_message(f"{target.display_name}のptを{pt}に設定しました。", ephemeral=True)

# ------------------ ランキング自動投稿 ------------------ #
@tasks.loop(minutes=1)
async def ranking_task():
    now = datetime.now()
    if now.hour in [13, 22] and now.minute == 0:
        channel = bot.get_channel(RANKING_CHANNEL_ID)
        if channel:
            ranking_list = sorted(players.items(), key=lambda x: -x[1]["pt"])
            msg = "**ランキング**\n"
            for uid, info in ranking_list:
                member = bot.get_guild(GUILD_ID).get_member(uid)
                if member:
                    msg += f"{member.display_name}: {rank_display(uid)}\n"
            await channel.send(msg)

# ------------------ 起動処理 ------------------ #
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    ranking_task.start()

# ------------------ 実行 ------------------ #
bot.run(TOKEN)
