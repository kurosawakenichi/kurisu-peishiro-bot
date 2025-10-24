import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
from typing import Dict, List, Optional, Tuple
import time

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)

# ----------- 定数 / 環境変数 -----------
GUILD_ID = int(os.getenv("GUILD_ID"))
JUDGE_CHANNEL_ID = int(os.getenv("JUDGE_CHANNEL_ID"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", 0))  # 必須
MATCH_TIMEOUT = 300  # 5分（=300秒）

# ----------- ユーザーデータ管理 -----------
# ユーザーのpt（メモリ保持 / 将来的に永続化可）
player_pt: Dict[int, int] = {}

# ランダムマッチ待機（希望リスト）
waiting_list: Dict[int, float] = {}

# 現在のin-match（成立ペア）
# {user_id : opponent_id, opponent_id : user_id}
in_match: Dict[int, int] = {}

# ロック（並列制御）
waiting_lock = asyncio.Lock()
match_lock = asyncio.Lock()

import random
from collections import defaultdict

# ----------------------------------------
# ランク定義（表示用）
# 0-4 Beginner 🔰
# 5-9 Silver 🥈
# 10-14 Gold 🥇
# 15-19 Master ⚔️
# 20-24 GroundMaster 🪽
# 25+ Challenger 😈
# ----------------------------------------
rank_roles_display = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, 99999, "Challenger", "😈"),
]

# 内部ランク階層（rank1..rank6） : マッチ判定と簡略化用
rank_ranges_internal = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000),# 25+
}

def get_display_for_pt(pt: int) -> Tuple[str, str]:
    """ptに対応する (role_name, icon) を返す。"""
    for s, e, name, icon in rank_roles_display:
        if s <= pt <= e:
            return (name, icon)
    return ("Challenger", "😈")

def get_internal_rank(pt: int) -> int:
    """ptから内部rank (1..6) を返す"""
    for r, rng in rank_ranges_internal.items():
        if pt in rng:
            return r
    return 6

async def safe_edit_nick(guild: discord.Guild, member: discord.Member, new_nick: Optional[str]):
    """安全にニックネームを変更するユーティリティ（例外を握る）"""
    try:
        # bot が対象ギルドでニックネームを変更できる権限を持つかをチェック
        me = guild.me
        if not me.guild_permissions.manage_nicknames:
            return False
        # 管理者（自身）に対しては変更できない場合もある
        await member.edit(nick=new_nick)
        return True
    except Exception:
        return False

async def update_member_display(member: discord.Member):
    """
    player_pt を参照してメンバーのニックネームを更新する。
    既存の表示（末尾に " {icon} {pt}pt" が付いているならそれを差し替える）
    基本表示名は member.display_name の "ベース部分" を使います（既にカスタムニックがあればそれをベースとします）。
    """
    guild = member.guild
    uid = member.id
    pt = player_pt.get(uid, 0)
    _, icon = get_display_for_pt(pt)

    # 既存の表示名（ニックネーム優先、なければ username）
    current = member.nick or member.name

    # 末尾に既に " <icon> <N>pt" の形式があるかを判定して切り取る
    # 例: "やんぐ 🔰 3pt"  -> ベース "やんぐ"
    #       "やんぐ" -> ベース "やんぐ"
    parts = current.split()
    # look back for pattern like '🔰' and '3pt'
    base_parts = parts[:]
    if len(parts) >= 2:
        last = parts[-1]
        second_last = parts[-2]
        if last.endswith("pt") and any(second_last == r[3] for r in rank_roles_display):
            base_parts = parts[:-2]
    base_name = " ".join(base_parts).strip()
    if base_name == "":
        base_name = member.name  # fallback

    new_nick = f"{base_name} {icon} {pt}pt"

    # Don't update if identical
    if (member.nick or member.name) == new_nick:
        return

    await safe_edit_nick(guild, member, new_nick)

def is_registered_match(a_id: int, b_id: int) -> bool:
    """a_id と b_id が in_match に登録済みか確認"""
    return in_match.get(a_id) == b_id and in_match.get(b_id) == a_id

def calculate_pt_for_result(winner_pt: int, loser_pt: int) -> Tuple[int, int]:
    """
    ライト仕様のpt計算（ランク差補正なし）：勝者 +1、敗者 -1（下限0）
    戻り値: (winner_new_pt, loser_new_pt)
    """
    w_new = winner_pt + 1
    l_new = max(0, loser_pt - 1)
    return (w_new, l_new)

import asyncio

# ----------------------------------------
# 内部リスト管理
# ----------------------------------------
# player_id -> 残りマッチ希望タイマー（秒）管理
match_request_timer = {}  

# 申請中リスト（抽選待ち）
match_waiting = set()

# 対戦中リスト (player_id -> opponent_id)
in_match = {}

# ロック：抽選処理の並列制御
waiting_lock = asyncio.Lock()

MATCH_WAIT_SECONDS = 5      # 抽選待機時間
MATCH_REQUEST_TIMEOUT = 300 # 5分

# ----------------------------------------
# /マッチ希望
# ----------------------------------------
@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def match_request(interaction: discord.Interaction):
    uid = interaction.user.id

    # 既に対戦中 or 申請済み
    if uid in in_match:
        await interaction.response.send_message("既に対戦中です。", ephemeral=True)
        return
    if uid in match_request_timer:
        await interaction.response.send_message("既にマッチ希望中です。", ephemeral=True)
        return

    # 希望リストに追加
    match_request_timer[uid] = interaction  # interactionを保持して後でephemeral返信可能
    match_waiting.add(uid)

    await interaction.response.send_message("マッチング中です…", ephemeral=True)

    async with waiting_lock:
        # 5秒待機（抽選ウィンドウ）
        await asyncio.sleep(MATCH_WAIT_SECONDS)

        # まだ抽選中に残っているユーザーを抽選対象に
        candidates = list(match_waiting)
        random.shuffle(candidates)

        paired = set()
        for i in range(0, len(candidates)-1, 2):
            a, b = candidates[i], candidates[i+1]

            # 階級差制限チェック（ライト仕様通り）
            if abs(get_internal_rank(player_pt.get(a,0)) - get_internal_rank(player_pt.get(b,0))) > 1:
                continue  # 組めない

            # 対戦成立
            in_match[a] = b
            in_match[b] = a
            paired.add(a)
            paired.add(b)

            # 希望リストから削除
            match_waiting.discard(a)
            match_waiting.discard(b)
            match_request_timer.pop(a, None)
            match_request_timer.pop(b, None)

            user_a = bot.get_user(a)
            user_b = bot.get_user(b)
            if user_a and user_b:
                msg = f"{user_a.mention} vs {user_b.mention} のマッチが成立しました。試合後、勝者が /結果報告 を行なってください"
                await interaction.channel.send(msg)

        # 余りは希望リストに残す（タイマー継続）
        for uid in match_waiting:
            # 5分後にタイムアウト
            async def timeout_task(u):
                await asyncio.sleep(MATCH_REQUEST_TIMEOUT)
                if u in match_request_timer:
                    match_waiting.discard(u)
                    match_request_timer.pop(u, None)
                    user = bot.get_user(u)
                    if user:
                        await interaction.channel.send(f"{user.mention} マッチング相手が見つかりませんでした。")
            asyncio.create_task(timeout_task(uid))

# ----------------------------------------
# /マッチ希望取下げ
# ----------------------------------------
@bot.tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます")
async def cancel_match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in match_request_timer:
        await interaction.response.send_message("マッチ希望はありません。", ephemeral=True)
        return

    # 希望リストから削除
    match_waiting.discard(uid)
    match_request_timer.pop(uid, None)
    await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)

from discord.ui import View, Button
from datetime import datetime, timedelta

# 対戦結果承認待ちリスト
pending_judgement = {}  # winner_id -> {"loser": loser_id, "timeout": datetime, "interaction": interaction}

JUDGE_TIMEOUT = 300  # 5分

# ----------------------------------------
# /結果報告
# ----------------------------------------
@bot.tree.command(name="結果報告", description="マッチング成立後の勝者申告")
async def report_result(interaction: discord.Interaction, opponent: discord.User):
    winner_id = interaction.user.id
    loser_id = opponent.id

    # マッチ成立確認
    if winner_id not in in_match or in_match[winner_id] != loser_id:
        await interaction.response.send_message(
            "このマッチングは登録されていません。まずはマッチ申請をお願いします。",
            ephemeral=True
        )
        return

    # マッチング成立中の2人を in_match から一旦削除
    in_match.pop(winner_id)
    in_match.pop(loser_id)

    # 審議用ビュー作成
    view = View(timeout=JUDGE_TIMEOUT)

    # 承認ボタン
    async def approve_callback(inter: discord.Interaction):
        # pt更新処理（ライト仕様: +1/-1）
        player_pt[winner_id] = player_pt.get(winner_id,0)+1
        player_pt[loser_id]  = max(player_pt.get(loser_id,0)-1, 0)

        await inter.response.send_message("結果を承認しました。", ephemeral=True)
        view.stop()
        pending_judgement.pop(winner_id, None)

    approve_button = Button(label="承認", style=discord.ButtonStyle.green)
    approve_button.callback = approve_callback
    view.add_item(approve_button)

    # 異議ボタン
    async def dispute_callback(inter: discord.Interaction):
        # 審議チャンネル通知
        channel = bot.get_channel(JUDGE_CHANNEL_ID)
        if channel:
            msg = f"⚖️ 審議依頼: <@{winner_id}> vs <@{loser_id}> に異議が出ました。\nこのマッチングは無効扱いとなっています。審議結果を @kurosawa0118 にご報告ください。"
            await channel.send(msg)

        await inter.response.send_message("異議を申請しました。管理者が確認します。", ephemeral=True)
        pending_judgement.pop(winner_id, None)
        view.stop()

    dispute_button = Button(label="異議", style=discord.ButtonStyle.red)
    dispute_button.callback = dispute_callback
    view.add_item(dispute_button)

    # 送信
    await interaction.response.send_message(
        f"{interaction.user.mention} の勝利を報告しました。管理者承認または異議が出るまでお待ちください。",
        ephemeral=True,
        view=view
    )

    # pending管理
    pending_judgement[winner_id] = {
        "loser": loser_id,
        "timeout": datetime.now() + timedelta(seconds=JUDGE_TIMEOUT),
        "interaction": interaction
    }

    # タイムアウト処理
    async def timeout_task():
        await asyncio.sleep(JUDGE_TIMEOUT)
        if winner_id in pending_judgement:
            # 期限切れは申請者勝利
            player_pt[winner_id] = player_pt.get(winner_id,0)+1
            player_pt[loser_id]  = max(player_pt.get(loser_id,0)-1, 0)
            try:
                await interaction.followup.send("承認期限が切れました。申請者の勝利として処理しました。", ephemeral=True)
            except:
                pass
            pending_judgement.pop(winner_id, None)
            view.stop()

    asyncio.create_task(timeout_task())
