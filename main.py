# main.py
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands

# 環境変数
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# 固定チャンネル（必要なら環境変数化）
RANKING_CHANNEL_ID = 1427542200614387846
JUDGE_CHANNEL_ID = 1427543619820191744

# 自動承認秒（15分）
AUTO_APPROVE_SECONDS = 15 * 60

# Intents
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

# ----------------------------------------
# データ構造（メモリ上）
# ----------------------------------------
# user_data: { user_id: {"pt": int} }
user_data: dict[int, dict] = {}

# matching: { user_id: opponent_id } 双方向で保持
matching: dict[int, int] = {}

# マッチ申請承認待ち用ロック（報告者->敗者向けの承認ビュー生成管理）
# (勝者id, 敗者id) のペアを一時的に保持する必要は matching で済む
# ----------------------------------------

# ----------------------------------------
# ランク定義（表示用）
# 各タプル: (start_pt, end_pt, role_name, icon_for_display)
# Challenge1 / Challenge2 を個別に扱う
# ----------------------------------------
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
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000),# 25+
}

# 昇級チャレンジの例外ポイント（降格戻しの処理に使用）
# 敗北時に元のptがキーの場合、負けたら戻るpt（仕様どおり）
loss_revert_map = {
    3: 2, 4: 2,
    8: 7, 9: 7,
    13: 12, 14: 12,
    18: 17, 19: 17,
    23: 22, 24: 22,
}

# 切り捨て対象（超過時に切り捨てされる上限点）
cut_thresholds = [3, 8, 13, 18, 23]

# ----------------------------------------
# ユーティリティ関数
# ----------------------------------------
def get_display_icon_and_role(pt: int) -> tuple[str, str]:
    for start, end, role_name, icon in rank_roles:
        if start <= pt <= end:
            return icon, role_name
    return "😈", "Challenger"

def get_internal_rank(pt: int) -> int:
    for rk, rng in rank_ranges_internal.items():
        if pt in rng:
            return rk
    return 6

def is_challenge_pt(pt:int) -> bool:
    return pt in {3,4,8,9,13,14,18,19,23,24}

# ----------------------------------------
# ユーザー表示更新（ニックネーム + ロール）
# 必ずPTを user_data に書き込んだ後に呼ぶこと
# ----------------------------------------
async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    icon, role_name = get_display_icon_and_role(pt)
    # ニックネーム更新: 元の表示名の最初のトークン（元のname）を使う（代替: member.display_name.split(' ')[0]）
    base_name = member.name
    new_nick = f"{base_name} {icon} {pt}pt"
    try:
        # 変更が無意味な場合はDiscordが例外を投げる可能性があるので捕捉
        await member.edit(nick=new_nick)
    except discord.Forbidden:
        # 権限不足で変更できない場合は無視
        pass
    except Exception:
        pass

    # ロール付け替え：まず既存のランク系ロールを削除してから付与
    guild = member.guild
    # collect rank role objects
    rank_role_objs = []
    for _, _, rname, _ in rank_roles:
        role = discord.utils.get(guild.roles, name=rname)
        if role:
            rank_role_objs.append(role)
    # remove any rank role present
    try:
        to_remove = [r for r in rank_role_objs if r in member.roles]
        if to_remove:
            await member.remove_roles(*to_remove)
    except Exception:
        pass
    # add target role
    target_role = discord.utils.get(guild.roles, name=role_name)
    if target_role:
        try:
            await member.add_roles(target_role)
        except Exception:
            pass

# ----------------------------------------
# PT 計算ロジック（内部rank差ベース + 例外処理）
# - result: "win" or "lose"
# ルール:
# - rank差 >=3 -> マッチ不可（マッチ前にチェック）
# - 同rank: win +1 / lose -1
# - +1rank: win +2 / lose -1
# - +2rank: win +3 / lose -1
# - -1rank: win +1 / lose -2
# - -2rank: win +1 / lose -3
# - 計算後、勝利での超過(>cut_threshold)がある場合は切り捨て（例: new_pt > 3 -> new_pt = 3）
# - 敗北で元のptが例外（3/4/...）なら loss_revert_map を使って戻す
# ----------------------------------------
def calculate_pt(user_pt: int, opponent_pt: int, result: str) -> int:
    user_internal = get_internal_rank(user_pt)
    opp_internal = get_internal_rank(opponent_pt)
    rank_diff = opp_internal - user_internal  # positive => opponent is higher internal rank

    # default change
    change = 0
    if result == "win":
        if rank_diff >= 3:
            # should not happen because matching prevents it
            change = 0
        elif rank_diff == 2:
            change = 3
        elif rank_diff == 1:
            change = 2
        elif rank_diff == 0:
            change = 1
        elif rank_diff == -1:
            change = 1
        elif rank_diff == -2:
            change = 1
    elif result == "lose":
        if rank_diff >= 3:
            change = -1
        elif rank_diff == 2:
            change = -1
        elif rank_diff == 1:
            change = -1
        elif rank_diff == 0:
            change = -1
        elif rank_diff == -1:
            change = -2
        elif rank_diff == -2:
            change = -3

    new_pt = user_pt + change

    # 敗北時に、元のptがチャレンジ例外の値なら戻しを適用
    if result == "lose" and user_pt in loss_revert_map:
        # 規定どおり敗北で pt は指定値に戻る
        return loss_revert_map[user_pt]

    # 勝利／敗北にかかわらず、超過切り捨てルール適用:
    # 「3,8,13,18,23 を超過する際は超過分は切り捨て」
    # つまり new_pt が対象の値を超えていたら対象値に切り捨て
    for t in cut_thresholds:
        if new_pt > t:
            new_pt = t

    # new_pt は最低 0
    if new_pt < 0:
        new_pt = 0

    return new_pt

# ----------------------------------------
# マッチ関連ユーティリティ
# ----------------------------------------
def is_registered_match(a: int, b: int) -> bool:
    return matching.get(a) == b and matching.get(b) == a

# ----------------------------------------
# ランキング定期投稿（JST 13:00 / 22:00）
# 毎分起動して時刻をチェックする (簡易実装)
# ----------------------------------------
@tasks.loop(seconds=60)
async def ranking_task():
    now = datetime.utcnow() + timedelta(hours=9)  # JST
    if now.minute != 0:
        return
    if now.hour not in (13, 22):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(RANKING_CHANNEL_ID)
    if not ch:
        return
    # build ranking
    entries = sorted(user_data.items(), key=lambda x: x[1].get("pt", 0), reverse=True)
    lines = ["🏆 ランキング 🏆"]
    for uid, data in entries:
        member = guild.get_member(uid)
        if not member:
            continue
        pt = data.get("pt", 0)
        icon, _ = get_display_icon_and_role(pt)
        lines.append(f"{member.display_name} {icon} {pt}pt")
    await ch.send("\n".join(lines))

# ----------------------------------------
# Views: マッチ申請承認 / 取り消し / 結果承認ビュー etc
# ----------------------------------------
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
        matching[self.applicant_id] = self.opponent_id
        matching[self.opponent_id] = self.applicant_id
        # 公開メッセージを申請発行元チャンネルに流す
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
        # 取り消しはボタン押した人ができる（申請者）
        # 解除処理
        a = self.existing_a
        b = self.existing_b
        if matching.get(a) == b:
            matching.pop(a, None)
            matching.pop(b, None)
            # 通知: 双方にメンション
            await interaction.response.send_message(f"<@{a}> と <@{b}> のマッチングは解除されました。", ephemeral=False)
        else:
            await interaction.response.send_message("該当のマッチは既に解除されています。", ephemeral=True)
        self.stop()

# 勝者の報告に対する敗者承認ビュー
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
        # 審判チャンネルに投稿（管理者に知らせる）
        guild = interaction.guild
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID)
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。@<@{ADMIN_ID}> に連絡してください。")
        # マッチ情報は削除（審議により管理者が手動で処理）
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)

# ----------------------------------------
# 承認時の実処理（勝者申告→敗者承認 or 自動承認→実際のpt更新）
# ----------------------------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    # マッチ登録チェック
    if not is_registered_match(winner_id, loser_id):
        await channel.send("このマッチングは登録されていません。まずはマッチング申請をお願いします。")
        return

    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt  = user_data.get(loser_id, {}).get("pt", 0)

    # 計算
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new  = calculate_pt(loser_pt, winner_pt, "lose")

    # 書き込み
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id,  {})["pt"] = loser_new

    # 反映（全ギルドの対象メンバーに反映）
    for g in bot.guilds:
        w_member = g.get_member(winner_id)
        l_member = g.get_member(loser_id)
        if w_member:
            await update_member_display(w_member)
        if l_member:
            await update_member_display(l_member)

    # マッチ解除
    matching.pop(winner_id, None)
    matching.pop(loser_id, None)

    # 結果メッセージ
    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

# ----------------------------------------
# コマンド: マッチ申請
# - 申請者が /マッチ申請 対戦相手 を実行
# - 相手にDMで承認ボタンを送る（DM不可はチャンネルで代替）
# - 既に誰かとマッチ中（matching に登録済）の相手へ申請する場合は
#   「すでにマッチ成立済みの試合は取り消しますか？」 と申請者に表示し取り消しボタンを表示。
# ----------------------------------------
@bot.tree.command(name="マッチ申請", description="対戦相手にマッチ申請を出します")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    applicant = interaction.user
    # 既に自分がマッチ中か
    if applicant.id in matching:
        # 申請者側に取り消し表示（申請者は既にマッチ中なので、そのマッチを解除するか問う）
        existing_op = matching.get(applicant.id)
        view = CancelExistingMatchView(applicant.id, existing_op)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return

    # 相手が既にマッチ中なら、申請者に取り消しの選択肢を表示
    if opponent.id in matching:
        existing_other = matching.get(opponent.id)
        # 申請者に取り消しボタンを出す（申請者が取り消すとその相手のマッチをキャンセル）
        view = CancelExistingMatchView(opponent.id, existing_other)
        await interaction.response.send_message("すでにマッチ成立済みの試合は取り消しますか？", view=view, ephemeral=True)
        return

    # マッチング制約: 3ランク差以上は不可（内部ランク差）
    applicant_pt = user_data.get(applicant.id, {}).get("pt", 0)
    opponent_pt = user_data.get(opponent.id, {}).get("pt", 0)
    if abs(get_internal_rank(applicant_pt) - get_internal_rank(opponent_pt)) >= 3:
        await interaction.response.send_message("申し訳ありません。ランク差が大きすぎてマッチングできません。", ephemeral=True)
        return

    # チャレンジ中のPTによる制約（3,4,8,9,... のときの追加制約）
    # 3,8,13,18,23 のときは自身と「同pt以上の相手」とのみマッチ
    # 4,9,14,19,24 のときは自身と「同pt-1 か 同pt以上」の相手とのみマッチ
    def challenge_match_ok(my_pt, other_pt):
        if my_pt in (3,8,13,18,23):
            return other_pt >= my_pt
        if my_pt in (4,9,14,19,24):
            return (other_pt >= my_pt) or (other_pt == my_pt - 1)
        return True

    if not challenge_match_ok(applicant_pt, opponent_pt):
        await interaction.response.send_message("昇級チャレンジ状態のため、同pt以上の相手としかマッチできません。", ephemeral=True)
        return
    if not challenge_match_ok(opponent_pt, applicant_pt):
        await interaction.response.send_message(f"{opponent.display_name} は昇級チャレンジ状態のため、この申請はできません。", ephemeral=True)
        return

    # 申請メッセージを相手に送る（DM があればDM、なければチャンネル）
    view = ApproveMatchView(applicant.id, opponent.id, interaction.channel.id if interaction.channel else None)
    content = f"<@{opponent.id}> に {applicant.display_name} からマッチ申請が届きました。承認してください。"
    sent = None
    try:
        sent = await opponent.send(content, view=view)
    except Exception:
        # DM 拒否ならチャンネルに置く（パブリック）
        channel = interaction.channel
        if channel:
            sent = await channel.send(content, view=view)
    await interaction.response.send_message(f"{opponent.display_name} にマッチング申請しました。承認を待ってください。", ephemeral=True)

# ----------------------------------------
# コマンド: 結果報告（勝者が実行）
# - 申請済みのマッチであることを確認
# - 敗者へ承認/異議ボタンを送る（敗者のみ操作可）
# - 敗者が承認するか15分経過で自動承認され、PT適用→ロール反映
# - 敗者が異議を押したら審議チャンネルへ通知、マッチ解除
# ----------------------------------------
@bot.tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent

    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチング申請をお願いします。", ephemeral=True)
        return

    # 敗者への承認ビューを送信
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    sent_msg = None
    try:
        sent_msg = await loser.send(content, view=ResultApproveView(winner.id, loser.id))
    except Exception:
        # DM拒否ならギルドの同じチャンネルに投稿して承認を待つ（承認ボタンは同じく敗者のみ押せる）
        ch = interaction.channel
        sent_msg = await ch.send(content, view=ResultApproveView(winner.id, loser.id))

    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)

    # 自動承認タスク
    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        # 再チェック
        if is_registered_match(winner.id, loser.id):
            # 実行
            await handle_approved_result(winner.id, loser.id, interaction.channel)
    bot.loop.create_task(auto_approve_task())

# ----------------------------------------
# 管理者コマンド: PT一括設定（今回はPTのみ管理）
# /admin_set_pt target pt
# ----------------------------------------
@bot.tree.command(name="admin_set_pt", description="管理者専用: ユーザーのPTを変更（PTに応じてロールと表示は自動更新）")
@app_commands.describe(target="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, target: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    if pt < 0:
        await interaction.response.send_message("PTは0以上で指定してください。", ephemeral=True)
        return
    # 整合チェック：ptとロールの不整合は起きない前提だが、過度な値は弾く
    if pt > 10000:
        await interaction.response.send_message("不正なPTです。", ephemeral=True)
        return
    user_data.setdefault(target.id, {})["pt"] = pt
    await update_member_display(target)
    await interaction.response.send_message(f"{target.display_name} のPTを {pt} に設定しました。", ephemeral=True)

# ----------------------------------------
# 管理者コマンド: 個別初期化 / 全体初期化
# ----------------------------------------
@bot.tree.command(name="admin_reset_user", description="管理者専用: 指定ユーザーのPTと表示を初期化")
@app_commands.describe(target="対象ユーザー")
async def cmd_admin_reset_user(interaction: discord.Interaction, target: discord.Member):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    user_data[target.id] = {"pt": 0}
    await update_member_display(target)
    await interaction.response.send_message(f"{target.display_name} を初期化しました。", ephemeral=True)

@bot.tree.command(name="admin_reset_all", description="管理者専用: 全ユーザーのPTを初期化")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    guild = interaction.guild
    for m in guild.members:
        if m.bot:
            continue
        user_data[m.id] = {"pt": 0}
        await update_member_display(m)
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

# ----------------------------------------
# 管理者コマンド: ランキング手動表示
# ----------------------------------------
@bot.tree.command(name="admin_show_ranking", description="管理者専用: 任意のタイミングでランキングを表示")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    guild = interaction.guild
    entries = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    lines = ["🏆 ランキング（手動）🏆"]
    for uid, data in entries:
        member = guild.get_member(uid)
        if not member:
            continue
        pt = data.get("pt", 0)
        icon, _ = get_display_icon_and_role(pt)
        lines.append(f"{member.display_name} {icon} {pt}pt")
    await interaction.response.send_message("\n".join(lines))

# ----------------------------------------
# 起動時処理
# - コマンド同期（ギルド単位）
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")
    # sync guild commands to the configured guild
    try:
        bot.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
        asyncio.create_task(bot.tree.sync(guild=discord.Object(id=GUILD_ID)))
    except Exception:
        pass
    # start ranking task
    if not ranking_task.is_running():
        ranking_task.start()

# ----------------------------------------
# 実行
# ----------------------------------------
if __name__ == "__main__":
    if DISCORD_TOKEN is None:
        print("[ERROR] DISCORD_TOKEN が未設定です。")
    else:
        bot.run(DISCORD_TOKEN)
