import os
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks, commands
from datetime import datetime, timedelta, timezone

# ----------------------------------------
# 環境変数
# ----------------------------------------
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])

# JSTタイムゾーン
JST = timezone(timedelta(hours=+9))

# 自動承認までの秒数（15分）
AUTO_APPROVE_SECONDS = 15 * 60

# ----------------------------------------
# 内部データ
# ----------------------------------------
# user_id -> {"pt": int}
user_data = {}
# 現在マッチ中のプレイヤー組み合わせ
matching = {}

# ランク定義（表示用）
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

# 内部ランク階層（rank1..rank6）
rank_ranges_internal = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000),# 25+
}

# 敗北時の降格先
demotion_map = {
    3: 2, 4: 2,
    8: 7, 9: 7,
    13: 12, 14: 12,
    18: 17, 19: 17,
    23: 22, 24: 22
}

# ----------------------------------------
# ボット初期化
# ----------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ----------------------------------------
# ランク取得
# ----------------------------------------
def get_rank_info(pt: int):
    for start, end, role, icon in rank_roles:
        if start <= pt <= end:
            return role, icon
    return "Unknown", "❓"

def get_internal_rank(pt: int):
    for rank, rrange in rank_ranges_internal.items():
        if pt in rrange:
            return rank
    return 1

# ----------------------------------------
# PT計算
# ----------------------------------------
def calculate_pt(my_pt: int, opp_pt: int, result: str) -> int:
    my_rank = get_internal_rank(my_pt)
    opp_rank = get_internal_rank(opp_pt)
    delta = 0

    if result == "win":
        if my_rank == opp_rank:
            delta = 1
        elif my_rank + 1 == opp_rank:
            delta = 2
        elif my_rank + 2 == opp_rank:
            delta = 3
        elif my_rank - 1 == opp_rank:
            delta = 1
        elif my_rank - 2 == opp_rank:
            delta = 1
    elif result == "lose":
        if my_rank == opp_rank:
            delta = -1
        elif my_rank + 1 == opp_rank:
            delta = -1
        elif my_rank + 2 == opp_rank:
            delta = -1
        elif my_rank - 1 == opp_rank:
            delta = -2
        elif my_rank - 2 == opp_rank:
            delta = -3

    new_pt = my_pt + delta
    # 降格処理
    if new_pt in demotion_map and delta < 0:
        new_pt = demotion_map[new_pt]
    if new_pt < 0:
        new_pt = 0
    return new_pt

# ----------------------------------------
# メンバー表示更新（名前+PT+アイコン+ロール付与）
# ----------------------------------------
async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    role_name, icon = get_rank_info(pt)
    try:
        await member.edit(nick=f"{member.display_name.split(' ')[0]} {icon} {pt}pt")
        # ロール付与
        guild = member.guild
        # 既存ロール削除
        for r in rank_roles:
            role = discord.utils.get(guild.roles, name=r[2])
            if role and role in member.roles:
                await member.remove_roles(role)
        # 新しいロール追加
        new_role = discord.utils.get(guild.roles, name=role_name)
        if new_role:
            await member.add_roles(new_role)
    except Exception as e:
        print(f"Error updating {member}: {e}")

# ----------------------------------------
# マッチチェック
# ----------------------------------------
def is_registered_match(a: int, b: int):
    return matching.get(a) == b and matching.get(b) == a

# ----------------------------------------
# Views
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
        ch = interaction.guild.get_channel(self.origin_channel_id)
        if ch:
            await ch.send(f"<@{self.applicant_id}> と <@{self.opponent_id}> のマッチングが成立しました。試合後、勝者が結果報告してください。")
        await interaction.response.send_message("承認しました。", ephemeral=True)
        self.stop()

class CancelExistingMatchView(discord.ui.View):
    def __init__(self, existing_a:int, existing_b:int):
        super().__init__(timeout=60)
        self.existing_a = existing_a
        self.existing_b = existing_b

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        a, b = self.existing_a, self.existing_b
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
        judge_ch = interaction.guild.get_channel(RANKING_CHANNEL_ID)
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。@<@{ADMIN_ID}> に連絡してください。")
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)

# ----------------------------------------
# 勝者承認処理
# ----------------------------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    if not is_registered_match(winner_id, loser_id):
        await channel.send("このマッチングは登録されていません。")
        return
    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt  = user_data.get(loser_id, {}).get("pt", 0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new  = calculate_pt(loser_pt, winner_pt, "lose")
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new

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

# ----------------------------------------
# コマンド: マッチ申請
# ----------------------------------------
@bot.tree.command(name="マッチ申請", description="対戦相手にマッチ申請")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    if applicant.id in matching:
        existing_op = matching.get(applicant.id)
        view = CancelExistingMatchView(applicant.id, existing_op)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return
    if opponent.id in matching:
        existing_other = matching.get(opponent.id)
        view = CancelExistingMatchView(opponent.id, existing_other)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return

    applicant_pt = user_data.get(applicant.id, {}).get("pt", 0)
    opponent_pt = user_data.get(opponent.id, {}).get("pt", 0)

    if abs(get_internal_rank(applicant_pt) - get_internal_rank(opponent_pt)) >= 3:
        await interaction.response.send_message("ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return

    # チャレンジ中制約
    def challenge_match_ok(my_pt, other_pt):
        if my_pt in (3,4,8,9,13,14,18,19,23,24):
            return get_internal_rank(my_pt) == get_internal_rank(other_pt)
        return True

    if not challenge_match_ok(applicant_pt, opponent_pt):
        await interaction.response.send_message("昇級チャレンジ中のため、同rankの相手としかマッチできません。", ephemeral=True)
        return
    if not challenge_match_ok(opponent_pt, applicant_pt):
        await interaction.response.send_message(f"{opponent.display_name} は昇級チャレンジ中のため、この申請はできません。", ephemeral=True)
        return

    view = ApproveMatchView(applicant.id, opponent.id, interaction.channel.id)
    content = f"<@{opponent.id}> に {applicant.display_name} からマッチ申請が届きました。承認してください。"
    sent_msg = await interaction.channel.send(content, view=view)
    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)

# ----------------------------------------
# コマンド: 結果報告（勝者用）
# ----------------------------------------
@bot.tree.command(name="結果報告", description="勝者用：対戦結果を報告します")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。", ephemeral=True)
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
# ランキング表示
# ----------------------------------------
def standard_competition_ranking():
    # user_id -> pt
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    result = []
    prev_pt = None
    rank = 0
    display_rank = 0
    for uid, data in sorted_users:
        pt = data.get("pt",0)
        display_rank += 1
        if pt != prev_pt:
            rank = display_rank
            prev_pt = pt
        result.append((rank, uid, pt))
    return result

@bot.tree.command(name="ランキング", description="PT順にランキング表示")
async def cmd_ranking(interaction: discord.Interaction):
    rankings = standard_competition_ranking()
    lines = []
    for rank, uid, pt in rankings:
        role, icon = get_rank_info(pt)
        member = interaction.guild.get_member(uid)
        if member:
            # 🔽 ここで元名だけ抽出
            display_name = member.display_name
            words = display_name.split()
            # 「末尾2つ(アイコン + PTpt)」を取り除いて元名を再構成
            base_name = " ".join(words[:-2]) if len(words) > 2 else display_name

            lines.append(f"{rank}位 {base_name} {icon} {pt}pt")
    await interaction.response.send_message("🏆 ランキング\n" + "\n".join(lines))

# ----------------------------------------
# 管理コマンド
# ----------------------------------------
@bot.tree.command(name="admin_set_pt", description="指定ユーザーのPTを設定")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    user_data.setdefault(user.id, {})["pt"] = pt
    await update_member_display(user)
    await interaction.response.send_message(f"{user.display_name} のPTを {pt} に設定しました。", ephemeral=True)

@bot.tree.command(name="admin_reset_all", description="全ユーザーのPTを0にリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    for uid in user_data.keys():
        user_data[uid]["pt"] = 0
        member = interaction.guild.get_member(uid)
        if member:
            await update_member_display(member)
    await interaction.response.send_message("全ユーザーのPTを0にリセットしました。", ephemeral=True)

# ----------------------------------------
# 自動ランキング投稿タスク
# ----------------------------------------
@tasks.loop(minutes=60)
async def auto_post_ranking():
    now = datetime.now(JST)
    if now.hour in (14, 23) and now.minute == 0:
        guild = bot.get_guild(GUILD_ID)
        ch = guild.get_channel(RANKING_CHANNEL_ID)
        if ch:
            rankings = standard_competition_ranking()
            lines = []
            for rank, uid, pt in rankings:
                role, icon = get_rank_info(pt)
                member = guild.get_member(uid)
                if member:
                    lines.append(f"{rank}位 {member.display_name} {icon} {pt}pt")
            await ch.send("🏆 自動ランキング\n" + "\n".join(lines))

@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    await bot.tree.sync()
    auto_post_ranking.start()

bot.run(DISCORD_TOKEN)
