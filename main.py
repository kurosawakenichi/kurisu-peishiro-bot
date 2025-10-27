# main.py
# 基本 main.py ランダム — フル実装（置き換え用）
# 環境変数（Railway Variables 等）に以下を設定してください:
# DISCORD_TOKEN, GUILD_ID, ADMIN_ID, RANKING_CHANNEL_ID, JUDGE_CHANNEL_ID
#
# 使い方:
#  - /マッチ希望         : ランダムマッチ希望（相手指定不要）
#  - /マッチ希望取下げ   : 自分の希望を取り下げ
#  - /結果報告 敗者: 勝者が報告（敗者承認フローが動作）
#  - /ランキング         : 誰でも使用可（現在のpt順表示）
#  - 管理者コマンド:
#      /admin_set_pt ユーザー pt
#      /admin_reset_all
#
# 依存: discord.py（2.x）、python >=3.10 推奨
# 永続化: data.json にユーザーデータを保存します
# -----------------------------------------------------------------------------

import os
import json
import random
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Set, Tuple, List
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks, commands

# -----------------------
# 環境変数読み込み
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

# 自動承認までの秒数（仕様：15分 -> 900s）
AUTO_APPROVE_SECONDS = 15 * 60

# 抽選ウェイト（待ち時間: 3秒 と指定のためデフォルト3）
DRAW_WAIT_SECONDS = 3

DATA_FILE = "data.json"

# -----------------------
# ランク定義（表示用・内部処理に使用）
# タプルは (start_pt, end_pt, role_name, icon)
# role_name は Discord にあらかじめ作成済みのロール名を想定
# -----------------------
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
    (25, 99999, "Challenger", "😈"),
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

# -----------------------
# Data structures
# -----------------------
# user_data: str(user_id) -> {"pt":int}
# matching structures in memory:
#  - wish_list: Dict[user_id, timestamp_of_request]
#  - draw_list: Set[user_id]  # currently in draw window
#  - in_match: Dict[user_id, opponent_id]  # both directions
#  - pending_result: Dict[winner_id, loser_id]  # waiting for loser approval or auto-approve
# -----------------------

user_data: Dict[str, Dict] = {}
wish_list: Dict[int, float] = {}   # user_id -> request_time (epoch)
draw_list: Set[int] = set()        # in current draw window
in_match: Dict[int, int] = {}      # user_id -> opponent_id
pending_result: Dict[int, Tuple[int, float]] = {}  # winner_id -> (loser_id, deadline_ts)
interaction_store: Dict[int, app_commands.Context] = {}  # not reliable long-term; minimal use

DATA_LOCK = asyncio.Lock()

# -----------------------
# Helper functions
# -----------------------
def now_ts() -> float:
    return datetime.utcnow().timestamp()

def load_data():
    global user_data
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                user_data = json.load(f)
        else:
            user_data = {}
    except Exception:
        user_data = {}

def save_data_sync():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(user_data, f, ensure_ascii=False, indent=2)

async def save_data():
    async with DATA_LOCK:
        save_data_sync()

def get_pt(uid: int) -> int:
    return int(user_data.get(str(uid), {}).get("pt", 0))

def set_pt(uid: int, pt: int):
    user_data.setdefault(str(uid), {})["pt"] = max(0, int(pt))

def get_rank_entry(pt: int):
    for s, e, role, icon in rank_roles:
        if s <= pt <= e:
            return (s, e, role, icon)
    return rank_roles[-1]

def get_icon_for_pt(pt: int) -> str:
    return get_rank_entry(pt)[3]

def get_role_name_for_pt(pt: int) -> str:
    return get_rank_entry(pt)[2]

def get_internal_rank(pt: int) -> int:
    for r, rng in rank_ranges_internal.items():
        if pt in rng:
            return r
    return 6

def calc_pt_delta(win_pt: int, lose_pt: int) -> Tuple[int,int]:
    """
    ベースロジック（内部ランク差に基づく簡略化）:
    - rank差 >=3 => マッチ不可（呼び出し前にチェックする）
    - 同rank: win +1, lose -1
    - 1rank上の相手に勝つ: win +2, lose -1
    - 2rank上の相手に勝つ: win +3, lose -1
    - 1rank下の相手に勝つ: win +1, lose -2
    - 2rank下の相手に勝つ: win +1, lose -3
    """
    r_win = get_internal_rank(win_pt)
    r_lose = get_internal_rank(lose_pt)
    diff = r_lose - r_win  # positive if loser is higher rank
    # Determine winner delta based on relative rank of loser vs winner
    if diff == 0:
        w_delta = 1
        l_delta = -1
    elif diff == 1:
        # loser 1 rank higher than winner => winner beat higher
        w_delta = 2
        l_delta = -1
    elif diff >= 2:
        w_delta = 3
        l_delta = -1
    elif diff == -1:
        # loser 1 rank lower
        w_delta = 1
        l_delta = -2
    else:  # diff <= -2
        w_delta = 1
        l_delta = -3
    return w_delta, l_delta

# チャレンジ例外チェック: 指定pt時の制約
def challenge_constraints_allow(my_pt: int, other_pt: int) -> bool:
    # 3,8,13,18,23 -> 相手は同pt以上のみ
    if my_pt in (3, 8, 13, 18, 23):
        return other_pt >= my_pt
    # 4,9,14,19,24 -> 相手は同pt-1 または 同pt以上
    if my_pt in (4, 9, 14, 19, 24):
        return (other_pt >= my_pt) or (other_pt == my_pt - 1)
    return True

# ニックネーム更新（表示ユーザー名 + (12pt 🔰) 形式）
async def update_member_display(member: discord.Member):
    try:
        pt = get_pt(member.id)
        icon = get_icon_for_pt(pt)
        base_name = member.display_name.split(" (")[0]  # 既に付与されている括りがあれば切る
        new_nick = f"{base_name} ({pt}pt {icon})"
        # Avoid trying to change if same
        if member.nick != new_nick:
            try:
                await member.edit(nick=new_nick)
            except discord.Forbidden:
                # 権限不足の場合は無視（管理者は手動対応）
                pass
            except Exception:
                pass
        # ロール付与/削除: メンバーのロールをptに応じたロール名に変更
        guild = member.guild
        target_role_name = get_role_name_for_pt(pt)
        # find role objects
        target_role = discord.utils.get(guild.roles, name=target_role_name)
        if target_role:
            # remove all rank roles if present, add target if not present
            rank_role_names = [r[2] for r in rank_roles]
            to_remove = [discord.utils.get(guild.roles, name=name) for name in rank_role_names if discord.utils.get(guild.roles, name=name)]
            # remove other rank roles
            try:
                for rr in to_remove:
                    if rr in member.roles and rr != target_role:
                        await member.remove_roles(rr, reason="Rank role auto-update")
                # ensure target role present
                if target_role not in member.roles:
                    await member.add_roles(target_role, reason="Rank role auto-update")
            except discord.Forbidden:
                pass
            except Exception:
                pass
    except Exception:
        pass

async def update_all_members_display(guild: discord.Guild):
    for m in guild.members:
        await update_member_display(m)

# -----------------------
# Bot / Command setup
# -----------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = False

bot = commands.Bot(command_prefix="/", intents=intents, help_command=None)

# -----------------------
# Views for buttons
# -----------------------
class ApproveMatchView(discord.ui.View):
    def __init__(self, applicant_id:int, opponent_id:int, origin_channel_id:int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.opponent_id = opponent_id
        self.origin_channel_id = origin_channel_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 承認できるのは被申請者のみ
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("承認できるのは申請された相手のみです。", ephemeral=True)
            return
        # 成立させる
        matching_success = False
        if (self.applicant_id not in in_match) and (self.opponent_id not in in_match):
            in_match[self.applicant_id] = self.opponent_id
            in_match[self.opponent_id] = self.applicant_id
            matching_success = True
        # 公開メッセージを申請発行元チャンネルに流す
        guild = interaction.guild
        ch = guild.get_channel(self.origin_channel_id) if self.origin_channel_id else interaction.channel
        if ch and matching_success:
            await ch.send(f"<@{self.applicant_id}> と <@{self.opponent_id}> のマッチングが成立しました。試合後、勝者が /結果報告 を行なってください。")
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
        if in_match.get(a) == b:
            in_match.pop(a, None)
            in_match.pop(b, None)
            await interaction.response.send_message(f"<@{a}> と <@{b}> のマッチングは解除されました。", ephemeral=False)
        else:
            await interaction.response.send_message("該当のマッチは既に解除されています。", ephemeral=True)
        self.stop()

class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id:int, loser_id:int, origin_channel_id:int):
        super().__init__(timeout=60*5)  # 5分有効
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.origin_channel_id = origin_channel_id
        self.processed = False

    async def on_timeout(self):
        # 自動承認: 申請から5分後に自動的に勝者申告を処理する（ただし既に処理済なら無視）
        if not self.processed:
            # check match still registered
            if is_registered_match(self.winner_id, self.loser_id):
                # channel resolution
                # try origin channel
                guild = None
                channel = None
                # Try to get guild & channel from bot
                for g in bot.guilds:
                    channel = g.get_channel(self.origin_channel_id)
                    if channel:
                        guild = g
                        break
                # fallback: any channel
                if not channel:
                    # try first guild default text channel
                    channel = None
                await handle_approved_result(self.winner_id, self.loser_id, channel)

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
        ch = interaction.channel
        await handle_approved_result(self.winner_id, self.loser_id, ch)

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
        # 審判チャンネルに投稿（管理者に知らせる）
        guild = interaction.guild
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID) if JUDGE_CHANNEL_ID else None
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。 このマッチングは無効扱いとなっています。審議結果を @kurosawa0118 にご報告ください。")
        # マッチ情報は解除（管理者が手動で処理）
        in_match.pop(self.winner_id, None)
        in_match.pop(self.loser_id, None)

# -----------------------
# Utility: match check
# -----------------------
def is_registered_match(a_id: int, b_id: int) -> bool:
    return in_match.get(a_id) == b_id and in_match.get(b_id) == a_id

# -----------------------
# Core result handling
# -----------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: Optional[discord.abc.Messageable]):
    # マッチ登録チェック
    if not is_registered_match(winner_id, loser_id):
        if channel:
            try:
                await channel.send("このマッチングは登録されていません。まずはマッチ申請をお願いします。")
            except Exception:
                pass
        return

    winner_pt = get_pt(winner_id)
    loser_pt = get_pt(loser_id)

    # 計算
    w_delta, l_delta = calc_pt_delta(winner_pt, loser_pt)
    winner_new = max(0, winner_pt + w_delta)
    loser_new = max(0, loser_pt + l_delta)

    # 例外: 昇級チャレンジ時の敗北時の降格先（3,4->2等）
    # 負けた側が 4,9,14,19,24 の場合は規定の降格先に戻す
    if loser_pt in (4,9,14,19,24) and l_delta < 0:
        # map to降格先
        mapping = {4:2,9:7,14:12,19:17,24:22}
        loser_new = mapping.get(loser_pt, loser_new)
    if loser_pt in (3,8,13,18,23) and l_delta < 0:
        mapping = {3:2,8:7,13:12,18:17,23:22}
        loser_new = mapping.get(loser_pt, loser_new)

    # 書き込み
    set_pt(winner_id, winner_new)
    set_pt(loser_id, loser_new)
    await save_data()

    # 反映（ギルド単位）
    for g in bot.guilds:
        w_member = g.get_member(winner_id)
        l_member = g.get_member(loser_id)
        if w_member:
            await update_member_display(w_member)
        if l_member:
            await update_member_display(l_member)

    # マッチ解除
    in_match.pop(winner_id, None)
    in_match.pop(loser_id, None)

    # 結果メッセージ
    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    if channel:
        try:
            await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")
        except Exception:
            pass

# -----------------------
# Background: draw processing
# - wish_list: user_id -> request_time
# - draw_list: users currently in draw window
# - When a user adds wish, include them in draw_list and start/extend timer.
# - After DRAW_WAIT_SECONDS of inactivity, randomly pair draw_list members into matches
# - Respect challenge constraints and rank-diff (rank差3以上は不成立)
# -----------------------
last_draw_time = 0.0
draw_wait_handle = None

async def process_draws(channel: discord.TextChannel):
    """
    Called when draw window time passes: attempt to create pairings from draw_list.
    """
    global draw_list
    if not draw_list:
        return
    candidates = list(draw_list)
    random.shuffle(candidates)
    paired = set()
    created_pairs = []
    # Attempt greedy pairing with rank constraints
    for i in range(len(candidates)):
        a = candidates[i]
        if a in paired:
            continue
        for j in range(i+1, len(candidates)):
            b = candidates[j]
            if b in paired:
                continue
            # check if either is already in match
            if a in in_match or b in in_match:
                continue
            # rank-diff check
            if abs(get_internal_rank(get_pt(a)) - get_internal_rank(get_pt(b))) >= 3:
                continue
            # challenge-specific constraints
            if not challenge_constraints_allow(get_pt(a), get_pt(b)):
                continue
            if not challenge_constraints_allow(get_pt(b), get_pt(a)):
                continue
            # pair them
            paired.add(a); paired.add(b)
            created_pairs.append((a,b))
            break
    # For each created pair, remove from wish_list and draw_list, set temporary pending state via public message for approval flow
    for a,b in created_pairs:
        draw_list.discard(a)
        draw_list.discard(b)
        wish_list.pop(a, None)
        wish_list.pop(b, None)
        # publish a channel message that match is formed, and record in_match only after both confirm with approve button
        # For random variant we automatically set in_match (no individual approval step) — but per spec we want confirmation? The Random spec: when matched, in_match register and notify both to start match.
        if a not in in_match and b not in in_match:
            in_match[a] = b
            in_match[b] = a
            try:
                await channel.send(f"<@{a}> vs <@{b}> のマッチが成立しました。試合後、勝者が /結果報告 を行なってください。")
            except Exception:
                pass

    # leave remaining unmatched candidates in wish_list (per spec: do not remove from wish list; they stay until 5 minutes expire)
    # clear draw_list wholly
    draw_list.clear()

# -----------------------
# Cleanup task
# - remove expired wish_list entries (after 5 minutes)
# - auto-approve pending results after AUTO_APPROVE_SECONDS (15min) from time of report (we store pending_result via pending_result mapping)
# - cleanup in_match that are stale (safety): if a match exists but no report after long time, we keep it — but auto remove if > 24h
# -----------------------
@tasks.loop(seconds=30.0)
async def cleanup_task():
    # wish_list expiry
    now = now_ts()
    expired = []
    for uid, ts in list(wish_list.items()):
        if now - ts > 5*60:  # 5 minutes
            expired.append(uid)
    for uid in expired:
        wish_list.pop(uid, None)
        # do NOT announce (only to user ephemeral ideally; here do nothing)

    # pending_result auto-approve (we store pending_result via pending_result dict when winner invoked /結果報告; use this only if used)
    # But core flow uses ResultApproveView with its own timeout auto_approve; this is a safety fallback
    to_auto = []
    for winner, (loser, deadline) in list(pending_result.items()):
        if now >= deadline:
            to_auto.append((winner, loser))
    for winner, loser in to_auto:
        # attempt to find any channel to post in (ranking channel fallback)
        ch = None
        if RANKING_CHANNEL_ID:
            for g in bot.guilds:
                c = g.get_channel(RANKING_CHANNEL_ID)
                if c:
                    ch = c
                    break
        await handle_approved_result(winner, loser, ch)
        pending_result.pop(winner, None)

# -----------------------
# COMMANDS
# -----------------------

# guild sync helper
async def sync_commands_guild(guild_id: int):
    try:
        await bot.tree.sync(guild=discord.Object(id=guild_id))
    except Exception:
        # best effort; ignore
        pass

# on_ready: load data & start cleanup task
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    # Sync commands to guild
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("Commands synced to guild.")
    except Exception as e:
        print("コマンド同期エラー:", e)
    # load data
    load_data()
    # start cleanup task if not running
    try:
        if not cleanup_task.is_running():
            cleanup_task.start()
    except Exception as e:
        print("cleanup_task start error:", e)

# ---- /マッチ希望 ----
@bot.tree.command(name="マッチ希望", description="ランダムマッチの希望を出します（相手指定なし）")
async def cmd_match_wish(interaction: discord.Interaction):
    uid = interaction.user.id
    # already in a match?
    if uid in in_match:
        opp = in_match.get(uid)
        await interaction.response.send_message(f"あなたは現在 <@{opp}> とマッチ中です。", ephemeral=True)
        return
    # already in wish_list?
    if uid in wish_list:
        await interaction.response.send_message("既にマッチ希望が登録されています。", ephemeral=True)
        return
    # add to wish_list
    wish_list[uid] = now_ts()
    draw_list.add(uid)
    # store this interaction for potential followup ephemeral to this user later
    # NOTE: we keep minimal reference only during process — use with care
    try:
        interaction_store[uid] = interaction
    except Exception:
        pass

    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)

    # schedule a draw if no other scheduling in place
    async def delayed_draw():
        await asyncio.sleep(DRAW_WAIT_SECONDS)
        # find channel to post matches: use the interaction channel if present
        channel = interaction.channel if interaction.channel else None
        # fallback to ranking channel if available
        if not channel and RANKING_CHANNEL_ID:
            for g in bot.guilds:
                c = g.get_channel(RANKING_CHANNEL_ID)
                if c:
                    channel = c
                    break
        await process_draws(channel)

    bot.loop.create_task(delayed_draw())

# ---- /マッチ希望取下げ ----
@bot.tree.command(name="マッチ希望取下げ", description="登録したマッチ希望を取り下げます（自分専用表示）")
async def cmd_cancel_match_wish(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in wish_list:
        wish_list.pop(uid, None)
        draw_list.discard(uid)
        await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)
    else:
        await interaction.response.send_message("マッチ希望が見つかりませんでした。", ephemeral=True)

# ---- /結果報告 （勝者が使う） ----
@bot.tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent

    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ申請をお願いします。", ephemeral=True)
        return

    # 敗者への承認ビューを送信（チャンネル上に表示）
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    sent_msg = None
    # Post to the channel where command executed (公開)
    ch = interaction.channel
    view = ResultApproveView(winner.id, loser.id, origin_channel_id=ch.id if ch else None)
    try:
        sent_msg = await ch.send(content, view=view)
    except Exception:
        # fallback to ephemeral message to winner only
        await interaction.response.send_message("敗者への承認メッセージを送信できませんでした。管理者に連絡してください。", ephemeral=True)
        return

    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)

    # pending_result entry (safety) used by cleanup_task fallback
    pending_result[winner.id] = (loser.id, now_ts() + AUTO_APPROVE_SECONDS)

# ---- /ランキング ----
@bot.tree.command(name="ランキング", description="現在のランキングを表示します（誰でも使用可）")
async def cmd_ranking(interaction: discord.Interaction):
    # Build ranking sorted by pt desc
    entries: List[Tuple[int,int]] = []
    for uid_s, info in user_data.items():
        try:
            uid = int(uid_s)
            pt = int(info.get("pt", 0))
            entries.append((uid, pt))
        except Exception:
            pass
    # include guild members who may not be in user_data yet
    # sort desc
    entries.sort(key=lambda x: x[1], reverse=True)

    if not entries:
        await interaction.response.send_message("ランキングデータがありません。", ephemeral=True)
        return

    # Standard competition ranking (1,2,2,4)
    ranking_lines = []
    prev_pt = None
    rank = 0
    displayed_rank = 0
    for uid, pt in entries:
        rank += 1
        if pt != prev_pt:
            displayed_rank = rank
        prev_pt = pt
        # Use pure username (avoid duplication with nickname having pt icon)
        member = None
        for g in bot.guilds:
            m = g.get_member(uid)
            if m:
                member = m
                break
        name = member.name if member else f"<@{uid}>"
        icon = get_icon_for_pt(pt)
        ranking_lines.append(f"{displayed_rank}位 {name} {icon} {pt}pt")

    # send as ephemeral or public? Defaults ephemeral
    await interaction.response.send_message("\n".join(ranking_lines), ephemeral=True)

# -----------------------
# 管理者コマンド
# -----------------------
def is_admin_check(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_ID

@bot.tree.command(name="admin_set_pt", description="管理者: ユーザーのPTを設定します（管理者のみ）")
@app_commands.describe(target="対象ユーザー", pt="設定するpt（数値）")
async def cmd_admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if not is_admin_check(interaction):
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    set_pt(target.id, pt)
    await save_data()
    # update member display
    await update_member_display(target)
    await interaction.response.send_message(f"{target.display_name} を {pt}pt に設定しました。", ephemeral=True)

@bot.tree.command(name="admin_reset_all", description="管理者: 全ユーザーのPTと表示を初期化します（管理者のみ）")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if not is_admin_check(interaction):
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    # reset pts for all known users
    for uid in list(user_data.keys()):
        user_data[uid]["pt"] = 0
    await save_data()
    # update displays in guilds
    for g in bot.guilds:
        await update_all_members_display(g)
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

# -----------------------
# Error handlers & safety
# -----------------------
@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    # Generic handler to avoid "アプリケーションが応答しませんでした" in many cases
    try:
        if isinstance(error, app_commands.errors.CommandSignatureMismatch):
            await interaction.response.send_message("コマンドの署名が不一致です。コマンド同期を試みます。", ephemeral=True)
            await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
            return
        # If interaction response already acknowledged, try followup
        if interaction.response.is_done():
            try:
                await interaction.followup.send(f"エラーが発生しました: {error}", ephemeral=True)
            except Exception:
                pass
        else:
            try:
                await interaction.response.send_message(f"エラーが発生しました: {error}", ephemeral=True)
            except Exception:
                pass
    except Exception:
        pass

# -----------------------
# Start the bot
# -----------------------
if __name__ == "__main__":
    load_data()
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot 起動エラー:", e)
