import os
import discord
from discord.ext import tasks
from discord import app_commands
import asyncio
import random

GUILD_ID = int(os.environ["GUILD_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# 内部データ管理
match_request_list = {}
drawing_list = []
in_match = {}
user_pt = {}

# マッチロジックの補助関数
def can_match(pt1, pt2):
    # 階級差によるマッチ不可制限を考慮
    rank1 = pt1 // 5
    rank2 = pt2 // 5
    return abs(rank1 - rank2) < 3

def get_rank_emoji(pt):
    if pt <= 4:
        return "🔰"
    elif pt <= 9:
        return "🥈"
    elif pt <= 14:
        return "🥇"
    elif pt <= 19:
        return "⚔️"
    elif pt <= 24:
        return "🪽"
    else:
        return "😈"

@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")

    guild = bot.get_guild(GUILD_ID)  # 修正版：実際の Guild オブジェクト取得
    if guild is None:
        print("指定されたギルドが見つかりません")
        return

    # ギルド単位で旧コマンドクリア
    await tree.clear_commands(guild=guild)

    # 新コマンド同期
    await tree.sync(guild=guild)
    print("Commands synced")

# /マッチ希望
@tree.command(name="マッチ希望", description="ランダムマッチに参加", guild=discord.Object(id=GUILD_ID))
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in in_match:
        await interaction.response.send_message("既に対戦中のため申請できません", ephemeral=True)
        return

    match_request_list[user_id] = {"time": asyncio.get_event_loop().time()}
    await interaction.response.send_message("マッチング中です", ephemeral=True)

    # 抽選処理
    await asyncio.sleep(5)  # 待機時間
    candidates = list(match_request_list.keys())
    random.shuffle(candidates)

    paired = set()
    for i in range(0, len(candidates) - 1, 2):
        a, b = candidates[i], candidates[i+1]
        pt_a = user_pt.get(a, 0)
        pt_b = user_pt.get(b, 0)
        if can_match(pt_a, pt_b):
            in_match[a] = b
            in_match[b] = a
            paired.update({a, b})
            # 両者にのみ通知
            user_a = bot.get_user(a)
            user_b = bot.get_user(b)
            if user_a:
                await user_a.send(f"マッチ成立: <@{a}> vs <@{b}> 試合後、勝者が /結果報告 を行なってください")
            if user_b:
                await user_b.send(f"マッチ成立: <@{a}> vs <@{b}> 試合後、勝者が /結果報告 を行なってください")

    # 成立した組は希望リストから削除
    for u in paired:
        if u in match_request_list:
            del match_request_list[u]

# /マッチ希望取下げ
@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げる", guild=discord.Object(id=GUILD_ID))
async def cancel_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in match_request_list:
        del match_request_list[user_id]
        await interaction.response.send_message("マッチ希望を取り下げました", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望はありません", ephemeral=True)

# /結果報告
@tree.command(name="結果報告", description="勝者が申告", guild=discord.Object(id=GUILD_ID))
async def report_result(interaction: discord.Interaction, winner: discord.Member):
    loser_id = in_match.get(winner.id)
    if loser_id is None:
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします。", ephemeral=True)
        return
    # 異議などは別処理で管理
    # 勝利処理
    user_pt[winner.id] = user_pt.get(winner.id, 0) + 1
    user_pt[loser_id] = max(user_pt.get(loser_id, 0) - 1, 0)
    # マッチ終了
    del in_match[winner.id]
    del in_match[loser_id]
    await interaction.response.send_message(f"{winner.display_name} が勝利しました。")

# 管理者コマンド
@tree.command(name="admin_reset_all", description="全プレイヤーのptをリセット", guild=discord.Object(id=GUILD_ID))
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    for uid in user_pt:
        user_pt[uid] = 0
    await interaction.response.send_message("全プレイヤーのptをリセットしました", ephemeral=False)

@tree.command(name="admin_set_pt", description="指定ユーザーのptを設定", guild=discord.Object(id=GUILD_ID))
async def admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    user_pt[member.id] = pt
    await interaction.response.send_message(f"{member.display_name} のptを {pt} に設定しました", ephemeral=False)

# /ランキング
@tree.command(name="ランキング", description="全ユーザーのptランキング表示", guild=discord.Object(id=GUILD_ID))
async def ranking(interaction: discord.Interaction):
    sorted_users = sorted(user_pt.items(), key=lambda x: -x[1])
    embed = discord.Embed(title="🏆 ランキング")
    rank = 1
    prev_pt = None
    for uid, pt in sorted_users:
        if prev_pt is not None and pt < prev_pt:
            rank += 1
        prev_pt = pt
        user = bot.get_user(uid)
        if user:
            embed.add_field(name=f"{rank}位 {user.display_name}", value=f"{get_rank_emoji(pt)} {pt}pt", inline=False)
    await interaction.response.send_message(embed=embed)

bot.run(DISCORD_TOKEN)
