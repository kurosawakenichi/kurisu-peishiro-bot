import discord
from discord.ext import tasks
from discord import app_commands
import asyncio
from datetime import datetime, time, timedelta
import os

# ----------------------------------------
# 環境変数
# ----------------------------------------
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID"))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID"))

# ----------------------------------------
# 定数
# ----------------------------------------
AUTO_APPROVE_SECONDS = 15 * 60  # 15分で自動承認

# ----------------------------------------
# データ
# ----------------------------------------
user_data = {}   # {user_id: {"pt": int}}
matching = {}    # {user_id: opponent_id}

# ----------------------------------------
# ランク定義（表示用）
# 各タプル: (start_pt, end_pt, role_name, icon_for_display)
# ----------------------------------------
rank_roles = [
    (0, 2, "Beginner", "🔰"),
    (3, 3, "SilverChallenge1", "🔰🔥"),
    (4, 4, "SilverChallenge2", "🔰🔥🔥"),
    (5, 7, "Silver", "🥈"),
    (8, 8, "GoldChallenge1", "🥈🔥"),
    (9, 9, "GoldChallenge2", "🥈🔥🔥"),
    (10, 12, "Gold", "🥇"),
    (13, 13, "MasterChallenge1", "🥇🔥"),
    (14, 14, "MasterChallenge2", "🥇🔥🔥"),
    (15, 17, "Master", "⚔️"),
    (18, 18, "GrandMasterChallenge1", "⚔️🔥"),
    (19, 19, "GrandMasterChallenge2", "⚔️🔥🔥"),
    (20, 22, "GrandMaster", "🪽"),
    (23, 23, "ChallengerChallenge1", "🪽🔥"),
    (24, 24, "ChallengerChallenge2", "🪽🔥🔥"),
    (25, 9999, "Challenger", "😈"),
]

# 内部ランク階層（rank1..rank6） : マッチ判定とpt増減ロジック簡略化用
rank_ranges_internal = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000),# 25+
}

# ----------------------------------------
# ユーティリティ関数
# ----------------------------------------
def get_rank_info(pt:int):
    for start, end, role, icon in rank_roles:
        if start <= pt <= end:
            return start, end, role, icon
    return 0, 0, "Unknown", ""

def get_internal_rank(pt:int):
    for rank, r in rank_ranges_internal.items():
        if pt in r:
            return rank
    return 1

def calculate_pt(pt_a:int, pt_b:int, result:str):
    rank_a = get_internal_rank(pt_a)
    rank_b = get_internal_rank(pt_b)
    diff = rank_b - rank_a
    delta = 0
    if result == "win":
        if diff == 0:
            delta = 1
        elif diff == 1:
            delta = 2
        elif diff == 2:
            delta = 3
        elif diff == -1:
            delta = 1
        elif diff == -2:
            delta = 1
    elif result == "lose":
        if diff == 0:
            delta = -1
        elif diff == 1:
            delta = -1
        elif diff == 2:
            delta = -1
        elif diff == -1:
            delta = -2
        elif diff == -2:
            delta = -3
    new_pt = pt_a + delta
    # 例外の降格
    if pt_a in (3,4) and new_pt < 2:
        new_pt = 2
    elif pt_a in (8,9) and new_pt < 7:
        new_pt = 7
    elif pt_a in (13,14) and new_pt < 12:
        new_pt = 12
    elif pt_a in (18,19) and new_pt < 17:
        new_pt = 17
    elif pt_a in (23,24) and new_pt < 22:
        new_pt = 22
    return new_pt

# ----------------------------------------
# Discord Bot
# ----------------------------------------
intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ----------------------------------------
# ランキング関連
# ----------------------------------------
async def update_member_display(member:discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    start, end, role_name, icon = get_rank_info(pt)
    try:
        await member.edit(nick=f"{member.display_name} {icon} {pt}pt")
    except:
        pass
    # ロール更新
    guild = member.guild
    for start_r, end_r, role, _ in rank_roles:
        role_obj = discord.utils.get(guild.roles, name=role)
        if role_obj:
            if start_r <= pt <= end_r:
                if role_obj not in member.roles:
                    await member.add_roles(role_obj)
            else:
                if role_obj in member.roles:
                    await member.remove_roles(role_obj)

def generate_ranking_text():
    # pt順ソート
    sorted_users = sorted(user_data.items(), key=lambda x: -x[1].get("pt",0))
    ranking_lines = []
    rank = 0
    prev_pt = None
    displayed_rank = 0
    for i, (uid, data) in enumerate(sorted_users):
        pt = data.get("pt",0)
        start, end, role_name, icon = get_rank_info(pt)
        if pt != prev_pt:
            displayed_rank = i+1
            prev_pt = pt
        user_line = f"{displayed_rank}位 <@{uid}> {icon} {pt}pt"
        ranking_lines.append(user_line)
    return "\n".join(ranking_lines)

async def post_ranking(channel_id:int):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(channel_id)
    if not ch:
        return
    text = generate_ranking_text()
    await ch.send(f"📊 ランキング\n{text}")

# 自動投稿タスク
@tasks.loop(minutes=1)
async def auto_post_ranking_task():
    now = datetime.utcnow() + timedelta(hours=9)
    if now.hour == 14 and now.minute == 0:
        await post_ranking(RANKING_CHANNEL_ID)
    elif now.hour == 23 and now.minute == 0:
        await post_ranking(RANKING_CHANNEL_ID)

# ----------------------------------------
# マッチ承認ビュー
# ----------------------------------------
class ApproveMatchView(discord.ui.View):
    def __init__(self, applicant_id:int, opponent_id:int, origin_channel_id:int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.opponent_id = opponent_id
        self.origin_channel_id = origin_channel_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("承認できるのは申請された相手のみです。", ephemeral=True)
            return
        matching[self.applicant_id] = self.opponent_id
        matching[self.opponent_id] = self.applicant_id
        guild = interaction.guild
        ch = guild.get_channel(self.origin_channel_id) if self.origin_channel_id else interaction.channel
        if ch:
            await ch.send(f"<@{self.applicant_id}> と <@{self.opponent_id}> のマッチングが成立しました。試合後、勝者が結果報告をしてください。")
        await interaction.response.send_message("承認しました。", ephemeral=True)
        self.stop()

class CancelExistingMatchView(discord.ui.View):
    def __init__(self, existing_a:int, existing_b:int):
        super().__init__(timeout=60)
        self.existing_a = existing_a
        self.existing_b = existing_b

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        a = self.existing_a
        b = self.existing_b
        if matching.get(a) == b:
            matching.pop(a, None)
            matching.pop(b, None)
            await interaction.response.send_message(f"<@{a}> と <@{b}> のマッチングは解除されました。", ephemeral=False)
        else:
            await interaction.response.send_message("該当のマッチは既に解除されています。", ephemeral=True)
        self.stop()

class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id:int, loser_id:int):
        super().__init__(timeout=None)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.processed = False

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        await interaction.response.edit_message(content="承認されました。結果を反映します。", view=None)
        await handle_approved_result(self.winner_id, self.loser_id, interaction.channel)

    @discord.ui.button(label="異議", style=discord.ButtonStyle.danger)
    async def dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        await interaction.response.edit_message(content="異議が申立てられました。審判チャンネルへ通知します。", view=None)
        guild = interaction.guild
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID)
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。@<@{ADMIN_ID}> に連絡してください。")
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)

# ----------------------------------------
# 勝者承認処理
# ----------------------------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    if not is_registered_match(winner_id, loser_id):
        await channel.send("このマッチングは登録されていません。まずはマッチング申請をお願いします。")
        return
    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt  = user_data.get(loser_id, {}).get("pt", 0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new  = calculate_pt(loser_pt, winner_pt, "lose")
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id,  {})["pt"] = loser_new
    for g in bot.guilds:
        w_member = g.get_member(winner_id)
        l_member = g.get_member(loser_id)
        if w_member:
            await update_member_display(w_member)
        if l_member:
            await update_member_display(l_member)
    matching.pop(winner_id, None)
    matching.pop(loser_id, None)
    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

def is_registered_match(a:int, b:int):
    return matching.get(a) == b and matching.get(b) == a

# ----------------------------------------
# コマンド: マッチ申請
# ----------------------------------------
@tree.command(name="マッチ申請", description="対戦相手にマッチ申請を出します")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    applicant_pt = user_data.get(applicant.id, {}).get("pt", 0)
    opponent_pt = user_data.get(opponent.id, {}).get("pt", 0)
    # 自身と相手の内部rank差
    rank_diff = abs(get_internal_rank(applicant_pt) - get_internal_rank(opponent_pt))
    if rank_diff >= 3:
        await interaction.response.send_message("申し訳ありません。ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return
    # チャレンジ時制約
    challenge_pts = (3,4,8,9,13,14,18,19,23,24)
    if applicant_pt in challenge_pts or opponent_pt in challenge_pts:
        if get_internal_rank(applicant_pt) != get_internal_rank(opponent_pt):
            await interaction.response.send_message(f"{opponent.display_name} は昇級チャレンジ状態のため、この申請はできません。", ephemeral=True)
            return
    view = ApproveMatchView(applicant.id, opponent.id, interaction.channel.id if interaction.channel else None)
    content = f"<@{opponent.id}> に {applicant.display_name} からマッチ申請が届きました。承認してください。"
    ch = interaction.channel
    await ch.send(content, view=view)
    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)

# ----------------------------------------
# コマンド: 結果報告
# ----------------------------------------
@tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチング申請をお願いします。", ephemeral=True)
        return
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？"
    sent_msg = await interaction.channel.send(content, view=ResultApproveView(winner.id, loser.id))
    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)
    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        if is_registered_match(winner.id, loser.id):
            await handle_approved_result(winner.id, loser.id, interaction.channel)
    bot.loop.create_task(auto_approve_task())

# ----------------------------------------
# 管理者コマンド
# ----------------------------------------
@tree.command(name="admin_reset_all", description="全ユーザーのPTと表示を初期化")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)
        return
    user_data.clear()
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

@tree.command(name="admin_set_pt", description="ユーザーのPTを設定します")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)
        return
    user_data.setdefault(user.id, {})["pt"] = pt
    await update_member_display(user)
    await interaction.response.send_message(f"{user.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# ----------------------------------------
# ランキング確認
# ----------------------------------------
@tree.command(name="ランキング", description="現在のPTランキングを表示します")
async def cmd_show_ranking(interaction: discord.Interaction):
    text = generate_ranking_text()
    await interaction.response.send_message(f"📊 ランキング\n{text}", ephemeral=False)

# ----------------------------------------
# Bot 起動
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    await tree.sync()
    auto_post_ranking_task.start()

bot.run(DISCORD_TOKEN)
