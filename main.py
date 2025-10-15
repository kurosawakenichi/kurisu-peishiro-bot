import discord
from discord.ext import tasks
from discord import app_commands
import os
import json
import asyncio
from datetime import datetime

intents = discord.Intents.default()
intents.members = True

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

PLAYERS_FILE = "players.json"
EVENT_FILE = "event.json"
MATCH_CHANNELS = ["beginner", "silver", "gold", "master", "groundmaster", "challenger", "free"]

# 階級の範囲とアイコン
RANKS = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, float('inf'), "Challenger", "😈")
]

# in-memory active matches: (winner_id, loser_id)
active_matches = {}

# JSON データ読み込み
def load_json(filename):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

players = load_json(PLAYERS_FILE)
event_info = load_json(EVENT_FILE)

# 階級アイコン取得
def get_rank_icon(pt, challenge=False):
    for low, high, _, icon in RANKS:
        if low <= pt <= high:
            return icon + ("🔥" if challenge else "")
    return "❓"

def get_rank_name(pt):
    for low, high, name, _ in RANKS:
        if low <= pt <= high:
            return name
    return "Unknown"

# ユーザー表示更新
async def update_member_display(uid):
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(int(uid))
    if member:
        user_data = players.get(uid, {})
        pt = user_data.get("pt", 0)
        challenge = user_data.get("challenge", False)
        rank_icon = get_rank_icon(pt, challenge)
        rank_name = get_rank_name(pt)
        try:
            await member.edit(nick=f"{member.name} {rank_icon} {pt}pt")
        except Exception:
            pass

# イベント期間判定
def event_active():
    if not event_info:
        return False
    now = datetime.utcnow()
    start = datetime.fromisoformat(event_info["start"])
    end = datetime.fromisoformat(event_info["end"])
    return start <= now <= end

# マッチング申請
@tree.command(name="マッチング申請")
async def matching_request(interaction: discord.Interaction, opponent: discord.User):
    await interaction.response.defer(ephemeral=True)
    if not event_active():
        await interaction.followup.send("⚠️ イベント期間外です。", ephemeral=True)
        return

    winner_id = str(interaction.user.id)
    loser_id = str(opponent.id)
    key = (winner_id, loser_id)
    if key in active_matches:
        await interaction.followup.send("⚠️ このマッチングは既に申請済です。", ephemeral=True)
        return

    active_matches[key] = {"approved": False}
    await interaction.followup.send(
        f"@{opponent.name} へマッチング申請を送信しました。承認後に試合を行ってください。", ephemeral=True
    )

# 承認
@tree.command(name="承認")
async def approve_match(interaction: discord.Interaction, opponent: discord.User):
    await interaction.response.defer(ephemeral=True)
    loser_id = str(interaction.user.id)
    winner_id = str(opponent.id)
    key = (winner_id, loser_id)
    if key not in active_matches:
        await interaction.followup.send("⚠️ 該当マッチング申請が存在しません。", ephemeral=True)
        return
    active_matches[key]["approved"] = True
    await interaction.followup.send("✅ マッチング申請を承認しました。", ephemeral=True)

# 試合結果報告（勝者）
@tree.command(name="試合結果報告")
async def report(interaction: discord.Interaction, opponent: discord.User):
    await interaction.response.defer(ephemeral=True)
    winner_id = str(interaction.user.id)
    loser_id = str(opponent.id)
    key = (winner_id, loser_id)
    if key not in active_matches or not active_matches[key].get("approved", False):
        await interaction.followup.send(
            "⚠️ 事前にマッチング申請・承認が完了していません。\n"
            "問題がある場合は <@kurosawa0118> までご報告ください", ephemeral=True
        )
        return

    # --- Pt 計算 ---
    winner_data = players.get(winner_id, {"pt": 0, "challenge": False})
    loser_data = players.get(loser_id, {"pt": 0, "challenge": False})

    winner_pt = winner_data.get("pt",0)
    loser_pt = loser_data.get("pt",0)

    # 階級差
    def calc_rank_diff(pt1, pt2):
        rank1 = next(i for i, (low, high, _, _) in enumerate(RANKS) if low <= pt1 <= high)
        rank2 = next(i for i, (low, high, _, _) in enumerate(RANKS) if low <= pt2 <= high)
        return abs(rank1 - rank2)

    diff = calc_rank_diff(winner_pt, loser_pt)
    # 同階級 or 階級差あり
    if diff == 0:
        winner_pt +=1
        loser_pt = max(loser_pt-1, 0)
    else:
        winner_pt += diff
        loser_pt = max(loser_pt-1,0)

    # 昇格チャレンジチェック
    challenge_thresholds = [4,9,14,19,24]
    winner_challenge = winner_pt in challenge_thresholds
    loser_challenge = loser_pt in challenge_thresholds

    players[winner_id] = {"pt": winner_pt, "challenge": winner_challenge}
    players[loser_id] = {"pt": loser_pt, "challenge": loser_challenge}
    save_json(PLAYERS_FILE, players)

    # 非同期でメンバー表示更新
    asyncio.create_task(update_member_display(winner_id))
    asyncio.create_task(update_member_display(loser_id))

    del active_matches[key]

    # ランキングチャンネルに昇級アナウンス
    guild = bot.get_guild(GUILD_ID)
    rank_icon = get_rank_icon(winner_pt, winner_challenge)
    channel = discord.utils.get(guild.text_channels, name="ランキング")
    if channel:
        asyncio.create_task(channel.send(f"🔥 <@{winner_id}> が {rank_icon} に昇級しました！"))

    await interaction.followup.send("✅ 勝敗を反映しました。", ephemeral=True)

# イベント設定
@tree.command(name="イベント設定")
async def event_setup(interaction: discord.Interaction, start: str, end: str):
    await interaction.response.defer(ephemeral=True)
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        await interaction.followup.send("⚠️ 日時の形式が正しくありません (例: 2025-10-15T14:00)", ephemeral=True)
        return
    global event_info
    event_info = {"start": start, "end": end}
    save_json(EVENT_FILE, event_info)
    await interaction.followup.send(f"イベントを {start} 〜 {end} に設定しました", ephemeral=True)

# Bot 起動時
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"{bot.user} が起動しました。")

bot.run(TOKEN)
