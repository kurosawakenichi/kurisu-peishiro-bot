import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
import random

# -------------------------
# 環境変数読み込み
# -------------------------
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID"))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID"))

# -------------------------
# Bot初期化
# -------------------------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

# -------------------------
# ユーザーデータ & マッチ情報
# -------------------------
user_data = {}  # {user_id: {"pt": int}}
matching = {}   # {user_id: opponent_id}

# ランク設定（ライト用、challenge無し）
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

# 内部ランク階層（match制限用）
rank_ranges_internal = {
    1: range(0, 5),
    2: range(5, 10),
    3: range(10, 15),
    4: range(15, 20),
    5: range(20, 25),
    6: range(25, 10000),
}

# -------------------------
# マッチ希望・抽選管理
# -------------------------
match_request_list = {}  # {user_id: timestamp}
lottery_list = set()      # ランダム抽選対象
lottery_task = None       # 抽選待機タスク
LOTTERY_WAIT = 3          # 秒
REQUEST_TIMEOUT = 300     # 秒

# -------------------------
# ユーティリティ関数
# -------------------------
def get_rank_icon_and_name(pt):
    for start, end, role_name, icon in rank_roles:
        if start <= pt <= end:
            return role_name, icon
    return "Unknown", "❓"

def get_internal_rank(pt):
    for r, rng in rank_ranges_internal.items():
        if pt in rng:
            return r
    return 1

def is_registered_match(a_id, b_id):
    return matching.get(a_id) == b_id

async def update_member_display(member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    role_name, icon = get_rank_icon_and_name(pt)
    new_name = f"{member.display_name.split()[0]} {icon} {pt}pt"
    try:
        await member.edit(nick=new_name)
    except Exception:
        pass

# -------------------------
# マッチング抽選処理
# -------------------------
async def run_lottery(channel):
    global lottery_list
    participants = list(lottery_list)
    lottery_list = set()  # 一旦リセット
    random.shuffle(participants)

    i = 0
    while i + 1 < len(participants):
        a = participants[i]
        b = participants[i+1]
        # internal rank差3以上は不可
        a_rank = get_internal_rank(user_data.get(a, {}).get("pt", 0))
        b_rank = get_internal_rank(user_data.get(b, {}).get("pt", 0))
        if abs(a_rank - b_rank) < 3:
            matching[a] = b
            matching[b] = a
            ch = channel
            await ch.send(f"<@{a}> vs <@{b}> のマッチが成立しました。試合後、勝者が /結果報告 を行なってください")
            i += 2
        else:
            i += 1

# -------------------------
# Views
# -------------------------
class CancelMatchView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これはあなたのマッチ申請ではありません。", ephemeral=True)
            return
        match_request_list.pop(self.user_id, None)
        lottery_list.discard(self.user_id)
        await interaction.response.send_message("マッチ申請をキャンセルしました。", ephemeral=True)
        self.stop()

# -------------------------
# コマンド: マッチ希望
# -------------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望を登録します")
async def cmd_match_request(interaction: discord.Interaction):
    user = interaction.user
    now = asyncio.get_event_loop().time()
    match_request_list[user.id] = now
    lottery_list.add(user.id)
    await interaction.response.send_message("マッチング中です...", ephemeral=True, view=CancelMatchView(user.id))

    await asyncio.sleep(LOTTERY_WAIT)
    await run_lottery(interaction.channel)

# -------------------------
# 結果報告コマンド
# -------------------------
@bot.tree.command(name="結果報告", description="対戦結果を報告します")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent

    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。", ephemeral=True)
        return

    winner_pt = user_data.get(winner.id, {}).get("pt", 0) + 1
    loser_pt = max(0, user_data.get(loser.id, {}).get("pt", 0) - 1)
    user_data.setdefault(winner.id, {})["pt"] = winner_pt
    user_data.setdefault(loser.id, {})["pt"] = loser_pt

    # 更新
    await update_member_display(winner)
    await update_member_display(loser)

    matching.pop(winner.id, None)
    matching.pop(loser.id, None)

    await interaction.response.send_message(f"結果反映: <@{winner.id}> +1pt / <@{loser.id}> -1pt")

# -------------------------
# ランキング表示コマンド
# -------------------------
@bot.tree.command(name="ランキング", description="ランキングを表示します")
async def cmd_ranking(interaction: discord.Interaction):
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    ranking_text = "🏆 ランキング\n"
    last_pt = None
    rank = 0
    display_rank = 0
    for user_id, data in sorted_users:
        display_rank += 1
        pt = data.get("pt",0)
        if pt != last_pt:
            rank = display_rank
        last_pt = pt
        _, icon = get_rank_icon_and_name(pt)
        ranking_text += f"{rank}位 <@{user_id}> {icon} {pt}pt\n"
    await interaction.response.send_message(ranking_text)

# -------------------------
# Bot起動
# -------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready")
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))

bot.run(DISCORD_TOKEN)
