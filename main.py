import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

# -------------------------
# 環境変数
# -------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID", RANKING_CHANNEL_ID))
AUTO_APPROVE_SECONDS = 15 * 60  # 15分で自動承認

# -------------------------
# ランク定義（表示用）
# -------------------------
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
    1: range(0, 5),
    2: range(5, 10),
    3: range(10, 15),
    4: range(15, 20),
    5: range(20, 25),
    6: range(25, 10000),
}

# -------------------------
# データ保持
# -------------------------
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
matching = {}  # 現在マッチ中ユーザーID: 相手ユーザーID
user_data = {}  # user_id -> {"pt": int}

# -------------------------
# ランク判定・PT計算
# -------------------------
def get_internal_rank(pt: int) -> int:
    for r, rng in rank_ranges_internal.items():
        if pt in rng:
            return r
    return 1

def calculate_pt(user_pt: int, opponent_pt: int, result: str) -> int:
    diff = get_internal_rank(opponent_pt) - get_internal_rank(user_pt)
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
        else:
            delta = 0
    else:  # lose
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
        else:
            delta = 0
    new_pt = user_pt + delta

    # 例外処理（3,8,13,18,23 の超過分は切り捨て）
    for val in (3,8,13,18,23):
        if user_pt <= val <= new_pt:
            new_pt = val
    # 特殊敗北時の降格処理
    if result == "lose" and user_pt in (3,4,8,9,13,14,18,19):
        new_pt = user_pt - 1
    return max(new_pt,0)

def get_rank_info(pt: int):
    for start, end, role_name, icon in rank_roles:
        if start <= pt <= end:
            return role_name, icon
    return "Unknown", "❓"

# -------------------------
# ユーザー表示更新（名前・ロール）
# -------------------------
async def update_member_display(member: discord.Member):
    user_id = member.id
    pt = user_data.get(user_id, {}).get("pt", 0)
    rank_name, _ = get_rank_info(pt)
    # ロール付与/削除
    guild = member.guild
    for _, _, r_name, _ in rank_roles:
        role = discord.utils.get(guild.roles, name=r_name)
        if role:
            if r_name == rank_name:
                if role not in member.roles:
                    await member.add_roles(role)
            else:
                if role in member.roles:
                    await member.remove_roles(role)
    # ユーザー名変更（元の名前にPTを付与などは行わず純粋な名前）
    # Discord名変更は不要。ランキング表示で名前とPTを管理

# -------------------------
# マッチ承認・結果承認 Views
# -------------------------
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

# -------------------------
# 結果承認処理
# -------------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    if matching.get(winner_id) != loser_id or matching.get(loser_id) != winner_id:
        await channel.send("このマッチングは登録されていません。まずはマッチング申請をお願いします。")
        return
    winner_pt = user_data.get(winner_id, {}).get("pt",0)
    loser_pt = user_data.get(loser_id, {}).get("pt",0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new = calculate_pt(loser_pt, winner_pt, "lose")
    user_data.setdefault(winner_id,{})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new
    for g in bot.guilds:
        w_member = g.get_member(winner_id)
        l_member = g.get_member(loser_id)
        if w_member: await update_member_display(w_member)
        if l_member: await update_member_display(l_member)
    matching.pop(winner_id,None)
    matching.pop(loser_id,None)
    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

# -------------------------
# コマンド: マッチ申請
# -------------------------
@bot.tree.command(name="マッチ申請", description="対戦相手にマッチ申請を出します")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    if applicant.id in matching:
        view = CancelExistingMatchView(applicant.id, matching.get(applicant.id))
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return
    if opponent.id in matching:
        view = CancelExistingMatchView(opponent.id, matching.get(opponent.id))
        await interaction.response.send_message("相手は既にマッチ中です。取り消しますか？", view=view, ephemeral=True)
        return
    applicant_pt = user_data.get(applicant.id,{}).get("pt",0)
    opponent_pt = user_data.get(opponent.id,{}).get("pt",0)
    if abs(get_internal_rank(applicant_pt)-get_internal_rank(opponent_pt)) >= 3:
        await interaction.response.send_message("ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return
    # チャレンジ制約
    def challenge_ok(my_pt, other_pt):
        if my_pt in (3,8,13,18,23): return other_pt >= my_pt
        if my_pt in (4,9,14,19,24): return other_pt >= my_pt or other_pt == my_pt - 1
        return True
    if not challenge_ok(applicant_pt, opponent_pt):
        await interaction.response.send_message("昇級チャレンジ中のため、この相手とはマッチできません。", ephemeral=True)
        return
    if not challenge_ok(opponent_pt, applicant_pt):
        await interaction.response.send_message(f"{opponent.display_name} は昇級チャレンジ中のため、この申請はできません。", ephemeral=True)
        return
    view = ApproveMatchView(applicant.id, opponent.id, interaction.channel.id)
    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", view=view, ephemeral=False)

# -------------------------
# コマンド: 結果報告
# -------------------------
@bot.tree.command(name="結果報告", description="勝者が実行。敗者を指定")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    if matching.get(winner.id) != loser.id or matching.get(loser.id) != winner.id:
        await interaction.response.send_message("このマッチングは登録されていません。", ephemeral=True)
        return
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？"
    await interaction.channel.send(content, view=ResultApproveView(winner.id, loser.id))
    await interaction.response.send_message("結果報告を受け付けました。", ephemeral=True)

    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        if matching.get(winner.id) == loser.id and matching.get(loser.id) == winner.id:
            await handle_approved_result(winner.id, loser.id, interaction.channel)
    bot.loop.create_task(auto_approve_task())

# -------------------------
# コマンド: ランキング表示
# -------------------------
@bot.tree.command(name="admin_show_ranking", description="ランキング表示")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    ranking_list = []
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    for idx, (uid, data) in enumerate(sorted_users, 1):
        member = interaction.guild.get_member(uid)
        if member:
            ranking_list.append(f"{idx}. {member.display_name} - {data.get('pt',0)}pt")
    if ranking_list:
        await interaction.response.send_message("\n".join(ranking_list), ephemeral=False)
    else:
        await interaction.response.send_message("まだユーザーが登録されていません。", ephemeral=True)

# -------------------------
# コマンド: /admin_reset_all
# -------------------------
@bot.tree.command(name="admin_reset_all", description="全ユーザーPTをリセット")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    for user_id in user_data:
        user_data[user_id]["pt"] = 0
        for g in bot.guilds:
            member = g.get_member(user_id)
            if member:
                await update_member_display(member)
    await interaction.response.send_message("全ユーザーのPTを初期化しました。", ephemeral=True)

# -------------------------
# コマンド: /admin_set_pt
# -------------------------
@bot.tree.command(name="admin_set_pt", description="任意ユーザーのPTを設定")
@app_commands.describe(target="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    user_data.setdefault(target.id, {})["pt"] = pt
    await update_member_display(target)
    await interaction.response.send_message(f"{target.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# -------------------------
# 起動
# -------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready.")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Error syncing commands: {e}")

bot.run(TOKEN)
