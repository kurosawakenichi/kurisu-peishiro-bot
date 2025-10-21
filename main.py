import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio

# -----------------------
# 環境変数取得
# -----------------------
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID"))
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID"))
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID", RANKING_CHANNEL_ID))  # 審判通知用

# -----------------------
# Bot 初期化
# -----------------------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# -----------------------
# データ保持
# -----------------------
user_data = {}  # {user_id: {"pt": int}}
matching = {}   # {user_id: opponent_id}

AUTO_APPROVE_SECONDS = 15 * 60  # 15分

# -----------------------
# ランク処理補助
# -----------------------
def get_internal_rank(pt: int) -> int:
    if pt <= 4: return 1
    if pt <= 9: return 2
    if pt <= 14: return 3
    if pt <= 19: return 4
    if pt <= 24: return 5
    return 6

def calculate_pt(my_pt: int, opp_pt: int, result: str) -> int:
    # rank差による基本増減
    rank_my = get_internal_rank(my_pt)
    rank_opp = get_internal_rank(opp_pt)
    delta = 0
    if result == "win":
        if rank_my == rank_opp: delta = 1
        elif rank_my + 1 == rank_opp: delta = 2
        elif rank_my + 2 == rank_opp: delta = 3
        elif rank_my - 1 == rank_opp: delta = 1
        elif rank_my - 2 == rank_opp: delta = 1
    else:
        if rank_my == rank_opp: delta = -1
        elif rank_my + 1 == rank_opp: delta = -1
        elif rank_my + 2 == rank_opp: delta = -1
        elif rank_my - 1 == rank_opp: delta = -2
        elif rank_my - 2 == rank_opp: delta = -3

    new_pt = my_pt + delta

    # 例外処理
    if my_pt in (3,8,13,18,23):
        new_pt = min(new_pt, my_pt)
    if my_pt in (4,9,14,19,24):
        new_pt = min(new_pt, my_pt + 1)

    # 敗北時降格
    if result == "lose":
        if my_pt in (3,4): new_pt = 2
        if my_pt in (8,9): new_pt = 7
        if my_pt in (13,14): new_pt = 12
        if my_pt in (18,19): new_pt = 17

    return max(new_pt, 0)

# -----------------------
# メンバー表示更新
# -----------------------
async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    # PT に応じたロール付与・削除
    rank = get_internal_rank(pt)
    role_name = f"Rank{rank}"
    # 既存ロール除去
    for r in member.roles:
        if r.name.startswith("Rank") and r.name != role_name:
            try: await member.remove_roles(r)
            except: pass
    # 付与
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role:
        await member.add_roles(role)

# -----------------------
# マッチ登録確認
# -----------------------
def is_registered_match(a_id: int, b_id: int) -> bool:
    return matching.get(a_id) == b_id and matching.get(b_id) == a_id

# -----------------------
# Views
# -----------------------
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

# -----------------------
# 結果反映処理
# -----------------------
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

# -----------------------
# コマンド: マッチ申請
# -----------------------
@bot.tree.command(name="マッチ申請", description="対戦相手にマッチ申請を出します")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    if applicant.id in matching:
        existing_op = matching.get(applicant.id)
        view = CancelExistingMatchView(applicant.id, existing_op)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view)
        return
    if opponent.id in matching:
        existing_other = matching.get(opponent.id)
        view = CancelExistingMatchView(opponent.id, existing_other)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view)
        return

    applicant_pt = user_data.get(applicant.id, {}).get("pt", 0)
    opponent_pt = user_data.get(opponent.id, {}).get("pt", 0)
    if abs(get_internal_rank(applicant_pt) - get_internal_rank(opponent_pt)) >= 3:
        await interaction.response.send_message("申し訳ありません。ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return

    def challenge_match_ok(my_pt, other_pt):
        if my_pt in (3,8,13,18,23):
            return other_pt >= my_pt
        if my_pt in (4,9,14,19,24):
            return (other_pt >= my_pt) or (other_pt == my_pt - 1)
        return True

    if not challenge_match_ok(applicant_pt, opponent_pt) or not challenge_match_ok(opponent_pt, applicant_pt):
        await interaction.response.send_message("昇級チャレンジ状態のため、この申請はできません。", ephemeral=True)
        return

    view = ApproveMatchView(applicant.id, opponent.id, interaction.channel.id if interaction.channel else None)
    content = f"<@{opponent.id}> に {applicant.display_name} からマッチ申請が届きました。承認してください。"
    ch = interaction.channel
    sent = await ch.send(content, view=view) if ch else None
    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)

# -----------------------
# コマンド: 結果報告
# -----------------------
@bot.tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチング申請をお願いします。", ephemeral=True)
        return

    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    ch = interaction.channel
    sent_msg = await ch.send(content, view=ResultApproveView(winner.id, loser.id))
    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)

    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        if is_registered_match(winner.id, loser.id):
            await handle_approved_result(winner.id, loser.id, interaction.channel)
    bot.loop.create_task(auto_approve_task())

# -----------------------
# 管理者コマンド: ランキング表示
# -----------------------
@bot.tree.command(name="admin_show_ranking", description="管理者用: ランキング表示")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    ranking_list = []
    for i, (uid, data) in enumerate(sorted_users, 1):
        user = bot.get_user(uid)
        ranking_list.append(f"{i}. {user.display_name} - {data.get('pt',0)}pt" if user else f"{i}. Unknown - {data.get('pt',0)}pt")
    await interaction.response.send_message("\n".join(ranking_list) if ranking_list else "ユーザーがいません。", ephemeral=True)

# -----------------------
# 管理者コマンド: 全リセット
# -----------------------
@bot.tree.command(name="admin_reset_all", description="管理者用: 全ユーザー PT とロール初期化")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    for g in bot.guilds:
        for m in g.members:
            user_data[m.id] = {"pt":0}
            await update_member_display(m)
    await interaction.response.send_message("全ユーザーのPTとロールを初期化しました。", ephemeral=True)

# -----------------------
# 起動
# -----------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")

bot.run(TOKEN)
