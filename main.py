# main.py
import os
import asyncio
import random
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

# -----------------------
# Configuration / Env
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])

# Timing constants
HOPE_TTL_SECONDS = 5 * 60        # /マッチ希望 の期限（5分）
DRAW_WAIT_SECONDS = 5            # 抽選待機時間（最後の参加からの秒数、ユーザー指定と同意）
RESULT_APPROVE_SECONDS = 15 * 60 # 敗者承認待ち（15分 -> 自動承認）

# -----------------------
# Rank definitions
# -----------------------
# 表示用（絵文字含む）
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 99999, "Challenger", "😈"),
]

# 内部ランク（rank1..rank6） : マッチ判定とpt増減ロジック簡略化用
internal_rank_map = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 100000),
}

def get_display_role(pt: int):
    for low, high, name, emoji in rank_roles:
        if low <= pt <= high:
            return name, emoji
    return "Beginner", "🔰"

def get_internal_rank(pt: int) -> int:
    for k, r in internal_rank_map.items():
        if pt in r:
            return k
    return 1

# -----------------------
# Bot setup
# -----------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = False

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# -----------------------
# In-memory storage
# -----------------------
# players: user_id -> {"pt": int, "updated": datetime}
players = {}

# match queues and state:
# hope_list: user_id -> timestamp (when they issued /マッチ希望)
hope_list = {}        # participants who have active hope (for 5min)
# drawing set: users currently in the pending draw pool
drawing_pool = set()
# in_match: user_id -> opponent_id (both directions)
in_match = {}

# draw task controller
_draw_task = None
_draw_task_lock = asyncio.Lock()

# pending result approvals: tuple (winner_id, loser_id) -> { "message": Message, "expires_at": datetime, "task": task }
pending_results = {}

# -----------------------
# Helper functions
# -----------------------
def now_utc():
    return datetime.utcnow()

def clean_expired_hopes():
    """ Remove expired hope_list entries older than HOPE_TTL_SECONDS """
    cutoff = now_utc() - timedelta(seconds=HOPE_TTL_SECONDS)
    expired = [uid for uid, ts in hope_list.items() if ts < cutoff]
    for uid in expired:
        hope_list.pop(uid, None)
        drawing_pool.discard(uid)

def standard_competition_ranking():
    """Return list of (rank_number, user_id, pt) using standard competition ranking."""
    # ensure players with no pt are included if they exist in guild
    items = sorted(players.items(), key=lambda kv: (-kv[1].get("pt", 0), kv[0]))
    result = []
    last_pt = None
    last_rank = 0
    for i, (uid, pdata) in enumerate(items, start=1):
        pt = pdata.get("pt", 0)
        if pt != last_pt:
            rank_number = i
            last_pt = pt
            last_rank = rank_number
        else:
            rank_number = last_rank
        result.append((rank_number, uid, pt))
    return result

async def update_member_display(guild: discord.Guild, user_id: int):
    """Update nickname/roles display for a single member based on players[user_id]['pt']"""
    try:
        member = guild.get_member(user_id)
        if not member:
            return
        pt = players.get(user_id, {}).get("pt", 0)
        name, emoji = get_display_role(pt)
        # Desired nickname format: "表示名 {emoji} {pt}pt"
        # If bot lacks permissions will silently fail.
        new_nick = f"{member.name} {emoji} {pt}pt"
        try:
            await member.edit(nick=new_nick)
        except discord.Forbidden:
            # cannot change nickname: ignore silently
            pass
    except Exception:
        pass

def can_pair(a_pt:int, b_pt:int) -> bool:
    """Check internal rank difference limit (<3)"""
    ra = get_internal_rank(a_pt)
    rb = get_internal_rank(b_pt)
    return abs(ra - rb) < 3

def schedule_draw(delay_seconds: int = DRAW_WAIT_SECONDS):
    """Ensure a draw task is scheduled / restarted for DRAW_WAIT_SECONDS from now."""
    global _draw_task
    async def run_draw_after_wait():
        await asyncio.sleep(delay_seconds)
        # perform draw
        await perform_draw()
    # cancel previous and restart
    if _draw_task and not _draw_task.done():
        _draw_task.cancel()
    _draw_task = asyncio.create_task(run_draw_after_wait())

async def perform_draw():
    """Perform random matching among drawing_pool, respecting can_pair constraints.
    - Paired players are removed from hope_list and drawing_pool; in_match gets set.
    - Unpaired remain in hope_list but removed from drawing_pool.
    - Notify channel: public message in the channel where request was made is not recorded per-user;
      We'll send a public message to the guild general (we use the first available channel: command used channel is preferable).
    """
    # We will pair randomly among drawing_pool
    global drawing_pool
    if not drawing_pool:
        return

    # snapshot and randomize
    candidates = list(drawing_pool)
    random.shuffle(candidates)
    paired = []
    unpaired = set()

    # Attempt greedy pairings from randomized list
    used = set()
    for i in range(len(candidates)):
        if candidates[i] in used:
            continue
        a = candidates[i]
        paired_flag = False
        for j in range(i+1, len(candidates)):
            b = candidates[j]
            if b in used:
                continue
            a_pt = players.get(a, {}).get("pt", 0)
            b_pt = players.get(b, {}).get("pt", 0)
            if can_pair(a_pt, b_pt):
                # pair them
                in_match[a] = b
                in_match[b] = a
                used.add(a)
                used.add(b)
                paired.append((a,b))
                paired_flag = True
                break
        if not paired_flag and a not in used:
            unpaired.add(a)

    # Remove paired from hope_list and drawing_pool
    for a,b in paired:
        hope_list.pop(a, None)
        hope_list.pop(b, None)
        drawing_pool.discard(a)
        drawing_pool.discard(b)

    # Remove paired from drawing_pool. Unpaired remain in hope_list but removed from drawing_pool
    for u in list(unpaired):
        drawing_pool.discard(u)

    # Send notifications (public) about pairings.
    # For notifications, post into the guild's default channel; prefer a text channel that exists.
    # We'll find an appropriate text channel from the guilds (we assume single guild GUILD_ID)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    # choose a channel to post notifications: try system channel > first text channel > None
    channel = None
    if guild.system_channel:
        channel = guild.system_channel
    else:
        for ch in guild.text_channels:
            # Ensure bot has permission to send message
            perms = ch.permissions_for(guild.me)
            if perms.send_messages:
                channel = ch
                break

    if channel is None:
        # can't notify
        return

    for a,b in paired:
        # mention both and instruct winner to /結果報告
        await channel.send(f"<@{a}> と <@{b}> のマッチが成立しました！ 試合後、勝者が /結果報告 を行なってください。")

# -----------------------
# Views for Buttons (Approve / Dispute)
# -----------------------
class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id:int, loser_id:int, origin_channel_id:int, interaction_user_id:int):
        super().__init__(timeout=None)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.origin_channel_id = origin_channel_id
        self.interaction_user_id = interaction_user_id
        self.processed = False

    async def _finalize_autoapprove(self):
        """Called when auto-approve triggers or when approved"""
        # apply pt changes
        w_before = players.get(self.winner_id, {}).get("pt", 0)
        l_before = players.get(self.loser_id, {}).get("pt", 0)
        players.setdefault(self.winner_id, {})["pt"] = w_before + 1
        players.setdefault(self.loser_id, {})["pt"] = max(l_before - 1, 0)

        # update nicknames for both in guild
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await update_member_display(guild, self.winner_id)
            await update_member_display(guild, self.loser_id)

        # remove from in_match
        in_match.pop(self.winner_id, None)
        in_match.pop(self.loser_id, None)

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # only loser can press
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        # finalize
        await self._finalize_autoapprove()
        await interaction.response.edit_message(content=f"承認されました。結果を反映しました。", view=None)

    @discord.ui.button(label="異議", style=discord.ButtonStyle.danger)
    async def dispute_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # only loser can press
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        # notify judge channel
        guild = bot.get_guild(GUILD_ID)
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID) if guild else None
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。 このマッチングは無効扱いとなっています。審議結果を <@{ADMIN_ID}> にご報告ください。")
        # remove match from in_match
        in_match.pop(self.winner_id, None)
        in_match.pop(self.loser_id, None)
        # edit original message to indicate dispute
        await interaction.response.edit_message(content="異議が申立てられました。審議チャンネルへ通知しました。", view=None)

# -----------------------
# Commands
# -----------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        # clear guild commands then resync
        tree.clear_commands(guild=guild)  # NOTE: clear_commands is not awaitable
        await tree.sync(guild=guild)
        print("Guild commands cleared and synced.")
    else:
        print("指定した GUILD_ID のギルドが見つかりません。")

# マッチ希望（ランダム）
@tree.command(name="マッチ希望", description="ランダムマッチに参加（相手指定不要）", guild=discord.Object(id=GUILD_ID))
async def cmd_match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    # check if already in match
    if uid in in_match:
        await interaction.response.send_message("既にマッチ中です。", ephemeral=True)
        return
    # check if already requested
    if uid in hope_list:
        await interaction.response.send_message("既にマッチ希望中です。", ephemeral=True)
        return

    # add to hope list and drawing pool
    hope_list[uid] = now_utc()
    drawing_pool.add(uid)
    # schedule draw timer: last join resets countdown
    async with _draw_task_lock:
        schedule_draw(DRAW_WAIT_SECONDS)

    await interaction.response.send_message("マッチ希望を受け付けました。抽選が行われます。", ephemeral=True)

# マッチ希望取下げ
@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます", guild=discord.Object(id=GUILD_ID))
async def cmd_cancel_request(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in hope_list:
        hope_list.pop(uid, None)
        drawing_pool.discard(uid)
        await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)
    else:
        await interaction.response.send_message("あなたのマッチ希望は見つかりません。", ephemeral=True)

# 結果報告（勝者が実行）
@tree.command(name="結果報告", description="勝者が対戦結果を報告します（敗者承認フローあり）", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="敗者のメンバー（メンション）")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    # must be a registered match (both directions)
    if in_match.get(winner.id) != loser.id or in_match.get(loser.id) != winner.id:
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ希望～抽選で成立した対戦であるか確認してください。", ephemeral=True)
        return

    # send approval UI to channel (public message with buttons)
    channel = interaction.channel
    # create view that only allows loser to approve/dispute
    view = ResultApproveView(winner.id, loser.id, origin_channel_id=channel.id if channel else None, interaction_user_id=interaction.user.id)
    content = f"この試合の勝者は <@{winner.id}> です。敗者の <@{loser.id}> は承認または異議を押してください。承認がない場合は {RESULT_APPROVE_SECONDS//60} 分で自動承認されます。"
    await interaction.response.send_message(content, view=view, ephemeral=False)

    # schedule auto-approve
    key = (winner.id, loser.id)
    async def auto_approve_task():
        await asyncio.sleep(RESULT_APPROVE_SECONDS)
        # If still registered as a match and not processed, auto-approve
        # We can send a synthesized approval if still in in_match
        if in_match.get(winner.id) == loser.id and in_match.get(loser.id) == winner.id:
            # apply pt changes
            w_before = players.get(winner.id, {}).get("pt", 0)
            l_before = players.get(loser.id, {}).get("pt", 0)
            players.setdefault(winner.id, {})["pt"] = w_before + 1
            players.setdefault(loser.id, {})["pt"] = max(l_before - 1, 0)
            # update displays
            guild = bot.get_guild(GUILD_ID)
            if guild:
                await update_member_display(guild, winner.id)
                await update_member_display(guild, loser.id)
            # remove from in_match
            in_match.pop(winner.id, None)
            in_match.pop(loser.id, None)
            # send message to channel to indicate auto-approval occurred
            try:
                ch = channel or (bot.get_guild(GUILD_ID).system_channel if bot.get_guild(GUILD_ID) else None)
                if ch:
                    await ch.send(f"承認がありませんでした。<@{winner.id}> の勝利を自動承認し、Ptを反映しました。")
            except Exception:
                pass

    # start background auto-approve
    task = asyncio.create_task(auto_approve_task())
    pending_results[(winner.id, loser.id)] = {"task": task, "created": now_utc()}

# 管理者: 全初期化
@tree.command(name="admin_reset_all", description="管理者: 全ユーザーのPTと表示をリセット（全員0pt）", guild=discord.Object(id=GUILD_ID))
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    # set all tracked players to 0
    for uid in list(players.keys()):
        players[uid]["pt"] = 0
    # update nicknames
    guild = bot.get_guild(GUILD_ID)
    if guild:
        for uid in players.keys():
            await update_member_display(guild, uid)
    await interaction.response.send_message("全ユーザーのPTを0にリセットしました。", ephemeral=True)

# 管理者: ユーザーPT設定
@tree.command(name="admin_set_pt", description="管理者: ユーザーのPTを設定", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="対象メンバー", pt="設定するPT（0以上）")
async def cmd_admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    if pt < 0:
        await interaction.response.send_message("pt は 0 以上を指定してください。", ephemeral=True)
        return
    players.setdefault(member.id, {})["pt"] = pt
    players[member.id]["updated"] = now_utc()
    # update display
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await update_member_display(guild, member.id)
    await interaction.response.send_message(f"{member.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# /ランキング
@tree.command(name="ランキング", description="現在のランキングを表示（誰でも使用可）", guild=discord.Object(id=GUILD_ID))
async def cmd_ranking(interaction: discord.Interaction):
    # ensure players known: if someone in guild not in players, include with 0
    guild = bot.get_guild(GUILD_ID)
    if guild:
        for m in guild.members:
            if m.bot:
                continue
            players.setdefault(m.id, {}).setdefault("pt", 0)
    ranking = standard_competition_ranking()
    if not ranking:
        await interaction.response.send_message("ランキングはまだありません。", ephemeral=False)
        return
    # build display lines
    lines = []
    for rank_number, uid, pt in ranking:
        member = bot.get_guild(GUILD_ID).get_member(uid)
        display_name = member.display_name if member else f"<@{uid}>"
        _, emoji = get_display_role(pt)
        lines.append(f"{rank_number}位 {display_name} {emoji} {pt}pt")
    text = "🏆 ランキング\n" + "\n".join(lines)
    await interaction.response.send_message(text, ephemeral=False)

# -----------------------
# Background cleanup task: expire hope_list entries older than HOPE_TTL_SECONDS
# -----------------------
@tasks.loop(seconds=60)
async def cleanup_task():
    clean_expired_hopes()

@cleanup_task.before_loop
async def before_cleanup():
    await bot.wait_until_ready()

cleanup_task.start()

# -----------------------
# Start bot
# -----------------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
