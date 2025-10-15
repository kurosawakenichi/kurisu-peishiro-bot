import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, time, timedelta
import asyncio

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = 1427542200614387846

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # 推奨はPrivileged IntentをON

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# プレイヤーデータ
players = {}

# 階級設定
rankings = [
    {"name": "Beginner", "emoji": "🔰", "min_pt": 0, "max_pt": 4},
    {"name": "Silver", "emoji": "🥈", "min_pt": 5, "max_pt": 9},
    {"name": "Gold", "emoji": "🥇", "min_pt": 10, "max_pt": 14},
    {"name": "Master", "emoji": "⚔️", "min_pt": 15, "max_pt": 19},
    {"name": "GroundMaster", "emoji": "🪽", "min_pt": 20, "max_pt": 24},
    {"name": "Challenger", "emoji": "😈", "min_pt": 25, "max_pt": 999},
]

event_active = False
event_start = None
event_end = None

# ============ ユーティリティ関数 ============

def get_rank(pt):
    for r in rankings:
        if r["min_pt"] <= pt <= r["max_pt"]:
            return r
    return rankings[0]

async def update_member_display(user_id):
    member = bot.get_guild(GUILD_ID).get_member(user_id)
    if member:
        pt = players[user_id]["pt"]
        rank = get_rank(pt)
        challenge = "🔥" if players[user_id].get("challenge", False) else ""
        new_name = f"{member.name} {rank['emoji']}{pt}{challenge}"
        try:
            await member.edit(nick=new_name)
        except discord.Forbidden:
            pass

# ============ イベント設定（管理者のみ） ============

@tree.command(name="イベント設定", description="イベントの開始・終了日時を設定", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def set_event(interaction: discord.Interaction, start: str, end: str):
    global event_active, event_start, event_end
    # ISO形式で受け取り
    event_start = datetime.fromisoformat(start)
    event_end = datetime.fromisoformat(end)
    event_active = True
    await interaction.response.send_message(f"イベント設定完了: {event_start} 〜 {event_end}")

# ============ マッチング申請 ============

match_requests = {}  # {challenger_id: opponent_id}

@tree.command(name="マッチング申請", description="対戦申請を送る", guild=discord.Object(id=GUILD_ID))
async def match_request(interaction: discord.Interaction, opponent: discord.Member):
    if not event_active:
        await interaction.response.send_message("イベントは未開始です")
        return
    uid = interaction.user.id
    if uid in match_requests:
        await interaction.response.send_message("既に申請中です")
        return
    match_requests[uid] = opponent.id
    # ボタン作成
    class ApproveButton(discord.ui.View):
        @discord.ui.button(label="承認", style=discord.ButtonStyle.green)
        async def approve(self, button: discord.ui.Button, button_interaction: discord.Interaction):
            if button_interaction.user.id != opponent.id:
                await button_interaction.response.send_message("あなたは承認できません", ephemeral=True)
                return
            await interaction.user.send(f"{opponent.name}が承認しました")
            await button_interaction.response.send_message("承認完了", ephemeral=True)
    view = ApproveButton()
    await interaction.response.send_message(f"{opponent.name}にマッチング申請しました。承認を待ってください。", view=view, ephemeral=True)

# ============ 試合結果報告 ============

@tree.command(name="試合結果報告", description="勝敗報告", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    uid_w = winner.id
    uid_l = loser.id
    if match_requests.get(uid_w) != uid_l:
        await interaction.response.send_message("マッチング申請・承認済みではありません", ephemeral=True)
        return
    # pt計算
    for uid in [uid_w, uid_l]:
        if uid not in players:
            players[uid] = {"pt":0}
    players[uid_w]["pt"] += 1
    players[uid_l]["pt"] = max(players[uid_l]["pt"] - 1, 0)
    # 昇格チャレンジ判定
    for uid in [uid_w, uid_l]:
        pt = players[uid]["pt"]
        if pt in [4,9,14,19,24]:
            players[uid]["challenge"] = True
        else:
            players[uid]["challenge"] = False
    # ニックネーム更新
    await update_member_display(uid_w)
    await update_member_display(uid_l)
    await interaction.response.send_message(f"{winner.name} が勝利しました。pt反映済み。")

# ============ ランキング表示 ============

async def post_ranking():
    channel = bot.get_channel(RANKING_CHANNEL_ID)
    lines = []
    for uid, data in sorted(players.items(), key=lambda x: -x[1]["pt"]):
        member = bot.get_guild(GUILD_ID).get_member(uid)
        if member:
            rank = get_rank(data["pt"])
            challenge = "🔥" if data.get("challenge", False) else ""
            lines.append(f"{member.name} {rank['emoji']}{data['pt']}{challenge}")
    text = "\n".join(lines)
    await channel.send(f"ランキング\n{text}")

@tasks.loop(time=[time(13,0), time(22,0)])
async def auto_post_ranking():
    await post_ranking()

# ============ 管理者コマンド ============

@tree.command(name="pt操作", description="管理者がptを操作", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def admin_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    players[member.id] = {"pt":pt}
    await update_member_display(member.id)
    await interaction.response.send_message(f"{member.name}のptを{pt}に設定しました")

@tree.command(name="ランキングリセット", description="ランキングを初期化", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def reset_ranking(interaction: discord.Interaction):
    global players
    players = {}
    await interaction.response.send_message("ランキングをリセットしました")

# ============ 起動処理 ============

@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("ギルドコマンド同期完了")
    except Exception as e:
        print("コマンド同期エラー:", e)
    auto_post_ranking.start()

bot.run(TOKEN)
