import os
import json
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import datetime, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DATA_FILE = "players.json"

# 階級設定
RANKS = [
    {"name": "Beginner", "icon": "🔰", "min": 0, "max": 4},
    {"name": "Silver", "icon": "🥈", "min": 5, "max": 9},
    {"name": "Gold", "icon": "🥇", "min": 10, "max": 14},
    {"name": "Master", "icon": "⚔️", "min": 15, "max": 19},
    {"name": "GroundMaster", "icon": "🪽", "min": 20, "max": 24},
    {"name": "Challenger", "icon": "😈", "min": 25, "max": 9999}
]

def load_players():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_players(players):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)

def get_rank_icon(pt, challenge=False):
    for r in RANKS:
        if r["min"] <= pt <= r["max"]:
            return r["icon"] + ("🔥" if challenge else "")
    return "❓"

@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"ギルド {GUILD_ID} が取得できませんでした。コマンド同期をスキップします。")
        return

    await tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    print("ギルドにコマンド同期完了")

# --- イベント設定コマンド ---
@tree.command(name="イベント設定", description="イベントの開始と終了を設定", guild=discord.Object(id=GUILD_ID))
async def set_event(interaction: discord.Interaction, start: str, end: str):
    # start/end は "YYYY-MM-DD HH:MM" 形式想定
    players = load_players()
    event_info = {"start": start, "end": end}
    players["_event"] = event_info
    save_players(players)
    await interaction.response.send_message(f"イベント開始: {start}, 終了: {end}", ephemeral=True)

# --- マッチング申請 ---
@tree.command(name="マッチング申請", description="対戦相手にマッチング申請", guild=discord.Object(id=GUILD_ID))
async def matching_request(interaction: discord.Interaction, opponent: discord.Member):
    players = load_players()
    uid = str(interaction.user.id)
    oid = str(opponent.id)

    if uid not in players:
        players[uid] = {"pt": 0, "challenge": False}
    if oid not in players:
        players[oid] = {"pt": 0, "challenge": False}

    # 既存申請チェック
    match_key = f"{uid}-{oid}"
    if "_matches" not in players:
        players["_matches"] = {}
    if match_key in players["_matches"]:
        await interaction.response.send_message("既に申請済です。取り下げ可能です。", ephemeral=True)
        return

    players["_matches"][match_key] = {"approved": False}
    save_players(players)
    await interaction.response.send_message(f"{opponent.mention} にマッチング申請を送りました。相手が承認するまでお待ちください。", ephemeral=True)

# --- 承認 ---
@tree.command(name="承認", description="マッチング申請を承認", guild=discord.Object(id=GUILD_ID))
async def approve(interaction: discord.Interaction, requester: discord.Member):
    players = load_players()
    uid = str(requester.id)
    oid = str(interaction.user.id)
    match_key = f"{uid}-{oid}"
    if "_matches" not in players or match_key not in players["_matches"]:
        await interaction.response.send_message("該当する申請がありません。", ephemeral=True)
        return

    players["_matches"][match_key]["approved"] = True
    save_players(players)
    await interaction.response.send_message("承認しました。", ephemeral=True)

# --- 試合結果報告 ---
@tree.command(name="試合結果報告", description="勝者が結果を報告", guild=discord.Object(id=GUILD_ID))
async def report(interaction: discord.Interaction, loser: discord.Member):
    players = load_players()
    winner_id = str(interaction.user.id)
    loser_id = str(loser.id)
    match_key = f"{winner_id}-{loser_id}"
    if "_matches" not in players or match_key not in players["_matches"]:
        await interaction.response.send_message(f"マッチング申請が承認されていません。@kurosawa0118 に報告してください。", ephemeral=True)
        return
    if not players["_matches"][match_key]["approved"]:
        await interaction.response.send_message(f"対戦相手が承認していません。@kurosawa0118 に報告してください。", ephemeral=True)
        return

    # --- Pt計算 ---
    win_pt = 1
    lose_pt = -1
    players.setdefault(winner_id, {"pt": 0, "challenge": False})
    players.setdefault(loser_id, {"pt": 0, "challenge": False})

    # 階級差で増減
    def get_rank_idx(pt):
        for i, r in enumerate(RANKS):
            if r["min"] <= pt <= r["max"]:
                return i
        return 0

    diff = get_rank_idx(players[winner_id]["pt"]) - get_rank_idx(players[loser_id]["pt"])
    if diff == 0:
        pass  # +1/-1
    else:
        win_pt += diff
        lose_pt -= abs(diff)

    # pt更新
    players[winner_id]["pt"] += win_pt
    players[loser_id]["pt"] = max(0, players[loser_id]["pt"] + lose_pt)

    # 昇格チャレンジ判定
    for pid in [winner_id, loser_id]:
        p = players[pid]
        challenge_thresholds = [4, 9, 14, 19, 24]
        p["challenge"] = p["pt"] in challenge_thresholds

    save_players(players)
    # ロール更新
    guild = bot.get_guild(GUILD_ID)
    for uid in [winner_id, loser_id]:
        member = guild.get_member(int(uid))
        if member:
            try:
                new_icon = get_rank_icon(players[uid]["pt"], players[uid]["challenge"])
                await member.edit(nick=f"{member.name} {new_icon}")
            except:
                pass

    await interaction.response.send_message(f"{interaction.user.mention} が {loser.mention} に勝利しました！", ephemeral=False)

bot.run(TOKEN)
