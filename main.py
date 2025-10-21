# main.py
import os
import re
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

# ------------------------
# 環境変数（RailwayのVariables等に登録）
# ------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID", RANKING_CHANNEL_ID))

# 自動承認（秒）: 15分
AUTO_APPROVE_SECONDS = 15 * 60

# ------------------------
# ランク定義（表示用）
# (start_pt, end_pt, role_name, icon_for_display)
# ------------------------
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

# ------------------------
# Bot 初期化
# ------------------------
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = False  # 通常不要（privileged）
bot = commands.Bot(command_prefix="/", intents=intents)

# ------------------------
# データ（メモリ）
# - user_data: user_id -> {"pt": int}
# - matching: user_id -> opponent_id  (双方向で保存)
# ------------------------
user_data: dict[int, dict] = {}
matching: dict[int, int] = {}

# ------------------------
# ユーティリティ関数
# ------------------------
def get_rank_info(pt: int):
    for s, e, name, icon in rank_roles:
        if s <= pt <= e:
            return name, icon
    return "Unknown", "❓"

def get_internal_rank(pt: int) -> int:
    for r, rng in rank_ranges_internal.items():
        if pt in rng:
            return r
    return 1

def calculate_pt(user_pt: int, opp_pt: int, result: str) -> int:
    """
    result: "win" or "lose"
    Implements:
    - rank-diff mapping:
      same rank: win +1 / lose -1
      +1 rank opponent: win +2 / lose -1
      +2 rank opponent: win +3 / lose -1
      -1 rank opponent: win +1 / lose -2
      -2 rank opponent: win +1 / lose -3
    - Exceptions:
      * when increasing would exceed thresholds (3,8,13,18,23) -> cut to that threshold
      * special loss drop: (3,4)->2, (8,9)->7, (13,14)->12, (18,19)->17, (23,24)->22
    """
    my_rank = get_internal_rank(user_pt)
    opp_rank = get_internal_rank(opp_pt)
    diff = opp_rank - my_rank

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

    # 超過切り捨てルール（3,8,13,18,23）
    for thr in (3, 8, 13, 18, 23):
        if new_pt > thr and user_pt <= thr:
            # If previously below or equal thr and new_pt would exceed thr -> set to thr
            new_pt = thr

    # 敗北時の強制降格（指定）
    if result == "lose":
        if user_pt in (3,4):
            new_pt = 2
        elif user_pt in (8,9):
            new_pt = 7
        elif user_pt in (13,14):
            new_pt = 12
        elif user_pt in (18,19):
            new_pt = 17
        elif user_pt in (23,24):
            new_pt = 22

    return max(new_pt, 0)

# 抜けや重複した末尾の " {icon} {pt}pt" を取り除く
_suffix_regex = re.compile(r'\s[^\s]+\s\d+pt$')
def build_display_nick(original_name: str, icon: str, pt: int) -> str:
    # remove existing suffix if present
    clean = _suffix_regex.sub('', original_name)
    return f"{clean} {icon} {pt}pt"

# ------------------------
# 表示更新（ロール & ニックネーム）
# - nickname を更新します（try/except: 権限がない場合はスキップ）
# - 役割は rank_roles の role_name に合わせて付け外し
# ------------------------
async def update_member_display(member: discord.Member):
    uid = member.id
    pt = user_data.get(uid, {}).get("pt", 0)
    role_name, icon = get_rank_info(pt)

    # Update roles: ensure exactly the correct rank role (if exists) is present
    guild = member.guild
    # Remove any rank_roles roles that the member shouldn't have
    try:
        # Remove incorrect rank roles
        for _, _, rname, _ in rank_roles:
            role = discord.utils.get(guild.roles, name=rname)
            if role and role in member.roles and rname != role_name:
                try:
                    await member.remove_roles(role, reason="Rank sync by bot")
                except Exception:
                    pass
        # Add correct role if present and not already assigned
        desired_role = discord.utils.get(guild.roles, name=role_name)
        if desired_role and desired_role not in member.roles:
            try:
                await member.add_roles(desired_role, reason="Rank sync by bot")
            except Exception:
                pass
    except Exception:
        # Roles operations can fail due to permissions; ignore but continue
        pass

    # Update nickname to include icon and pt
    try:
        # Use member.name (account username) rather than display_name to derive base
        base = member.display_name  # keep current display name but strip trailing pattern if any
        new_nick = build_display_nick(base, icon, pt)
        # Only change if different
        if member.display_name != new_nick:
            try:
                await member.edit(nick=new_nick, reason="Update rank display")
            except Exception:
                # If nickname change not permitted, ignore silently
                pass
    except Exception:
        pass

# ------------------------
# マッチングルール（チャレンジ制約）
# - According to final rules:
#   - If my_pt in (3,8,13,18,23) then only allow opponents with other_pt >= my_pt
#   - If my_pt in (4,9,14,19,24) then allow opponents with other_pt >= my_pt OR other_pt == my_pt-1
#   - Otherwise OK
# ------------------------
def challenge_match_ok(my_pt: int, other_pt: int) -> bool:
    if my_pt in (3, 8, 13, 18, 23):
        return other_pt >= my_pt
    if my_pt in (4, 9, 14, 19, 24):
        return (other_pt >= my_pt) or (other_pt == my_pt - 1)
    return True

# ------------------------
# マッチ登録チェック
# ------------------------
def is_registered_match(a_id: int, b_id: int) -> bool:
    return matching.get(a_id) == b_id and matching.get(b_id) == a_id

# ------------------------
# Views: Approve / Cancel / Result Approve
# ------------------------
class ApproveMatchView(discord.ui.View):
    def __init__(self, applicant_id:int, opponent_id:int, origin_channel_id:int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.opponent_id = opponent_id
        self.origin_channel_id = origin_channel_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only opponent can approve
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("承認できるのは申請された相手のみです。", ephemeral=True)
            return

        # finalize matching (store bidirectional)
        matching[self.applicant_id] = self.opponent_id
        matching[self.opponent_id] = self.applicant_id

        # announce in origin channel
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
        # Only requester or admin can cancel? We'll allow the click by either (interaction user control handled upstream)
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
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。<@{ADMIN_ID}> に連絡してください。")
        # remove matching; admin will handle via judge channel
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)

# ------------------------
# 実際の結果反映処理
# ------------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    # verify matching exists
    if not is_registered_match(winner_id, loser_id):
        await channel.send("このマッチングは登録されていません。まずはマッチング申請をお願いします。")
        return

    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt = user_data.get(loser_id, {}).get("pt", 0)

    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new = calculate_pt(loser_pt, winner_pt, "lose")

    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new

    # update members display & roles across guilds
    for g in bot.guilds:
        w_mem = g.get_member(winner_id)
        l_mem = g.get_member(loser_id)
        if w_mem:
            await update_member_display(w_mem)
        if l_mem:
            await update_member_display(l_mem)

    # clear matching
    matching.pop(winner_id, None)
    matching.pop(loser_id, None)

    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

# ------------------------
# コマンド: マッチ申請
# - 投稿は申請したチャンネルへ（DMは使わない）
# - 既にどちらかがマッチ中なら取り消しの選択肢を表示
# ------------------------
@bot.tree.command(name="マッチ申請", description="対戦相手にマッチ申請を出します")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    channel = interaction.channel

    # self check
    if applicant.id == opponent.id:
        await interaction.response.send_message("自分自身には申請できません。", ephemeral=True)
        return

    # if applicant already matched
    if applicant.id in matching:
        existing_op = matching.get(applicant.id)
        view = CancelExistingMatchView(applicant.id, existing_op)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return

    # if opponent already matched
    if opponent.id in matching:
        existing_op = matching.get(opponent.id)
        view = CancelExistingMatchView(opponent.id, existing_op)
        await interaction.response.send_message("相手は既にマッチ中です。取り消しますか？", view=view, ephemeral=True)
        return

    # rank gap check (internal rank)
    applicant_pt = user_data.get(applicant.id, {}).get("pt", 0)
    opponent_pt = user_data.get(opponent.id, {}).get("pt", 0)
    if abs(get_internal_rank(applicant_pt) - get_internal_rank(opponent_pt)) >= 3:
        await interaction.response.send_message("申し訳ありません。ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return

    # challenge constraints (pt-based rules)
    if not challenge_match_ok(applicant_pt, opponent_pt):
        await interaction.response.send_message("昇級チャレンジ中のため、この相手とはマッチできません。", ephemeral=True)
        return
    if not challenge_match_ok(opponent_pt, applicant_pt):
        await interaction.response.send_message(f"{opponent.display_name} は昇級チャレンジ中のため、この申請はできません。", ephemeral=True)
        return

    # send public request into the same channel (no DM)
    view = ApproveMatchView(applicant.id, opponent.id, channel.id if channel else None)
    content = f"<@{opponent.id}> に <@{applicant.id}> からマッチ申請が届きました。承認してください。"
    # Post the message publicly in the channel with the approve button
    if channel:
        await channel.send(content, view=view)
        await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)
    else:
        await interaction.response.send_message("チャンネル情報が取得できません。", ephemeral=True)

# ------------------------
# コマンド: 結果報告（勝者が実行）
# - 敗者の承認を待ち、15分で自動承認
# ------------------------
@bot.tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    channel = interaction.channel

    # must be registered match
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチング申請をお願いします。", ephemeral=True)
        return

    # Post approval view in same channel (no DM)
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？"
    if channel:
        await channel.send(content, view=ResultApproveView(winner.id, loser.id))
        await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)
    else:
        await interaction.response.send_message("チャンネルが取得できません。", ephemeral=True)
        return

    # schedule auto-approve after AUTO_APPROVE_SECONDS
    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        if is_registered_match(winner.id, loser.id):
            # If still registered, auto-apply
            await handle_approved_result(winner.id, loser.id, channel)
    bot.loop.create_task(auto_approve_task())

# ------------------------
# 管理者コマンド: ランキング表示（純ユーザー名のみ）
# ------------------------
@bot.tree.command(name="admin_show_ranking", description="管理者用: ランキング表示（順位付き）")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    # sort by pt desc
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt", 0), reverse=True)
    lines = []
    for i, (uid, data) in enumerate(sorted_users, start=1):
        # use account username (member.name) to avoid duplicated nick suffix
        member = interaction.guild.get_member(uid)
        name = member.name if member else f"Unknown({uid})"
        lines.append(f"{i}. {name} - {data.get('pt',0)}pt")
    if not lines:
        await interaction.response.send_message("まだユーザーが登録されていません。", ephemeral=True)
    else:
        # Post publicly to the ranking channel as well as respond
        ranking_text = "\n".join(lines)
        # respond ephemeral to admin and also send to ranking channel publicly
        await interaction.response.send_message("ランキングを投稿しました（管理者にのみ表示）", ephemeral=True)
        try:
            ch = bot.get_channel(RANKING_CHANNEL_ID)
            if ch:
                await ch.send("**ランキング**\n" + ranking_text)
        except Exception:
            pass

# ------------------------
# 管理者コマンド: /admin_reset_all
# ------------------------
@bot.tree.command(name="admin_reset_all", description="管理者用: 全ユーザーのPT/表示を初期化")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    # reset memory
    for g in bot.guilds:
        for m in g.members:
            if m.bot:
                continue
            user_data.setdefault(m.id, {})["pt"] = 0
            # update display & roles
            try:
                await update_member_display(m)
            except Exception:
                pass
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

# ------------------------
# 管理者コマンド: /admin_set_pt
# ------------------------
@bot.tree.command(name="admin_set_pt", description="管理者用: 指定ユーザーのPTを設定（ロール・表示自動反映）")
@app_commands.describe(target="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    user_data.setdefault(target.id, {})["pt"] = max(0, int(pt))
    # reflect immediately
    try:
        await update_member_display(target)
    except Exception:
        pass
    await interaction.response.send_message(f"{target.name} のPTを {pt} に設定しました。", ephemeral=True)

# ------------------------
# 起動時処理
# ------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    # sync commands to guild only (speeds up)
    try:
        guild = discord.Object(id=GUILD_ID)
        await bot.tree.sync(guild=guild)
        print("Commands synced to guild.")
    except Exception:
        try:
            await bot.tree.sync()
            print("Commands synced globally.")
        except Exception as e:
            print("Command sync failed:", e)

# ------------------------
# Run
# ------------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
