# -*- coding: utf-8 -*-
"""
基本 main.py
2025-10-21 時点での完全版（JSON保存）
このファイルはそのままコピペで動かせることを目標とした1ファイル実装です。
事前準備（Railway Variables）:
 - DISCORD_TOKEN
 - GUILD_ID (int)
 - ADMIN_ID (int)
 - RANKING_CHANNEL_ID (int)  # optional
 - JUDGE_CHANNEL_ID (int)    # optional

仕様（要点）:
 - JSONで user_data を保存 (users.json)
 - ランク帯とアイコンは固定 (Beginner..Challenger)
 - /マッチ申請 (相手を指定) -> 相手に承認ボタン（相手のみ押せる）
 - /結果報告 (勝者申告) -> 敗者の承認 or 異議 -> 承認時にpt更新, ロール & ニックネーム更新
 - 管理者コマンド: /admin_reset_all, /admin_set_pt, /admin_show_ranking
 - ランキング投稿コマンド実装
 - 自動承認タイマー: 15分
 - 保存/読み込みは安全に行う
 - Discord側のロールは既に用意されている想定 (Beginner,Silver,Gold,Master,GroundMaster,Challenger)

注意: このファイルは"基本 main.py"仕様に沿った実装です。
"""

import os
import json
import asyncio
import logging
from typing import Dict, Optional
import datetime
import pytz

import discord
from discord import app_commands
from discord.ext import tasks

# -----------------------
# ログ設定
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("matchbot")

# -----------------------
# 環境変数 / 定数
# -----------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID")) if os.environ.get("GUILD_ID") else None
ADMIN_ID = int(os.environ.get("ADMIN_ID")) if os.environ.get("ADMIN_ID") else None
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

DATA_FILE = "users.json"
AUTO_APPROVE_SECONDS = 15 * 60  # 15分
RANK_ICON_MAP = {
    "Beginner": "🔰",
    "Silver": "🥈",
    "Gold": "🥇",
    "Master": "⚔️",
    "GroundMaster": "🪽",
    "Challenger": "😈",
}

# ランク定義 (表示用): (start_pt, end_pt, role_name, icon)
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
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

# -----------------------
# データ構造（インメモリ）
# user_data: {user_id: {"pt":int, "role_name":str}}
# matching: {user_a: user_b, user_b: user_a}
# -----------------------
user_data: Dict[int, Dict] = {}
matching: Dict[int, int] = {}

# -----------------------
# Bot 初期化
# -----------------------
intents = discord.Intents.default()
intents.members = True

class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # guild限定でコマンド同期
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            try:
                await self.tree.sync(guild=guild)
                logger.info("Commands synced to guild.")
            except Exception as e:
                logger.exception("コマンド同期エラー:", exc_info=e)

client = MyBot()
bot = client  # alias

# -----------------------
# ヘルパー関数
# -----------------------

def load_data():
    global user_data
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                user_data = json.load(f)
                # keys are strings -> convert to int
                user_data = {int(k): v for k, v in user_data.items()}
                logger.info(f"Loaded {len(user_data)} users from {DATA_FILE}")
        else:
            user_data = {}
    except Exception as e:
        logger.exception("Failed to load data", exc_info=e)
        user_data = {}


def save_data():
    try:
        serializable = {str(k): v for k, v in user_data.items()}
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Failed to save data", exc_info=e)


def get_role_for_pt(pt: int) -> str:
    for start, end, name, icon in rank_roles:
        if start <= pt <= end:
            return name
    return "Beginner"


def get_icon_for_role(role_name: str) -> str:
    return RANK_ICON_MAP.get(role_name, "🔰")


def get_internal_rank(pt: int) -> int:
    for r, rng in rank_ranges_internal.items():
        if pt in rng:
            return r
    return 6


def calculate_pt(my_pt: int, other_pt: int, outcome: str) -> int:
    """
    outcome: "win" or "lose"
    Basic logic per internal rank difference
    """
    my_rank = get_internal_rank(my_pt)
    other_rank = get_internal_rank(other_pt)
    rank_diff = other_rank - my_rank  # positive = opponent is higher rank

    if outcome == "win":
        if rank_diff == 0:
            delta = 1
        elif rank_diff == 1:
            delta = 2
        elif rank_diff == 2:
            delta = 3
        else:
            # opponent much higher -> still allowed? default to +3
            delta = 3
        new = my_pt + delta
    else:  # lose
        if rank_diff == 0:
            delta = -1
        elif rank_diff > 0:
            delta = -1
        elif rank_diff < 0:
            # losing to lower rank costs more
            if rank_diff == -1:
                delta = -2
            elif rank_diff == -2:
                delta = -3
            else:
                delta = -3
        new = my_pt + delta
        if new < 0:
            new = 0
    # special: challenge boundaries handling (basic main.py rules)
    # If new surpasses 3,8,13,18,23 we keep it (basic main.py had not cutting here)
    return new


async def update_member_display(member: discord.Member):
    """
    - Update nickname to include icon and pt (if permitted)
    - Ensure role assignment matches pt (assumes roles already exist)
    """
    uid = member.id
    data = user_data.get(uid, {})
    pt = data.get("pt", 0)
    role_name = get_role_for_pt(pt)

    # update nickname: prefer original name + ' {icon} {pt}pt' format
    base_name = member.display_name
    # Attempt to strip existing suffix like ' 🔰 3pt' if present
    # We will naively remove last two tokens if they match pattern
    try:
        parts = base_name.rsplit(' ', 2)
        if len(parts) == 3 and parts[-1].endswith('pt'):
            base_core = parts[0]
        else:
            base_core = member.name
    except Exception:
        base_core = member.name

    new_nick = f"{base_core} {get_icon_for_role(role_name)} {pt}pt"
    # set nickname if different
    try:
        if member.guild.me.guild_permissions.manage_nicknames:
            if member.nick != new_nick:
                await member.edit(nick=new_nick)
    except Exception:
        logger.exception(f"Failed to set nickname for {member}")

    # Role assignment
    try:
        guild = member.guild
        # remove all managed rank roles then assign correct one
        target_role = discord.utils.get(guild.roles, name=role_name)
        if target_role:
            # remove other rank roles
            for _, _, rn, _ in rank_roles:
                r = discord.utils.get(guild.roles, name=rn)
                if r and r in member.roles and r != target_role:
                    try:
                        await member.remove_roles(r)
                    except Exception:
                        logger.exception("remove role failed")
            if target_role not in member.roles:
                try:
                    await member.add_roles(target_role)
                except Exception:
                    logger.exception("add role failed")
    except Exception:
        logger.exception("Failed role sync")


def is_registered_match(a:int, b:int) -> bool:
    return matching.get(a) == b and matching.get(b) == a

# -----------------------
# Views
# -----------------------
class ApproveMatchView(discord.ui.View):
    def __init__(self, applicant_id:int, opponent_id:int, origin_channel_id:int | None):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.opponent_id = opponent_id
        self.origin_channel_id = origin_channel_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("承認できるのは申請された相手のみです。", ephemeral=True)
            return
        # 成立
        matching[self.applicant_id] = self.opponent_id
        matching[self.opponent_id] = self.applicant_id
        # 公開メッセージを申請発行元チャンネルに流す (or current channel)
        guild = interaction.guild
        ch = None
        if self.origin_channel_id and guild:
            ch = guild.get_channel(self.origin_channel_id)
        if not ch:
            ch = interaction.channel
        if ch:
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
        if JUDGE_CHANNEL_ID and guild:
            judge_ch = guild.get_channel(JUDGE_CHANNEL_ID)
            if judge_ch:
                await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。\nこのマッチングは無効扱いとなっています。審議結果を @kurosawa0118 にご報告ください。")
        # マッチ情報は解除
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)

# -----------------------
# 承認時処理
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
    save_data()

    # 反映
    for g in client.guilds:
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
@client.tree.command(name="マッチ申請", description="対戦相手にマッチ申請を出します")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    # 自分が既にマッチ中
    if applicant.id in matching:
        existing_op = matching.get(applicant.id)
        view = CancelExistingMatchView(applicant.id, existing_op)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return

    # 相手が既にマッチ中
    if opponent.id in matching:
        existing_other = matching.get(opponent.id)
        view = CancelExistingMatchView(opponent.id, existing_other)
        await interaction.response.send_message("申請先は既にマッチ中です。取り消しますか？", view=view, ephemeral=True)
        return

    # ランク差制約
    applicant_pt = user_data.get(applicant.id, {}).get("pt", 0)
    opponent_pt = user_data.get(opponent.id, {}).get("pt", 0)
    if abs(get_internal_rank(applicant_pt) - get_internal_rank(opponent_pt)) >= 3:
        await interaction.response.send_message("申し訳ありません。ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return

    # チャレンジ中等の追加制約は基本版では無し（後の改版で追加）

    # 申請を相手へチャンネル投稿（DMはしない）
    view = ApproveMatchView(applicant.id, opponent.id, interaction.channel.id if interaction.channel else None)
    content = f"<@{opponent.id}> に <@{applicant.id}> からマッチ申請が届きました。承認してください。"
    try:
        # チャンネルに通知
        await interaction.channel.send(content, view=view)
    except Exception:
        await interaction.response.send_message("申請の通知に失敗しました。チャンネル権限等を確認してください。", ephemeral=True)
        return

    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)

# -----------------------
# コマンド: 結果報告（勝者が実行）
# -----------------------
@client.tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチング申請をお願いします。", ephemeral=True)
        return

    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    # 公開チャンネルで投稿（敗者の承認ボタンは敗者のみ押せる）
    view = ResultApproveView(winner.id, loser.id)
    try:
        await interaction.channel.send(content, view=view)
    except Exception:
        await interaction.response.send_message("承認通知の投稿に失敗しました。", ephemeral=True)
        return

    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)

    # 自動承認タスク
    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        if is_registered_match(winner.id, loser.id):
            # 自動承認扱い
            await handle_approved_result(winner.id, loser.id, interaction.channel)
    bot.loop.create_task(auto_approve_task())

# -----------------------
# 管理者コマンド
# -----------------------
async def admin_check(interaction: discord.Interaction) -> bool:
    if ADMIN_ID and interaction.user.id == ADMIN_ID:
        return True
    await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
    return False

@client.tree.command(name="admin_reset_all", description="全ユーザーのPTと表示を初期化します（管理者限定）")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if not await admin_check(interaction):
        return
    # reset all pts
    for uid in list(user_data.keys()):
        user_data[uid]["pt"] = 0
    save_data()
    # reflect to guild
    guild = client.get_guild(GUILD_ID) if GUILD_ID else None
    if guild:
        for m in guild.members:
            if m.bot:
                continue
            try:
                await update_member_display(m)
            except Exception:
                logger.exception("failed update member")
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

@client.tree.command(name="admin_set_pt", description="(管理者用) 指定ユーザーのPTを設定します")
@app_commands.describe(user="対象ユーザー", pt="設定するptの値(整数)")
async def cmd_admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if not await admin_check(interaction):
        return
    if pt < 0:
        await interaction.response.send_message("ptは0以上で指定してください。", ephemeral=True)
        return
    user_data.setdefault(user.id, {})["pt"] = pt
    save_data()
    await update_member_display(user)
    await interaction.response.send_message(f"{user.display_name} のPTを {pt} に設定しました。", ephemeral=True)

@client.tree.command(name="admin_show_ranking", description="ランキングを表示します（管理者限定）")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if not await admin_check(interaction):
        return
    # build ranking
    # convert to list of (uid, pt)
    items = [(uid, data.get("pt", 0)) for uid, data in user_data.items()]
    items.sort(key=lambda x: x[1], reverse=True)
    if not items:
        await interaction.response.send_message("ランキングに登録されたユーザーがいません。", ephemeral=True)
        return
    lines = ["🏆 ランキング"]
    rank = 1
    prev_pt = None
    display_rank = 1
    for uid, pt in items:
        member = None
        guild = client.get_guild(GUILD_ID) if GUILD_ID else None
        if guild:
            member = guild.get_member(uid)
        name = member.display_name if member else str(uid)
        if prev_pt is None:
            display_rank = rank
        else:
            if pt < prev_pt:
                display_rank = rank
        lines.append(f"{display_rank}位 {name} {get_icon_for_role(get_role_for_pt(pt))} {pt}pt")
        prev_pt = pt
        rank += 1
    await interaction.response.send_message("\n".join(lines), ephemeral=False)

# -----------------------
# 汎用コマンド: ランキング（誰でも可）
# -----------------------
@client.tree.command(name="ランキング", description="現在のランキングを表示します")
async def cmd_show_ranking(interaction: discord.Interaction):
    items = [(uid, data.get("pt", 0)) for uid, data in user_data.items()]
    items.sort(key=lambda x: x[1], reverse=True)
    if not items:
        await interaction.response.send_message("ランキングに登録されたユーザーがいません。", ephemeral=True)
        return
    lines = ["🏆 ランキング"]
    rank = 1
    prev_pt = None
    display_rank = 1
    for uid, pt in items:
        member = None
        guild = client.get_guild(GUILD_ID) if GUILD_ID else None
        if guild:
            member = guild.get_member(uid)
        name = member.display_name if member else str(uid)
        if prev_pt is None:
            display_rank = rank
        else:
            if pt < prev_pt:
                display_rank = rank
        lines.append(f"{display_rank}位 {name} {get_icon_for_role(get_role_for_pt(pt))} {pt}pt")
        prev_pt = pt
        rank += 1
    await interaction.response.send_message("\n".join(lines), ephemeral=False)

# -----------------------
# タスク: 自動投稿 (基本版は無効化しておく。改版で有効化)
# -----------------------
# (基本 main.py では自動投稿は運用側で起動するようにしていたためここではコメントアウト)

# -----------------------
# 永続化と起動処理
# -----------------------
@client.event
async def on_ready():
    logger.info(f"{client.user} is ready. Guilds: {[g.name for g in client.guilds]}")
    load_data()

# -----------------------
# 起動
# -----------------------
if __name__ == '__main__':
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN が設定されていません。")
        raise SystemExit(1)
    try:
        client.run(DISCORD_TOKEN)
    except Exception as e:
        logger.exception("Bot 起動エラー", exc_info=e)
