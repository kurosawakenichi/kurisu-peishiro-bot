import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
from datetime import datetime, timedelta

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ----- プレイヤー情報管理 -----
# JSONではなく既存方式。参加中にメモリ上で管理
players = {}  # user_id: { "pt": 0, "rank": "Beginner", "challenge": False, "max_pt": 0 }

RANKS = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, float('inf'), "Challenger", "😈")
]

# ----- ユーティリティ関数 -----
def get_rank(pt):
    for low, high, name, emoji in RANKS:
        if low <= pt <= high:
            return name, emoji
    return "Beginner", "🔰"

async def update_member_display(user: discord.Member):
    pdata = players.get(user.id)
    if not pdata:
        return
    rank_name, rank_emoji = get_rank(pdata["pt"])
    challenge_icon = "🔥" if pdata.get("challenge") else ""
    new_nick = f"{rank_emoji}{challenge_icon} {user.name} ({pdata['pt']}pt)"
    try:
        await user.edit(nick=new_nick)
    except discord.Forbidden:
        pass

def adjust_pt(winner_id, loser_id):
    winner = players[winner_id]
    loser = players[loser_id]

    # 階級差計算
    winner_rank_idx = next(i for i,r in enumerate(RANKS) if r[2]==get_rank(winner['pt'])[0])
    loser_rank_idx = next(i for i,r in enumerate(RANKS) if r[2]==get_rank(loser['pt'])[0])
    diff = abs(winner_rank_idx - loser_rank_idx)

    # PT計算
    if winner_rank_idx == loser_rank_idx:
        winner['pt'] += 1
        loser['pt'] = max(0, loser['pt'] - 1)
    elif winner_rank_idx < loser_rank_idx:
        winner['pt'] += 1 + diff
        loser['pt'] = max(0, loser['pt'] - 1)
    else:
        winner['pt'] += 1
        loser['pt'] = max(0, loser['pt'] - 1 - diff)

    # 最大PT更新
    if winner['pt'] > winner.get('max_pt',0):
        winner['max_pt'] = winner['pt']

    # 昇級チャレンジ判定
    for threshold in [4, 9, 14, 19, 24]:
        if winner['pt'] == threshold:
            winner['challenge'] = True
            break

# ----- マッチング管理 -----
match_requests = {}  # user_id: target_id
pending_approval = {}  # winner_id: loser_id

# ----- コマンド登録 -----
def register_commands():
    @tree.command(name="イベント設定", description="イベント開始・終了日時を設定")
    @app_commands.describe(開始="開始日時 (YYYY-MM-DD HH:MM)", 終了="終了日時 (YYYY-MM-DD HH:MM)")
    async def イベント設定(interaction: discord.Interaction, 開始: str, 終了: str):
        await interaction.response.send_message(f"イベントを {開始} 〜 {終了} に設定しました", ephemeral=True)

    @tree.command(name="ランキングリセット", description="ランキングをリセット")
    async def ランキングリセット(interaction: discord.Interaction):
        for uid in players:
            players[uid]['pt'] = 0
            players[uid]['challenge'] = False
        await interaction.response.send_message("ランキングをリセットしました", ephemeral=True)

    @tree.command(name="マッチング申請", description="対戦相手を申請")
    @app_commands.describe(相手="対戦相手を指定")
    async def マッチング申請(interaction: discord.Interaction, 相手: discord.Member):
        uid = interaction.user.id
        tid = 相手.id
        if uid in match_requests or tid in match_requests.values():
            await interaction.response.send_message("すでにマッチング申請中です", ephemeral=True)
            return
        match_requests[uid] = tid
        await interaction.response.send_message(f"{相手.display_name} に対戦申請しました。承認待ちです", ephemeral=True)

    @tree.command(name="承認", description="対戦申請を承認")
    async def 承認(interaction: discord.Interaction):
        uid = interaction.user.id
        found = None
        for winner, loser in match_requests.items():
            if loser == uid:
                found = (winner, loser)
                break
        if not found:
            await interaction.response.send_message("承認対象の申請がありません", ephemeral=True)
            return
        winner, loser = found
        pending_approval[winner] = loser
        del match_requests[winner]
        await interaction.response.send_message("承認しました。勝者が /試合結果報告 で報告可能です", ephemeral=True)

    @tree.command(name="試合結果報告", description="試合結果を報告")
    async def 試合結果報告(interaction: discord.Interaction):
        winner_id = interaction.user.id
        if winner_id not in pending_approval:
            await interaction.response.send_message(f"承認済の申請がありません。@kurosawa0118 に連絡してください", ephemeral=True)
            return
        loser_id = pending_approval[winner_id]
        adjust_pt(winner_id, loser_id)
        del pending_approval[winner_id]

        winner = bot.get_user(winner_id)
        loser = bot.get_user(loser_id)
        # ロール・名前更新
        await update_member_display(winner)
        await update_member_display(loser)
        # ランキングチャンネル告知
        guild = bot.get_guild(GUILD_ID)
        ranking_channel = discord.utils.get(guild.text_channels, name="ランキング")
        rank_name, rank_emoji = get_rank(players[winner_id]['pt'])
        await ranking_channel.send(f"🔥 {winner.mention} が {rank_name}{rank_emoji} に昇級しました！")

        await interaction.response.send_message(f"{winner.display_name} vs {loser.display_name} の結果を反映しました", ephemeral=True)

# ----- 起動処理 -----
@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")

    guild = discord.Object(id=GUILD_ID)
    try:
        print("[INFO] ギルドコマンド全削除＆再同期中...")
        await tree.clear_commands(guild=guild)
        await tree.sync(guild=guild)
        await tree.sync()
        register_commands()
        await tree.sync(guild=guild)
        print("[INFO] コマンド同期完了 ✅")
    except Exception as e:
        print("[ERROR] コマンド同期中にエラー発生:", e)

    print(f"✅ {bot.user} が起動しました。")

# ----- 実行 -----
if __name__ == "__main__":
    print("[START] Bot starting...")
    bot.run(TOKEN)
