import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta
import asyncio

# 環境変数
ADMIN_ID = int(os.getenv("ADMIN_ID"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

# データ構造
user_data = {}  # user_id: {'pt': int, 'rank': str, 'challenge': bool}
active_matches = {}  # (user_id, opponent_id): match_info
challenge_states = {3:2,4:2,8:7,9:7,13:12,14:12,18:17,19:17,23:22,24:22}

# ランク・ロール設定
rank_table = [
    (0,2,'Beginner','🔰'),
    (3,3,'SilverChallenge1','🔰🔥'),
    (4,4,'SilverChallenge2','🔰🔥🔥'),
    (5,7,'Silver','🥈'),
    (8,8,'GoldChallenge1','🥈🔥'),
    (9,9,'GoldChallenge2','🥈🔥🔥'),
    (10,12,'Gold','🥇'),
    (13,13,'MasterChallenge1','🥇🔥'),
    (14,14,'MasterChallenge2','🥇🔥🔥'),
    (15,17,'Master','⚔️'),
    (18,18,'GrandMasterChallenge1','⚔️🔥'),
    (19,19,'GrandMasterChallenge2','⚔️🔥🔥'),
    (20,22,'GrandMaster','🪽'),
    (23,23,'ChallengerChallenge1','🪽🔥'),
    (24,24,'ChallengerChallenge2','🪽🔥🔥'),
    (25,9999,'Challenger','😈')
]

rank_groups = [
    (0,4),
    (5,9),
    (10,14),
    (15,19),
    (20,24),
    (25,9999)
]

def get_rank_info(pt:int):
    for start,end,name,icon in rank_table:
        if start <= pt <= end:
            return name, icon
    return 'Challenger','😈'

def get_group(pt:int):
    for idx,(start,end) in enumerate(rank_groups,1):
        if start <= pt <= end:
            return idx
    return 6

# --- マッチング ---
class ApproveMatchView(discord.ui.View):
    def __init__(self, winner_id, loser_id):
        super().__init__(timeout=900)
        self.winner_id = winner_id
        self.loser_id = loser_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction:discord.Interaction, button:discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです", ephemeral=True)
            return

        await process_match_result(self.winner_id, self.loser_id)
        await interaction.response.edit_message(content=f"勝者 <@{self.winner_id}> にPTを反映しました", view=None)

async def process_match_result(winner_id:int, loser_id:int):
    # 内部データ取得
    winner_pt = user_data[winner_id]['pt']
    loser_pt = user_data[loser_id]['pt']
    winner_group = get_group(winner_pt)
    loser_group = get_group(loser_pt)
    # ランク差計算
    rank_diff = loser_group - winner_group
    # Pt計算
    pt_change = 0
    if rank_diff >= 2:
        pt_change = 3
    elif rank_diff == 1:
        pt_change = 2
    elif rank_diff == 0:
        pt_change = 1
    elif rank_diff == -1:
        pt_change = 1
    elif rank_diff <= -2:
        pt_change = 1
    # 敗者Pt変化
    loser_change = 1
    if rank_diff == -1: loser_change = 2
    if rank_diff <= -2: loser_change = 3
    # Pt更新
    winner_new = winner_pt + pt_change
    loser_new = loser_pt - loser_change
    # 昇級チャレンジ例外処理
    if winner_new in challenge_states:
        winner_new = challenge_states[winner_new]
    if loser_new in challenge_states:
        loser_new = challenge_states[loser_new]
    user_data[winner_id]['pt'] = winner_new
    user_data[loser_id]['pt'] = max(loser_new,0)
    # ユーザー名更新
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    # マッチ削除
    active_matches.pop((winner_id,loser_id), None)
    active_matches.pop((loser_id,winner_id), None)

async def update_member_display(user_id:int):
    guild = bot.guilds[0]
    member = guild.get_member(user_id)
    pt = user_data[user_id]['pt']
    rank, icon = get_rank_info(pt)
    await member.edit(nick=f"{member.name} {icon} {pt}pt")

# --- コマンド ---
@bot.tree.command(name="マッチ申請")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction:discord.Interaction, opponent:discord.Member):
    if interaction.user.id == opponent.id:
        await interaction.response.send_message("自分には申請できません", ephemeral=True)
        return
    if (interaction.user.id, opponent.id) in active_matches or (opponent.id, interaction.user.id) in active_matches:
        await interaction.response.send_message(f"<@{opponent.id}> とのマッチはすでに存在します。取り消しますか？", ephemeral=True)
        return
    active_matches[(interaction.user.id, opponent.id)] = {'time':datetime.utcnow()}
    view = ApproveMatchView(interaction.user.id, opponent.id)
    await opponent.send(f"{interaction.user.mention} がマッチ申請しました。承認してください。", view=view)
    await interaction.response.send_message("マッチ申請を送信しました。", ephemeral=True)

@bot.tree.command(name="結果報告")
@app_commands.describe(opponent_id="敗者のユーザーID")
async def cmd_result_report(interaction:discord.Interaction, opponent_id:int):
    if (interaction.user.id, opponent_id) not in active_matches and (opponent_id, interaction.user.id) not in active_matches:
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします", ephemeral=True)
        return
    view = ApproveMatchView(interaction.user.id, opponent_id)
    await interaction.response.send_message(f"<@{opponent_id}> この試合の勝者は <@{interaction.user.id}> です。結果に同意しますか？", view=view)

@bot.tree.command(name="admin_show_ranking")
async def cmd_admin_show_ranking(interaction:discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    ranking_list = []
    for uid,data in user_data.items():
        member = interaction.guild.get_member(uid)
        if member:
            ranking_list.append(f"{member.name}")  # 表示はユーザー名のみ
    await interaction.response.send_message("\n".join(ranking_list), ephemeral=True)

@bot.tree.command(name="admin_reset_all")
async def cmd_admin_reset_all(interaction:discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    for uid in user_data.keys():
        user_data[uid]['pt'] = 0
        await update_member_display(uid)
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

@bot.tree.command(name="admin_set_pt")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction:discord.Interaction, user:discord.Member, pt:int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です", ephemeral=True)
        return
    if user.id not in user_data:
        user_data[user.id] = {'pt':0,'rank':'Beginner','challenge':False}
    user_data[user.id]['pt'] = pt
    await update_member_display(user.id)
    await interaction.response.send_message(f"{user.mention} のPTを {pt} に設定しました。", ephemeral=True)

# --- 自動ランキング投稿 ---
@tasks.loop(minutes=60)
async def auto_post_ranking():
    now = datetime.utcnow().hour + 9  # JST換算
    if now in [13,22]:
        guild = bot.guilds[0]
        channel = guild.get_channel(1427542200614387846)
        ranking_list = []
        for uid,data in user_data.items():
            member = guild.get_member(uid)
            if member:
                rank, icon = get_rank_info(data['pt'])
                ranking_list.append(f"{member.name} {icon} {data['pt']}pt")
        await channel.send("\n".join(ranking_list))

@bot.event
async def on_ready():
    print(f"{bot.user} is ready")
    auto_post_ranking.start()

bot.run(os.getenv("DISCORD_TOKEN"))
