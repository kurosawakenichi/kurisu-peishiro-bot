# -*- coding: utf-8 -*-
# 完全版: 基本 main.py ランダム
# - /マッチ希望 (ランダムマッチ希望、相手指定不要)
# - /マッチ希望取下げ
# - マッチ抽選(5秒待機で抽選)、余りは希望リストに残る（5分経過で自動取消）
# - マッチ成立は該当チャンネルに公開投稿（勝者が /結果報告）
# - /結果報告: 勝者が報告 -> 敗者に承認/異議ボタン（敗者のみ押せる）
#   - 承認: 自動でpt反映 (Light 仕様: 勝者 +1 / 敗者 -1、下限0)
#   - 異議: 審議チャンネルへ通知し当該マッチは無効扱い（管理者が手動で処理）
#   - 承認ボタン有効期限: 5分 -> 期限切れは自動承認
# - /ランキング: 全ユーザーが使用可（標準競技方式ランキング）
# - 管理者コマンド: /admin_set_pt, /admin_reset_all
# - ユーザー表示: ニックネームを「元の表示名 + ' ' + アイコン + ' {n}pt'」へ更新
#   - 既存の後付け部分は正規表現で消去して上書きします（重複表示を防ぐ）
# - 内部的データはメモリ管理（永続化を行いません）
# - 抽選・マッチング等の内部リストは公開されません（DMは使わない）

import os
import asyncio
import logging
import random
import re
from typing import Dict, Optional, Set, List, Tuple
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

# ------------------------------
# 環境変数
# ------------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
# RANKING_CHANNEL_ID は表示用に保持（自動投稿は行わない/手動コマンドで使用）
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)

# ------------------------------
# ロギング
# ------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("matchbot")

# ------------------------------
# Intents / Bot 初期化
# ------------------------------
intents = discord.Intents.default()
intents.message_content = False  # 不要
intents.members = True  # 必須: メンバー管理/ニックネーム変更に必要
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ------------------------------
# 設定値
# ------------------------------
DRAW_WAIT_SECONDS = 5               # 抽選待機時間（秒） — ユーザーが入るたびにリセット
HOPE_EXPIRE_SECONDS = 5 * 60       # マッチ希望の有効期限（秒） → 5 分
RESULT_APPROVE_SECONDS = 5 * 60    # 敗者承認の有効期限（秒） → 5 分
MATCH_AUTO_CLEAR_SECONDS = 15 * 60 # マッチ成立後何もしない場合の自動クリア（15分）
# Light 仕様: 勝利 +1 / 敗北 -1 ; pt下限 0
PT_MIN = 0

# ------------------------------
# ランク表示テーブル（ライト仕様、Challenge 無し）
# 0–4 Beginner 🔰
# 5–9 Silver 🥈
# 10–14 Gold 🥇
# 15–19 Master ⚔️
# 20–24 GroundMaster 🪽
# 25+ Challenger 😈
# ------------------------------
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

# 内部ランク (rank1..rank6) の定義 (マッチ可否制限に使う)
rank_ranges_internal = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000),# 25+
}

def get_internal_rank(pt: int) -> int:
    for k, r in rank_ranges_internal.items():
        if pt in r:
            return k
    return 6

def get_rank_info(pt: int) -> Tuple[str, str]:
    """(role_name, icon)"""
    for s, e, name, icon in rank_roles:
        if s <= pt <= e:
            return name, icon
    return "Challenger", "😈"

# ------------------------------
# データ構造（メモリ）
# ------------------------------
# user_data: { user_id: {"pt": int, "ever_gold": bool} }
user_data: Dict[int, Dict] = {}

# hope_list: マッチ希望中ユーザー
# { user_id: {"ts": datetime, "origin_channel_id": int} }
hope_list: Dict[int, Dict] = {}

# draw_group: 当該抽選に参加中のユーザー集合
draw_group: Set[int] = set()
_draw_task: Optional[asyncio.Task] = None
_draw_lock = asyncio.Lock()

# in_match: マッチ成立中のユーザー -> opponent_id
in_match: Dict[int, int] = {}

# pending_result: 勝者が報告して敗者承認待ち中のマッチ (winner_id -> loser_id)
pending_result: Dict[int, int] = {}

# ux: store origin channel id for users (last call)
user_origin_channel: Dict[int, int] = {}

# ------------------------------
# ユーティリティ関数
# ------------------------------
def now() -> datetime:
    return datetime.utcnow()

def cleanup_expired_hopes():
    """期限切れのマッチ希望を削除"""
    expired = []
    cutoff = now() - timedelta(seconds=HOPE_EXPIRE_SECONDS)
    for uid, info in list(hope_list.items()):
        if info["ts"] < cutoff:
            expired.append(uid)
    for uid in expired:
        hope_list.pop(uid, None)
        # ensure removal from draw_group if present
        draw_group.discard(uid)

def standard_competition_ranking(users_pts: List[Tuple[int,int]]) -> List[Tuple[int,int,int]]:
    """
    users_pts: list of (user_id, pt)
    return: list of tuples (rank, user_id, pt)
    Standard competition ranking: 1,2,2,4...
    """
    # Sort by pt desc, user id stable
    sorted_list = sorted(users_pts, key=lambda x: (-x[1], x[0]))
    result = []
    last_pt = None
    rank = 0
    count = 0
    for uid, pt in sorted_list:
        count += 1
        if pt != last_pt:
            rank = count
            last_pt = pt
        result.append((rank, uid, pt))
    return result

def sanitize_base_name(nick_or_name: str) -> str:
    """
    ユーザーの表示名から既に追加されている " <icon> Npt" 部分を取り除く
    例: "alice 🔰 3pt" -> "alice"
    """
    # 末尾に「 空白 + 何らかの絵文字 + 空白 + 数字 + pt 」というパターンを取り除く
    # ある程度寛容にマッチさせる
    s = re.sub(r'\s[^\s]{1,3}\s*\d+pt\s*$', '', nick_or_name)
    # 余分なスペースを右側のみ削除
    return s.strip()

async def update_member_display(member: discord.Member):
    """
    メンバーのニックネーム (guild nickname) を更新して
    基本: <base_display_name> <icon> <n>pt
    既存の後付けは sanitize して上書き（重複防止）
    またロール（Beginner/Silver/..）を付与/削除する
    """
    uid = member.id
    data = user_data.get(uid, {})
    pt = data.get("pt", 0)
    role_name, icon = get_rank_info(pt)

    # 元のベース名: guild nickname があればそれを優先、なければ member.name
    base = member.nick or member.name
    base = sanitize_base_name(base)

    new_nick = f"{base} {icon} {pt}pt"
    # Discord のニックネームは 32 文字制限
    if len(new_nick) > 32:
        new_nick = new_nick[:32]

    # Update nick if different
    try:
        if member.nick != new_nick:
            await member.edit(nick=new_nick)
    except discord.Forbidden:
        logger.warning(f"Insufficient permission to change nickname for {member} ({uid})")
    except Exception as e:
        logger.exception("Failed to edit nickname: %s", e)

    # Role management: ensure the role corresponding to role_name is present, and other rank roles removed
    guild = member.guild
    if guild:
        try:
            # find role objects
            target_role = discord.utils.get(guild.roles, name=role_name)
            if target_role and target_role not in member.roles:
                # remove other rank roles first
                for _, _, rn, _ in rank_roles:
                    r = discord.utils.get(guild.roles, name=rn)
                    if r and r in member.roles and r != target_role:
                        try:
                            await member.remove_roles(r, reason="rank sync")
                        except discord.Forbidden:
                            logger.warning(f"No permission to remove role {r} from {member}")
                # add target role
                try:
                    await member.add_roles(target_role, reason="rank sync")
                except discord.Forbidden:
                    logger.warning(f"No permission to add role {target_role} to {member}")
            else:
                # still ensure extraneous rank roles removed
                for _, _, rn, _ in rank_roles:
                    r = discord.utils.get(guild.roles, name=rn)
                    if r and r in member.roles and (not target_role or r != target_role):
                        try:
                            await member.remove_roles(r, reason="rank sync cleanup")
                        except discord.Forbidden:
                            logger.warning(f"No permission to remove role {r} from {member}")
        except Exception as e:
            logger.exception("Role update error: %s", e)

# ------------------------------
# PT計算
# - Basic main.py ライト に準拠（ランク差補正なし：勝ち+1、負け-1、pt下限0）
# ------------------------------
def compute_pt_delta_winner_loser(winner_pt: int, loser_pt: int) -> Tuple[int,int]:
    """
    returns (winner_new, loser_new)
    Light rules: winner +1, loser -1, pt floor at 0
    """
    w_new = max(PT_MIN, winner_pt + 1)
    l_new = max(PT_MIN, loser_pt - 1)
    return w_new, l_new

# ------------------------------
# マッチング抽選ロジック（draw timer）
# ------------------------------
async def schedule_draw_after_delay():
    """
    管理下の draw_group に対して、DRAW_WAIT_SECONDS 秒のタイマーを開始する。
    入るたびにこの関数はキャンセル＋再実行されるため、単一実行となる。
    """
    global _draw_task
    async with _draw_lock:
        if _draw_task and not _draw_task.done():
            _draw_task.cancel()
            _draw_task = None
        _draw_task = asyncio.create_task(_draw_worker())

async def _draw_worker():
    """実際に待機してから抽選を行うワーカー"""
    try:
        await asyncio.sleep(DRAW_WAIT_SECONDS)
        await perform_draw()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("draw worker crashed")

async def perform_draw():
    """
    draw_group にいるユーザー達でランダムに組を作る。
    マッチ制約: 内部rank差が 3 以上ならマッチ不可（ライト仕様での制限継続）
    成立したペアは in_match に登録、希望リストから削除される。
    余りは hope_list に残す（5分タイマーは継続）
    マッチ成立は各ユーザーの origin_channel_id に公開投稿する（相互のチャンネルが違う場合は両方で通知）
    """
    async with _draw_lock:
        if not draw_group:
            return
        # build candidate list from draw_group (filter out expired hopes)
        now_dt = now()
        candidates = []
        for uid in list(draw_group):
            info = hope_list.get(uid)
            if not info:
                # not in hope_list anymore, remove from draw_group
                draw_group.discard(uid)
                continue
            # only include if still within HOPE_EXPIRE_SECONDS
            if info["ts"] + timedelta(seconds=HOPE_EXPIRE_SECONDS) < now_dt:
                # expired
                hope_list.pop(uid, None)
                draw_group.discard(uid)
                continue
            candidates.append(uid)

        if not candidates:
            draw_group.clear()
            return

        random.shuffle(candidates)
        paired = set()  # already paired users
        pairs: List[Tuple[int,int]] = []

        # Attempt to greedily pair adjacent users while respecting internal rank difference < 3
        # We'll try a simple greedy algorithm: iterate and try to find a match for each unpaired user
        for i in range(len(candidates)):
            if candidates[i] in paired:
                continue
            a = candidates[i]
            a_pt = user_data.get(a, {}).get("pt", 0)
            a_rank = get_internal_rank(a_pt)
            # find someone to pair with after i
            found = None
            for j in range(i+1, len(candidates)):
                b = candidates[j]
                if b in paired:
                    continue
                b_pt = user_data.get(b, {}).get("pt", 0)
                b_rank = get_internal_rank(b_pt)
                if abs(a_rank - b_rank) < 3:
                    found = b
                    break
            if found:
                paired.add(a)
                paired.add(found)
                pairs.append((a, found))

        # Register pairs
        for a, b in pairs:
            # remove from hope_list and draw_group
            hope_list.pop(a, None)
            hope_list.pop(b, None)
            draw_group.discard(a)
            draw_group.discard(b)
            in_match[a] = b
            in_match[b] = a
            # notify in origin channels (if present)
            ch_a = user_origin_channel.get(a)
            ch_b = user_origin_channel.get(b)
            # choose a set of channel ids to send public message to:
            send_channels = set()
            if ch_a:
                send_channels.add(ch_a)
            if ch_b:
                send_channels.add(ch_b)
            content = f"<@{a}> vs <@{b}> のマッチングが成立しました。試合後、勝者が `/結果報告` を行ってください。"
            for cid in send_channels:
                try:
                    ch = bot.get_channel(cid)
                    if ch:
                        await ch.send(content)
                except Exception:
                    logger.exception("Failed to send match notice to channel %s", cid)
            # schedule auto-clear for this match in case nothing happens
            asyncio.create_task(_auto_clear_match_after_timeout(a, b))
        # After drawing clear the draw_group leftovers (we removed paired ones)
        # draw_group already had paired discarded individually
        # Leave hope_list entries for leftovers (they keep their expiration)
        # Done

async def _auto_clear_match_after_timeout(a: int, b: int):
    """マッチ成立後、MATCH_AUTO_CLEAR_SECONDS 経過で in_match をクリア（if still present）"""
    await asyncio.sleep(MATCH_AUTO_CLEAR_SECONDS)
    if in_match.get(a) == b and in_match.get(b) == a:
        in_match.pop(a, None)
        in_match.pop(b, None)
        # If pending result exists (maybe winner reported), it's ignored (we only clear idle matches)
        logger.info(f"Auto-cleared idle match {a} vs {b}")

# ------------------------------
# VIEW: 勝者報告に対する敗者の承認ビュー（ボタン）
# ------------------------------
class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id: int, loser_id: int, origin_channel_id: Optional[int]):
        super().__init__(timeout=RESULT_APPROVE_SECONDS)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.origin_channel_id = origin_channel_id
        self.processed = False

    async def on_timeout(self):
        # タイムアウトしたら自動承認（勝者の申告どおり）
        if not self.processed:
            self.processed = True
            # perform auto approval
            ch = bot.get_channel(self.origin_channel_id) if self.origin_channel_id else None
            # if channel None, try to find guild channel via stored origin
            target_channel = ch or (bot.get_channel(RANKING_CHANNEL_ID) if RANKING_CHANNEL_ID else None)
            if target_channel:
                try:
                    await handle_approved_result(self.winner_id, self.loser_id, target_channel)
                    # notify in channel
                    await target_channel.send(f"承認期限が切れたため、<@{self.winner_id}> の申告を自動承認しました。")
                except Exception:
                    logger.exception("Auto-approval failed")
            # cleanup pending_result mapping if present
            pending_result.pop(self.winner_id, None)

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        # reflect result
        ch = interaction.channel
        await interaction.response.edit_message(content="承認されました。結果を反映します。", view=None)
        try:
            await handle_approved_result(self.winner_id, self.loser_id, ch)
        except Exception:
            logger.exception("Error reflecting approved result")
        pending_result.pop(self.winner_id, None)

    @discord.ui.button(label="異議", style=discord.ButtonStyle.danger)
    async def dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        # mark as disputed => post to judge channel, clear in_match and pending_result
        guild = interaction.guild
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID) if guild else None
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。 このマッチングは無効扱いとなっています。審議結果を <@{ADMIN_ID}> にご報告ください。")
        # notify original
        await interaction.response.edit_message(content="異議が申立てられました。審議チャンネルへ通知しました。", view=None)
        # remove in_match
        in_match.pop(self.winner_id, None)
        in_match.pop(self.loser_id, None)
        pending_result.pop(self.winner_id, None)

# ------------------------------
# 実際の結果反映処理
# ------------------------------
async def handle_approved_result(winner_id: int, loser_id: int, channel: discord.abc.Messageable):
    """
    勝者申告が承認 (or 自動承認) されたときの実処理:
    - 該当マッチが in_match に登録されていることを確認
    - pt を計算して user_data を更新
    - ユーザー名・ロールを更新
    - in_match, pending_result を削除
    - 結果メッセージを投稿
    """
    # マッチ確認
    if in_match.get(winner_id) != loser_id or in_match.get(loser_id) != winner_id:
        await channel.send("このマッチングは登録されていません。まずは /マッチ希望 をお願いします。")
        return

    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt = user_data.get(loser_id, {}).get("pt", 0)
    winner_new, loser_new = compute_pt_delta_winner_loser(winner_pt, loser_pt)

    # update data
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new

    # Reflect to guild members (update display and roles)
    # iterate through guilds and try to find members
    for guild in bot.guilds:
        w_member = guild.get_member(winner_id)
        l_member = guild.get_member(loser_id)
        if w_member:
            try:
                await update_member_display(w_member)
            except Exception:
                logger.exception("Failed updating winner display")
        if l_member:
            try:
                await update_member_display(l_member)
            except Exception:
                logger.exception("Failed updating loser display")

    # cleanup match mappings
    in_match.pop(winner_id, None)
    in_match.pop(loser_id, None)
    pending_result.pop(winner_id, None)

    # post result message
    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt ／ <@{loser_id}> に {delta_l}pt の反映を行いました。")

# ------------------------------
# コマンド: /マッチ希望
# ------------------------------
@tree.command(name="マッチ希望", description="ランダムマッチに参加します（相手指定不要）")
async def cmd_match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    # Already in match?
    if uid in in_match:
        opp = in_match[uid]
        await interaction.response.send_message(f"現在 <@{opp}> とマッチ中です。試合完了後に再度お試しください。", ephemeral=True)
        return
    # Already pending result?
    if uid in pending_result.values() or uid in pending_result.keys():
        await interaction.response.send_message("あなたは現在結果承認待ちの試合があります。処理が終わるまでお待ちください。", ephemeral=True)
        return
    # Already in hope_list?
    if uid in hope_list:
        await interaction.response.send_message("既にマッチ希望リストに登録されています。/マッチ希望取下げ で取り下げ可能です。", ephemeral=True)
        return
    # store origin channel
    origin_channel_id = interaction.channel.id if interaction.channel else None
    user_origin_channel[uid] = origin_channel_id

    # add to hope_list and draw_group
    hope_list[uid] = {"ts": now(), "origin_channel_id": origin_channel_id}
    draw_group.add(uid)

    # schedule draw with delay (reset on each new entrant)
    await schedule_draw_after_delay()

    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)

# ------------------------------
# コマンド: /マッチ希望取下げ
# ------------------------------
@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます（5分タイマー中のみ）")
async def cmd_cancel_match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in hope_list:
        await interaction.response.send_message("あなたは現在マッチ希望登録されていません。", ephemeral=True)
        return
    # remove from hope_list and draw_group
    hope_list.pop(uid, None)
    draw_group.discard(uid)
    await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)

# ------------------------------
# コマンド: /結果報告 (勝者が実行)
# ------------------------------
@tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    # validate match exists
    if in_match.get(winner.id) != loser.id or in_match.get(loser.id) != winner.id:
        await interaction.response.send_message("このマッチングは登録されていません。まずは /マッチ希望 をお願いします。", ephemeral=True)
        return
    # create view and send public message in the channel where command invoked
    origin_channel_id = interaction.channel.id if interaction.channel else None
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    view = ResultApproveView(winner.id, loser.id, origin_channel_id)
    # send message publicly in the channel (as per user's instruction)
    await interaction.response.send_message(content, view=view)
    # mark pending result so others cannot re-report
    pending_result[winner.id] = loser.id

# ------------------------------
# コマンド: /ランキング (誰でも)
# ------------------------------
@tree.command(name="ランキング", description="現在のランキングを表示します（誰でも実行可）")
async def cmd_show_ranking(interaction: discord.Interaction):
    # build user list from guild members (use bot guild)
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            await interaction.response.send_message("ギルドが見つかりません。", ephemeral=True)
            return
        # collect users with pt (if not present, treat as 0)
        users_pts = []
        for member in guild.members:
            if member.bot:
                continue
            uid = member.id
            pt = user_data.get(uid, {}).get("pt", 0)
            users_pts.append((uid, pt))
        ranking = standard_competition_ranking(users_pts)
        if not ranking:
            await interaction.response.send_message("ランキングデータがありません。", ephemeral=True)
            return
        lines = ["🏆 ランキング"]
        last_rank = None
        for rank, uid, pt in ranking:
            member = guild.get_member(uid)
            display = member.display_name if member else f"<@{uid}>"
            lines.append(f"{rank}位 {display} {pt}pt")
        await interaction.response.send_message("\n".join(lines))
    except Exception:
        logger.exception("Failed to build ranking")
        await interaction.response.send_message("ランキングの表示に失敗しました。", ephemeral=True)

# ------------------------------
# 管理者コマンド: /admin_set_pt
# - 管理者のみ
# - ユーザーのptを任意に設定（pt に応じてロール/ニックネームを反映）
# ------------------------------
@tree.command(name="admin_set_pt", description="[Admin] ユーザーのPTを設定します")
@app_commands.describe(member="対象メンバー", pt="設定するPT（0以上の整数）")
async def cmd_admin_set_pt(interaction: discord.Interaction, member: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    if pt < 0:
        await interaction.response.send_message("PTは0以上を指定してください。", ephemeral=True)
        return
    user_data.setdefault(member.id, {})["pt"] = pt
    # update member display and roles
    try:
        await update_member_display(member)
    except Exception:
        logger.exception("Failed to update member display for admin_set_pt")
    await interaction.response.send_message(f"{member.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# ------------------------------
# 管理者コマンド: /admin_reset_all
# - 全ユーザーのPTと表示を初期化（0ptに）
# ------------------------------
@tree.command(name="admin_reset_all", description="[Admin] 全ユーザーのPTを0にリセットします")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await interaction.response.send_message("ギルドが見つかりません。", ephemeral=True)
        return
    # reset internal data
    for member in guild.members:
        if member.bot:
            continue
        user_data.setdefault(member.id, {})["pt"] = 0
        try:
            await update_member_display(member)
        except Exception:
            logger.exception("Failed to update member during reset_all")
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

# ------------------------------
# Utility: periodically cleanup expired hope_list entries
# ------------------------------
@tasks.loop(seconds=60)
async def periodic_cleanup():
    try:
        cleanup_expired_hopes()
    except Exception:
        logger.exception("periodic cleanup error")

# ------------------------------
# on_ready: sync commands and start background tasks
# ------------------------------
@bot.event
async def on_ready():
    logger.info(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    # sync commands to guild only (avoid global delay)
    try:
        guild = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild)
        logger.info("Commands synced to guild.")
    except Exception:
        logger.exception("Failed to sync commands to guild")
    # start periodic cleanup if not running
    if not periodic_cleanup.is_running():
        periodic_cleanup.start()

# ------------------------------
# エラーハンドリング: app_commands のエラーをキャッチして返信
# ------------------------------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    logger.exception("Command error: %s", error)
    try:
        await interaction.response.send_message("コマンドの実行中にエラーが発生しました。", ephemeral=True)
    except Exception:
        # 既に応答済みなど
        pass

# ------------------------------
# 起動
# ------------------------------
if __name__ == "__main__":
    # run the bot
    bot.run(TOKEN)
