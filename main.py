# main.py
import os
import asyncio
import random
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

# ---------- ログ ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("matchbot")

# ---------- 環境変数 ----------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
# optional channels
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

# 自動承認タイム（敗者承認待ちの時間）
AUTO_APPROVE_SECONDS = 15 * 60  # 15分

# /マッチ希望 の保持時間
MATCH_WISH_TTL = 5 * 60  # 5分

# 抽選待機（短時間）: 5秒 に設定（仕様に合わせて）
DRAW_WAIT_SECONDS = 5

# 勝者申告後の敗者承認ボタンの有効期限（5分）
RESULT_APPROVE_TIMEOUT = 5 * 60

# ---------- Bot セットアップ ----------
intents = discord.Intents.default()
intents.members = True  # ユーザ情報が必要（ロール付与・ニック変更）
intents.message_content = False  # 不要
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---------- データ構造（メモリ内） ----------
# user_data: user_id -> {"pt": int}
user_data: dict[int, dict] = {}

# matching: user_id -> opponent_id  (for established matches)
matching: dict[int, int] = {}

# pending_applications: applicant_id -> (opponent_id, origin_channel_id, msg_id)
# For direct match requests (not random); kept for compatibility (but Random variant uses /マッチ希望)
pending_applications: dict[int, dict] = {}

# hope_list: user_id -> timestamp (when they issued /マッチ希望)
hope_list: dict[int, float] = {}

# For tracking per-user TTL removal tasks (so we can cancel when user withdraws)
hope_timers: dict[int, asyncio.Task] = {}

# draw_in_progress flag and draw lock to prevent concurrent draws
draw_lock = asyncio.Lock()
draw_task: asyncio.Task | None = None

# result_pending: (winner_id, loser_id) -> {"message": msg, "task": auto_approve_task}
result_pending: dict[tuple[int, int], dict] = {}

# role mapping (for display icon and role name)
# Note: Random variant uses simplified rank (no challenge). Icons used for nickname formatting.
rank_definitions = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, 99999, "Challenger", "😈"),
]

# internal rank ranges for rank-difference logic
internal_rank_ranges = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000), # 25+
}

# ---------- ユーティリティ関数 ----------

def get_user_pt(user_id: int) -> int:
    return user_data.get(user_id, {}).get("pt", 0)

def set_user_pt(user_id: int, pt: int):
    if pt < 0:
        pt = 0
    user_data.setdefault(user_id, {})["pt"] = pt

def get_rank_info_by_pt(pt: int):
    for start, end, role_name, icon in rank_definitions:
        if isinstance(start, int) and isinstance(end, int):
            if pt >= start and pt <= end:
                return role_name, icon
    # fallback
    return "Beginner", "🔰"

def get_role_for_pt(guild: discord.Guild, pt: int):
    role_name, _ = get_rank_info_by_pt(pt)
    return discord.utils.get(guild.roles, name=role_name)

def get_icon_for_pt(pt: int) -> str:
    _, icon = get_rank_info_by_pt(pt)
    return icon

def get_internal_rank(pt: int) -> int:
    for r, rng in internal_rank_ranges.items():
        if pt in rng:
            return r
    return 1

# PT計算（ランク差による計算を internal_rank 差で扱う）
def calculate_pt_change(winner_pt: int, loser_pt: int):
    # internal ranks
    wr = get_internal_rank(winner_pt)
    lr = get_internal_rank(loser_pt)
    diff = lr - wr  # positive if loser is higher internal rank than winner
    # But we need to compute by perspective of winner vs loser
    # Use the rules:
    # same rank: winner +1, loser -1
    # 1 rank up opponent: winner +2, loser -1
    # 2 rank up opponent: winner +3, loser -1
    # 1 rank down opponent: winner +1, loser -2
    # 2 rank down opponent: winner +1, loser -3
    rank_diff = get_internal_rank(loser_pt) - get_internal_rank(winner_pt)
    # if loser is higher internal rank than winner: rank_diff > 0
    if rank_diff == 0:
        w_delta = 1
        l_delta = -1
    elif rank_diff == 1:
        w_delta = 2
        l_delta = -1
    elif rank_diff == 2:
        w_delta = 3
        l_delta = -1
    elif rank_diff == -1:
        w_delta = 1
        l_delta = -2
    elif rank_diff == -2:
        w_delta = 1
        l_delta = -3
    else:
        # rank diff >=3 or <= -3 shouldn't have been allowed to match
        w_delta = 1
        l_delta = -1
    return w_delta, l_delta

def apply_rank_edge_cases_after_loss(pt_before: int) -> int:
    # handle downgrade when the lost pt is in special ranges (3,4 -> 2 etc)
    # per spec exceptions (for Random variant we kept same exceptions as main):
    if pt_before in (3,4):
        return 2
    if pt_before in (8,9):
        return 7
    if pt_before in (13,14):
        return 12
    if pt_before in (18,19):
        return 17
    if pt_before in (23,24):
        return 22
    # else return current pt (they will be adjusted by deltas)
    return None

# nickname formatting: original_name (if possible) + " {icon} {pt}pt"
def compose_display_nick(base_name: str, icon: str, pt: int):
    return f"{base_name} {icon} {pt}pt"

async def safe_update_nick(member: discord.Member, new_nick: str):
    try:
        # If the bot can't change nick (e.g., target is owner/higher role), ignore gracefully
        await member.edit(nick=new_nick)
    except Exception as e:
        logger.debug(f"Failed to update nick for {member}: {e}")

async def update_member_display(guild: discord.Guild, user_id: int):
    member = guild.get_member(user_id)
    if not member:
        return
    pt = get_user_pt(user_id)
    icon = get_icon_for_pt(pt)
    # prefer original username without previous appended tag: strip trailing " {icon} {pt}pt" patterns
    base = member.name
    # If member.nick exists and includes pattern, remove it
    name_to_use = member.name
    # Compose new nick
    new_nick = compose_display_nick(name_to_use, icon, pt)
    # Limit nick length to 32
    if len(new_nick) > 32:
        new_nick = new_nick[:32]
    await safe_update_nick(member, new_nick)
    # Update roles: remove other rank roles, add correct one
    role_for_pt = get_role_for_pt(guild, pt)
    if role_for_pt:
        try:
            # Remove all rank roles then add the correct one
            rank_role_names = [r[2] for r in rank_definitions]
            to_remove = [r for r in guild.roles if r.name in rank_role_names and r in member.roles and r != role_for_pt]
            if to_remove:
                await member.remove_roles(*to_remove, reason="Rank sync")
            if role_for_pt not in member.roles:
                await member.add_roles(role_for_pt, reason="Rank sync")
        except Exception as e:
            logger.debug(f"Failed to update roles for {member}: {e}")

# ---------- マッチ/抽選ロジック ----------

async def start_draw_if_needed(guild: discord.Guild, origin_channel: discord.abc.Messageable):
    """
    Called when hope_list changes. If enough participants exist and no draw in progress,
    start a draw that waits DRAW_WAIT_SECONDS then attempts to randomly pair entries respecting rank constraints.
    """
    global draw_task
    async with draw_lock:
        if draw_task and not draw_task.done():
            return  # already drawing
        # if fewer than 2 participants, nothing to do
        alive = list(hope_list.keys())
        if len(alive) < 2:
            return
        # launch draw task
        draw_task = asyncio.create_task(draw_and_pair(guild, origin_channel))

async def draw_and_pair(guild: discord.Guild, origin_channel: discord.abc.Messageable):
    """
    Wait DRAW_WAIT_SECONDS allowing more joiners, then attempt to pair randomly.
    Respect rank-difference constraint: internal rank difference must be < 3.
    Matched pairs are removed from hope_list and added to matching.
    Matched users are notified publicly in the channel where their /マッチ希望 was issued (origin_channel).
    """
    await asyncio.sleep(DRAW_WAIT_SECONDS)
    # snapshot of current hope_list keys (note: hope_list entries may change concurrently)
    participants = list(hope_list.keys())
    random.shuffle(participants)
    paired = set()
    pairs = []
    # greedy: try to pair in random order with first compatible partner
    for i, a in enumerate(participants):
        if a in paired:
            continue
        for b in participants[i+1:]:
            if b in paired:
                continue
            # check internal rank diff
            ar = get_internal_rank(get_user_pt(a))
            br = get_internal_rank(get_user_pt(b))
            if abs(ar - br) >= 3:
                continue
            # also check special-challenge-like restrictions on pt (the limited-match states)
            a_pt = get_user_pt(a)
            b_pt = get_user_pt(b)
            def challenge_ok(my_pt, other_pt):
                if my_pt in (3,8,13,18,23):
                    return other_pt >= my_pt
                if my_pt in (4,9,14,19,24):
                    return (other_pt >= my_pt) or (other_pt == my_pt - 1)
                return True
            if not challenge_ok(a_pt, b_pt) or not challenge_ok(b_pt, a_pt):
                continue
            # pair them
            paired.add(a); paired.add(b)
            pairs.append((a, b))
            break
    # For each pair, register matching and notify both
    for a, b in pairs:
        # remove from hope_list and cancel timers
        hope_list.pop(a, None)
        t = hope_timers.pop(a, None)
        if t and not t.done():
            t.cancel()
        hope_list.pop(b, None)
        t = hope_timers.pop(b, None)
        if t and not t.done():
            t.cancel()
        # register matching (both directions)
        matching[a] = b
        matching[b] = a
        # notify publicly in origin_channel
        try:
            await origin_channel.send(f"<@{a}> と <@{b}> のマッチングが成立しました。試合後、勝者が /結果報告 を行なってください。")
        except Exception:
            logger.debug("Failed to send match-formed message to origin channel.")
    # leftover participants are kept in hope_list (per spec), so do nothing else
    return

async def schedule_remove_wish(user_id: int):
    """
    Called when a user issues /マッチ希望. Wait MATCH_WISH_TTL and then remove them if still present.
    We keep a handle so we can cancel if they withdraw earlier or are matched.
    """
    await asyncio.sleep(MATCH_WISH_TTL)
    if user_id in hope_list:
        hope_list.pop(user_id, None)
    # clear timer entry
    hope_timers.pop(user_id, None)

# ---------- Views (Buttons) ----------

class ApproveMatchView(discord.ui.View):
    """
    For manual direct match request: allows the target to approve.
    Only the target can press Approve.
    """
    def __init__(self, applicant_id:int, opponent_id:int, origin_channel_id:int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.opponent_id = opponent_id
        self.origin_channel_id = origin_channel_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        # only the opponent can approve
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("承認できるのは申請された相手のみです。", ephemeral=True)
            return
        # make matching
        matching[self.applicant_id] = self.opponent_id
        matching[self.opponent_id] = self.applicant_id
        guild = interaction.guild
        ch = guild.get_channel(self.origin_channel_id) if self.origin_channel_id else interaction.channel
        if ch:
            await ch.send(f"<@{self.applicant_id}> と <@{self.opponent_id}> のマッチングが成立しました。試合後、勝者が /結果報告 を行なってください。")
        await interaction.response.send_message("承認しました。", ephemeral=True)
        self.stop()

class CancelExistingMatchView(discord.ui.View):
    """
    If someone tries to apply to a user who is already in a match, prompt to cancel existing match.
    Only applicant (the one seeing the prompt) can press the cancel button to cancel existing match pair.
    """
    def __init__(self, existing_a:int, existing_b:int):
        super().__init__(timeout=60)
        self.existing_a = existing_a
        self.existing_b = existing_b

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # allow the one who sees it (the applicant) to cancel
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
    """
    View for loser to approve or dispute the result. Only the loser can interact.
    """
    def __init__(self, winner_id:int, loser_id:int):
        super().__init__(timeout=RESULT_APPROVE_TIMEOUT)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.processed = False

    async def on_timeout(self):
        # auto-approve if not processed
        if not self.processed:
            key = (self.winner_id, self.loser_id)
            if key in result_pending:
                info = result_pending.pop(key, None)
                # call approved handler (channel from info)
                channel = info.get("channel")
                try:
                    await handle_approved_result(self.winner_id, self.loser_id, channel)
                except Exception:
                    logger.exception("auto-approve failed")

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
        # notify judge channel
        guild = interaction.guild
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID) if JUDGE_CHANNEL_ID else None
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。\nこのマッチングは無効扱いとなっています。審議結果を <@{ADMIN_ID}> にご報告ください。")
        # remove matching
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)
        # clear pending
        result_pending.pop((self.winner_id, self.loser_id), None)

# ---------- 結果処理 ----------

def is_registered_match(a: int, b: int) -> bool:
    return matching.get(a) == b and matching.get(b) == a

async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    # validate registration
    if not is_registered_match(winner_id, loser_id):
        await channel.send("このマッチングは登録されていません。まずはマッチング申請をお願いします。")
        return

    winner_pt_before = get_user_pt(winner_id)
    loser_pt_before = get_user_pt(loser_id)
    w_delta, l_delta = calculate_pt_change(winner_pt_before, loser_pt_before)

    # apply loss exception downgrade if losing pt is one of special cases BEFORE applying delta?
    # The spec says: on loss, revert to specific values; but general logic applied as per spec: implement loss downgrade override.
    # We'll first compute provisional loser_pt_after = loser_pt_before + l_delta, then if loser_pt_before in exception set, set to provided floor.
    provisional_loser_after = loser_pt_before + l_delta
    # check exception
    special = apply_rank_edge_cases_after_loss(provisional_loser_after)
    if special is not None:
        loser_new = special
    else:
        loser_new = provisional_loser_after
        if loser_new < 0:
            loser_new = 0

    winner_new = winner_pt_before + w_delta
    if winner_new < 0:
        winner_new = 0

    # write back
    set_user_pt(winner_id, winner_new)
    set_user_pt(loser_id, loser_new)

    # reflect changes to guild members (all guilds bot is in)
    for g in bot.guilds:
        await update_member_display(g, winner_id)
        await update_member_display(g, loser_id)

    # remove matching
    matching.pop(winner_id, None)
    matching.pop(loser_id, None)
    # remove pending result
    result_pending.pop((winner_id, loser_id), None)

    # send result message
    delta_w = winner_new - winner_pt_before
    delta_l = loser_new - loser_pt_before
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

# ---------- コマンド群 ----------

@tree.command(name="マッチ希望", description="ランダムマッチにエントリーします。相手指定は不要です。")
async def cmd_match_wish(interaction: discord.Interaction):
    user = interaction.user
    uid = user.id
    # if already in match, cannot enter
    if uid in matching:
        opp = matching[uid]
        await interaction.response.send_message(f"<@{opp}> との試合が既に成立中です。試合を終えてから再度申請してください。", ephemeral=True)
        return
    # if already in hope list
    if uid in hope_list:
        await interaction.response.send_message("既にマッチ希望が出ています。", ephemeral=True)
        return
    # add to hope_list
    hope_list[uid] = asyncio.get_event_loop().time()
    # schedule removal after TTL
    t = asyncio.create_task(schedule_remove_wish(uid))
    hope_timers[uid] = t
    # if there's at least one other waiting, start draw
    # notify only the user
    await interaction.response.send_message("マッチング希望を受け付けました。 マッチングが成立した場合、そのチャンネルにて通知されます。", ephemeral=True)
    # start draw on the channel where command executed
    await start_draw_if_needed(interaction.guild, interaction.channel)

@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます（自分のみ有効）。")
async def cmd_cancel_wish(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in hope_list:
        await interaction.response.send_message("あなたのマッチ希望は見つかりません。", ephemeral=True)
        return
    # cancel timer
    hope_list.pop(uid, None)
    t = hope_timers.pop(uid, None)
    if t and not t.done():
        t.cancel()
    await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)

@tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします。", ephemeral=True)
        return
    # send approval view to channel (no DM)
    view = ResultApproveView(winner.id, loser.id)
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    sent = await interaction.channel.send(content, view=view)
    # place entry in result_pending with auto-approve timer
    # We'll store channel so auto-approve can call handle_approved_result
    key = (winner.id, loser.id)
    # store info
    result_pending[key] = {"message": sent, "channel": interaction.channel}
    # immediate feedback to reporter
    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)
    # Start an auto-approve task: handled by ResultApproveView on_timeout using timeout above, so nothing more to do here.

@tree.command(name="ランキング", description="現在のランキングを表示します（全ユーザー使用可）。")
async def cmd_ranking(interaction: discord.Interaction):
    # Build ranking: sort by pt desc. Use standard competition ranking for ties.
    items = [(uid, user_data.get(uid, {}).get("pt", 0)) for uid in user_data.keys()]
    # ensure all guild members are included as needed
    # also include members in guild who might not yet be in user_data (default pt 0)
    guild = interaction.guild
    for m in guild.members:
        if m.bot:
            continue
        if m.id not in [x[0] for x in items]:
            items.append((m.id, get_user_pt(m.id)))
    # sort
    items.sort(key=lambda x: x[1], reverse=True)
    # standard competition ranking (1,2,2,4)
    ranking_lines = []
    last_score = None
    rank = 0
    display_rank = 0
    for uid, pt in items:
        rank += 1
        if pt != last_score:
            display_rank = rank
            last_score = pt
        # display simple username (use member.name to avoid duplicated icons)
        member = guild.get_member(uid)
        name = member.name if member else str(uid)
        role_name, icon = get_rank_info_by_pt(pt)
        ranking_lines.append(f"{display_rank}位 {name} {icon} {pt}pt")
    if not ranking_lines:
        await interaction.response.send_message("ランキングデータがありません。", ephemeral=True)
        return
    # send ephemeral to invoker with full list
    await interaction.response.send_message("\n".join(ranking_lines), ephemeral=True)

# ---------- 管理者コマンド群 (ADMIN_ID only) ----------

def is_admin(user: discord.User) -> bool:
    return user.id == ADMIN_ID

@tree.command(name="admin_set_pt", description="管理者: 指定ユーザーのPTを設定します（管理者のみ）。")
@app_commands.describe(target="対象のメンバー", pt="設定するpt（整数）")
async def cmd_admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return
    if pt < 0:
        pt = 0
    set_user_pt(target.id, pt)
    # update display and roles across guilds
    for g in bot.guilds:
        await update_member_display(g, target.id)
    await interaction.response.send_message(f"{target.display_name} のPTを {pt} に設定しました。", ephemeral=True)

@tree.command(name="admin_reset_all", description="管理者: 全ユーザーのPTと表示を初期化します（管理者のみ）。")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return
    user_data.clear()
    # reset nick/roles for all members in guilds
    for g in bot.guilds:
        for m in g.members:
            if m.bot:
                continue
            try:
                await safe_update_nick(m, None)
            except Exception:
                pass
            # remove rank roles
            try:
                rank_role_names = [r[2] for r in rank_definitions]
                to_remove = [r for r in g.roles if r.name in rank_role_names and r in m.roles]
                if to_remove:
                    await m.remove_roles(*to_remove, reason="Admin reset")
            except Exception:
                pass
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

# ---------- Lifecycle: on_ready and command sync ----------

@bot.event
async def on_ready():
    logger.info(f"{datetime.utcnow().isoformat()} - {bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    # Sync commands to the specific guild for faster updates during development
    try:
        guild = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild)
        logger.info("Commands synced to guild.")
    except Exception as e:
        logger.exception("Failed to sync commands to guild: %s", e)

# ---------- Run ----------

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.exception("Bot raised an exception on run: %s", e)
