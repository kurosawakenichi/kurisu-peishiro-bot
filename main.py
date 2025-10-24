import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json

# --- 設定 ---
TOKEN = "YOUR_DISCORD_TOKEN"
GUILD_ID = 1427541907009044502  # 例
ADMIN_ID = 123456789012345678
JUDGE_CHANNEL_ID = 987654321098765432
RANKING_CHANNEL_ID = 876543210987654321

# --- Bot初期化 ---
intents = discord.Intents.default()
intents.members = True
client = commands.Bot(command_prefix='/', intents=intents)
tree = client.tree

# --- データ管理 ---
users = {}  # {user_id: {pt, role, ...}}
in_match = {}  # 成立中マッチ
waiting_list = []  # マッチ希望

# --- タスク ---
@tasks.loop(seconds=60)
async def cleanup_task():
    # マッチ希望5分タイマー処理
    now = discord.utils.utcnow()
    for user in waiting_list[:]:
        if now - user['requested_at'] > discord.utils.timedelta(minutes=5):
            waiting_list.remove(user)
            print(f"マッチ希望自動削除: {user['id']}")

# --- ユーティリティ ---
def get_user_role(pt):
    if pt < 10:
        return '🔰'
    elif pt < 50:
        return '🥈'
    elif pt < 100:
        return '🥇'
    elif pt < 200:
        return '⚔️'
    elif pt < 500:
        return '🪽'
    else:
        return '😈'

async def update_member_role(member: discord.Member, pt: int):
    new_role_name = get_user_role(pt)
    # ロール付与処理
    for role in member.roles:
        if role.name in ['🔰','🥈','🥇','⚔️','🪽','😈']:
            await member.remove_roles(role)
    guild_role = discord.utils.get(member.guild.roles, name=new_role_name)
    if guild_role:
        await member.add_roles(guild_role)

# --- Botイベント ---
@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if guild:
        await tree.sync(guild=guild)
    print(f"{client.user} is ready. Guild: {GUILD_ID}")
    if not cleanup_task.is_running():
        cleanup_task.start()

# --- コマンド ---
@tree.command(guild=discord.Object(id=GUILD_ID), name='マッチ希望')
async def match_request(interaction: discord.Interaction):
    user_id = interaction.user.id
    # 重複登録チェック
    if any(u['id']==user_id for u in waiting_list):
        await interaction.response.send_message("すでにマッチ希望に登録されています。", ephemeral=True)
        return
    waiting_list.append({'id': user_id, 'requested_at': discord.utils.utcnow()})
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。")
    # 抽選処理
    if len(waiting_list) >= 2:
        p1 = waiting_list.pop(0)
        p2 = waiting_list.pop(0)
        in_match[p1['id']] = {'opponent': p2['id'], 'start': discord.utils.utcnow()}
        in_match[p2['id']] = {'opponent': p1['id'], 'start': discord.utils.utcnow()}
        await interaction.channel.send(f"マッチ成立: <@{p1['id']}> vs <@{p2['id']}>")

@tree.command(guild=discord.Object(id=GUILD_ID), name='結果報告')
async def result_report(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member):
    # 成立中マッチ確認
    if winner.id not in in_match or loser.id != in_match[winner.id]['opponent']:
        await interaction.response.send_message("このマッチは存在しません。", ephemeral=True)
        return
    # PT更新
    users.setdefault(winner.id, {'pt':0})['pt'] += 1
    users.setdefault(loser.id, {'pt':0})['pt'] = max(users[loser.id]['pt']-1,0)
    # ロール更新
    await update_member_role(winner, users[winner.id]['pt'])
    member_loser = interaction.guild.get_member(loser.id)
    await update_member_role(member_loser, users[loser.id]['pt'])
    # マッチ消去
    del in_match[winner.id]
    del in_match[loser.id]
    await interaction.response.send_message(f"結果登録完了: {winner.display_name} 勝利, {loser.display_name} 敗北")

@tree.command(name='ランキング')
async def ranking(interaction: discord.Interaction):
    sorted_users = sorted(users.items(), key=lambda x:x[1]['pt'], reverse=True)
    lines = [f"<@{uid}>: {data['pt']}pt" for uid,data in sorted_users]
    text = "\n".join(lines) if lines else "ランキングはまだありません。"
    await interaction.response.send_message(text)

@tree.command(guild=discord.Object(id=GUILD_ID), name='admin_reset_all')
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    for uid in users.keys():
        users[uid]['pt'] = 0
        member = interaction.guild.get_member(uid)
        if member:
            await update_member_role(member, 0)
    await interaction.response.send_message("全ユーザーのPTをリセットしました。")

@tree.command(guild=discord.Object(id=GUILD_ID), name='admin_set_pt')
async def admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    users.setdefault(target.id, {'pt':0})['pt'] = pt
    await update_member_role(target, pt)
    await interaction.response.send_message(f"{target.display_name} のPTを {pt} に設定しました。")

# --- Bot起動 ---
client.run(TOKEN)
