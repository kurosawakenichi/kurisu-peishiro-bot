import os
import discord
from discord import app_commands
from discord.ext import tasks
import asyncio
from datetime import datetime, timedelta

# 環境変数
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -----------------------------
# データ管理
# -----------------------------
players = {}  # ユーザーID: pt
match_request_list = []  # ランダムマッチ希望者
in_match = {}  # マッチ中: {user_id: match_info}
pending_judge = {}  # 異議審議中

# -----------------------------
# ユーティリティ関数
# -----------------------------
def get_user_pt(user_id):
    return players.get(user_id, 0)

def set_user_pt(user_id, pt):
    players[user_id] = pt

def pt_to_emoji(pt):
    # 例: 0-9 🔰, 10-19 🥈, 20-29 🥇 ...
    if pt < 10:
        return "🔰"
    elif pt < 20:
        return "🥈"
    elif pt < 30:
        return "🥇"
    elif pt < 40:
        return "⚔️"
    elif pt < 50:
        return "🪽"
    else:
        return "😈"

# -----------------------------
# コマンド定義
# -----------------------------

@tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in [m['user_id'] for m in in_match.values()]:
        await interaction.response.send_message("すでに対戦中です。", ephemeral=True)
        return
    if user_id in match_request_list:
        await interaction.response.send_message("すでにマッチ希望済みです。", ephemeral=True)
        return

    match_request_list.append(user_id)
    await interaction.response.send_message("マッチ希望を受け付けました。マッチング抽選中です。", ephemeral=True)

    # 簡易マッチ抽選（同pt帯・ランダム）
    await asyncio.sleep(5)  # 5秒待機
    candidates = [uid for uid in match_request_list if uid != user_id]
    if candidates:
        opponent_id = candidates[0]
        # マッチ成立
        in_match[user_id] = {"opponent": opponent_id, "start": datetime.utcnow(), "winner": None}
        in_match[opponent_id] = {"opponent": user_id, "start": datetime.utcnow(), "winner": None}
        match_request_list.remove(user_id)
        match_request_list.remove(opponent_id)

        await interaction.followup.send(
            f"<@{user_id}> と <@{opponent_id}> のマッチが成立しました。試合後、勝者が /結果報告 を行なってください",
            ephemeral=True
        )
        # 相手にも通知
        opponent = client.get_user(opponent_id)
        if opponent:
            await opponent.send(
                f"<@{user_id}> とマッチが成立しました。試合後、勝者が /結果報告 を行なってください"
            )
    else:
        # 余りは希望リストに残る
        await interaction.followup.send("マッチング相手が見つかりませんでした。しばらくお待ちください。", ephemeral=True)

@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます")
async def cancel_match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in match_request_list:
        match_request_list.remove(user_id)
        await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望は登録されていません。", ephemeral=True)

@tree.command(name="結果報告", description="勝者が報告します")
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = None
    for uid, match in in_match.items():
        if winner.id == uid:
            loser_id = match["opponent"]
            break
    if not loser_id:
        await interaction.response.send_message("このマッチングは登録されていません。", ephemeral=True)
        return

    # 審議なしなら pt 加減算
    in_match[winner.id]["winner"] = winner.id
    in_match[loser_id]["winner"] = winner.id

    set_user_pt(winner.id, get_user_pt(winner.id) + 1)
    set_user_pt(loser_id, max(0, get_user_pt(loser_id) - 1))

    await interaction.response.send_message(
        f"勝者: <@{winner.id}> (PT: {get_user_pt(winner.id)})\n"
        f"敗者: <@{loser_id}> (PT: {get_user_pt(loser_id)})"
    )

    # マッチクリア
    del in_match[winner.id]
    del in_match[loser_id]

@tree.command(name="ランキング", description="全ユーザーのPTランキングを表示")
async def show_ranking(interaction: discord.Interaction):
    ranking = sorted(players.items(), key=lambda x: -x[1])
    text = "\n".join([f"<@{uid}>: {pt} {pt_to_emoji(pt)}" for uid, pt in ranking])
    await interaction.response.send_message(f"PTランキング:\n{text}")

@tree.command(name="admin_reset_all", description="管理者用: 全プレイヤーPTリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    for uid in players:
        players[uid] = 0
    await interaction.response.send_message("全プレイヤーのptをリセットしました")

@tree.command(name="admin_set_pt", description="管理者用: 任意ユーザーのPT設定")
async def admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    set_user_pt(target.id, pt)
    await interaction.response.send_message(f"<@{target.id}> のPTを {pt} に設定しました。")

# -----------------------------
# Bot起動時
# -----------------------------
@client.event
async def on_ready():
    print(f"{client.user} is ready. Guild ID: {GUILD_ID}")
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print("新しいコマンドをギルドに同期しました")

client.run(TOKEN)
