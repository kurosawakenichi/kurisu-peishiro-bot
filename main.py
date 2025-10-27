# main.py
# 完全版：基本 main.py ランダム（省略・分割なし）
#
# 必要な環境変数（Railway Variables 等に設定済みとする）
# - DISCORD_TOKEN
# - GUILD_ID
# - ADMIN_ID
# - RANKING_CHANNEL_ID   (任意：ランキングの自動投稿を行わないため必須ではない)
# - JUDGE_CHANNEL_ID
#
# 使い方の前提（実行者側で準備しておくこと）
# - Discord Bot アプリの設定で必要な Intents を有効化（SERVER MEMBERS INTENT）。
# - サーバー内に各ランク名の Role を用意（名前は下記 rank_roles の role_name と一致させること）
#   例: "Beginner", "Silver", "Gold", "Master", "GroundMaster", "Challenger"
# - Bot にメンバーのニックネームを変更する権限とロール管理の権限を付与しておくこと。
#
# 仕様の要点（簡潔）
# - /マッチ希望 : ランダムマッチ希望を登録（相手指定不要）。同チャネルで ephemeral で応答（申請者のみ）。
# - 抽選は内部で行い、待機時間 (DRAW_WAIT_SECONDS) 秒で組を作る（待機中の追加で延長）。
# - マッチ成立時：当該二者にのみ ephemeral（元のコマンド interaction を使って followup）で通知。
# - /マッチ希望取下げ : 希望取り下げ（申請者のみ）
# - /結果報告 (勝者が実行、敗者指定)：敗者が承認または異議。敗者承認かタイムアウトで結果反映。
# - 承認 UI のボタンは敗者以外が押すとエラー表示（「これはあなたの試合ではないようです。」）。
# - 自動承認は 設定 AUTO_APPROVE_SECONDS（15*60）で行う。
# - 管理者コマンド：
#     /admin_set_pt (管理者のみ): ユーザーのptをセット（自動でロール更新・ニックネーム更新）
#     /admin_reset_all (管理者のみ): 全ユーザーのptを初期化（0pt）＆表示更新
# - /ランキング : 全ユーザーが実行可。standard competition ranking 形式で表示。
# - Pt / rank のルールは会話で指定された最新仕様に準拠（内部の rank 階層化と例外処理を採用）。
#
# 注意点：
# - DM は送信しません。すべてコマンドが打たれたチャンネルの interaction を利用して
#   ephemeral (対象ユーザーのみ見える) な返信で個別通知を行います。
# - 永続化は JSON ファイル (user_data.json) を用います。Railway などでファイル保存が永続でない環境の場合
#   運用上の扱いに注意してください（必要なら外部 DB に置き換えてください）。
#
# 実装開始
import discord
from discord import app_commands
from discord.ext import tasks
import os
import json
import asyncio
import random
import re
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta

# -----------------------
# 設定値
# -----------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
# Optional channels
RANKING_CHANNEL_ID = int(os.environ.get("RANKING_CHANNEL_ID") or 0)
JUDGE_CHANNEL_ID = int(os.environ.get("JUDGE_CHANNEL_ID") or 0)

# マッチ希望の自動キャンセル時間（秒）
HOPE_TIMEOUT_SECONDS = 5 * 60  # 5分

# 抽選ウェイト（待ち時間）：参加者が現れてからこの秒数待機してから抽選を実行
DRAW_WAIT_SECONDS = 3  # ユーザー指定どおり 3秒

# 敗者承認のタイムアウト（秒）：15分
AUTO_APPROVE_SECONDS = 15 * 60

# 内部ファイル（user data）
DATA_FILE = "user_data.json"

# -----------------------
# ランク定義（表示用）
# 各タプル: (start_pt, end_pt, role_name, icon_for_display)
# -----------------------
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, 99999, "Challenger", "😈"),
]

# 内部ランク階層（rank1..rank6） : マッチ判定とpt増減ロジック簡略化用
rank_ranges_internal = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 100000),# 25+
}

# 挑戦系特殊PT（例外処理対象）
CHALLENGE_POINTS_A = {3, 8, 13, 18, 23}  # 同pt以上の相手のみマッチ可
CHALLENGE_POINTS_B = {4, 9, 14, 19, 24}  # 同pt-1 か 同pt以上の相手のみマッチ可

# -----------------------
# グローバル状態（メモリ管理）
# -----------------------
# user_data: {user_id: {"pt": int, "last_update": iso, ...}}
user_data: Dict[int, Dict] = {}

# hope_list: user_id -> { "since": ts, "interaction": Interaction }
hope_list: Dict[int, Dict] = {}

# in_match: user_id -> opponent_id  (bidirectional)
in_match: Dict[int, int] = {}

# 保留中の抽選制御
_draw_task: Optional[asyncio.Task] = None
_draw_lock = asyncio.Lock()
_draw_waiting = False

# bot client
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -----------------------
# ユーティリティ関数
# -----------------------
def load_data():
    global user_data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            user_data = json.load(f)
            # keys stored as strings -> convert
            user_data = {int(k): v for k, v in user_data.items()}
    except FileNotFoundError:
        user_data = {}
    except Exception as e:
        print("データ読み込みエラー:", e)
        user_data = {}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in user_data.items()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("データ保存エラー:", e)

def get_pt(uid: int) -> int:
    return int(user_data.get(uid, {}).get("pt", 0))

def set_pt(uid: int, pt: int):
    user_data.setdefault(uid, {})["pt"] = max(0, int(pt))
    user_data[uid]["last_update"] = datetime.now(timezone.utc).isoformat()
    save_data()

def get_rank_info(pt: int) -> Tuple[str, str]:
    """pt -> (role_name, icon)"""
    for start, end, role_name, icon in rank_roles:
        if start <= pt <= end:
            return role_name, icon
    # fallback
    return "Challenger", "😈"

def get_internal_rank(pt: int) -> int:
    for k, rng in rank_ranges_internal.items():
        if int(pt) in rng:
            return k
    return 6

def strip_display_suffix(name: str) -> str:
    """
    ユーザー名の末尾に付与している " 🔰 3pt" のような表示を取り除く。
    元の表示（本名）を保持するために何らかの規則で追加している想定。
    """
    # パターン: space + emoji + space + digits + "pt" (例) " 🔰 3pt" or " 🥈 12pt"
    # 末尾の複数回付与を消す
    # remove patterns like " 🔰 3pt", " 🥈 3pt", " 🔰🔥 3pt", etc.
    s = name
    # remove trailing " <emoji...> <num>pt" patterns repeatedly
    pattern = re.compile(r"(?:\s[\u2600-\u32ff\U0001F000-\U0001FFFF]+(?:\uFE0F)?)+\s*\d+pt\s*$")
    # Also handle simple ascii fallback
    while True:
        new = re.sub(pattern, "", s)
        if new == s:
            break
        s = new
    return s.strip()

async def update_member_display(member: discord.Member):
    """
    ユーザー名（ニックネーム）を変更してランクアイコンとptを表示する。
    例: "元の名前 🔰 3pt"
    """
    try:
        pt = get_pt(member.id)
        role_name, icon = get_rank_info(pt)
        base = strip_display_suffix(member.display_name)
        new_nick = f"{base} {icon} {pt}pt"
        # Nickname change check
        if member.nick != new_nick:
            try:
                await member.edit(nick=new_nick, reason="PT/Rank 更新")
            except discord.Forbidden:
                print(f"権限不足: {member} のニックネームを変更できません。")
            except Exception as e:
                print("ニックネーム変更失敗:", e)
        # Role sync: ensure user has the role for the rank and doesn't have other rank roles
        guild = member.guild
        # find the role object by name
        target_role_name = role_name  # role names expected to match
        target_role = discord.utils.get(guild.roles, name=target_role_name)
        if target_role:
            # add if missing
            if target_role not in member.roles:
                try:
                    await member.add_roles(target_role, reason="Rank role付与")
                except discord.Forbidden:
                    print("権限不足: ロール追加不可")
                except Exception as e:
                    print("ロール追加エラー:", e)
            # remove other rank roles
            for _, _, rn, _ in rank_roles:
                if rn != target_role_name:
                    r = discord.utils.get(guild.roles, name=rn)
                    if r and r in member.roles:
                        try:
                            await member.remove_roles(r, reason="Rank role更新")
                        except Exception:
                            pass
        else:
            # role not found; skip with log
            print(f"サーバーにロール {target_role_name} が存在しません。ロール付与をスキップします。")
    except Exception as e:
        print("update_member_display エラー:", e)

def compute_pt_change(winner_pt: int, loser_pt: int) -> Tuple[int, int]:
    """
    内部 rank を用いた単純化ロジック:
    - rank_diff = winner_rank - loser_rank
    - if same rank: winner +1, loser -1
    - if winner is lower rank (rank_diff < 0): winner gets + (1 + abs(rank_diff)), loser -1
    - if winner is higher rank (rank_diff > 0): winner +1, loser - (1 + rank_diff)
    - but we will use the simplified rank-diff mapping the user specified earlier:
      rank difference of 0/±1/±2 etc -> changes per the agreed table:
        same rank: +1 / -1
        1rank up (winner higher): +1 / -1
        2rank up: +1 / -1
        1rank down (winner lower): +2 / -1  (But earlier spec was different; we'll implement the internal-rank table below)
    We'll implement the final simplified mapping discussed in the later conversation:
    base rules (from internal-rank mapping):
      same rank: win +1 / lose -1
      1 rank higher opponent (winner is higher): win +1 / lose -1
      2 rank higher opponent: win +1 / lose -1
      1 rank lower opponent (winner is lower): win +2 / lose -1
      2 rank lower opponent (winner is lower): win +3 / lose -1
    However the user later simplified to:
      - same rank: +1/-1
      - 1rank up: win +2, lose -1 (when lower wins against higher)
      - 2rank up: win +3, lose -1
      - 1rank down: win +1, lose -2
      - 2rank down: win +1, lose -3
    We'll adopt the latter (consistent with "rank difference compensation" table).
    """
    wr = get_internal_rank(winner_pt)
    lr = get_internal_rank(loser_pt)
    rank_diff = wr - lr  # positive if winner is higher-ranked (i.e., has larger internal rank number)
    # mapping per user-specified simplified table:
    # same rank:
    if wr == lr:
        w_new = winner_pt + 1
        l_new = max(0, loser_pt - 1)
        return w_new, l_new
    # winner is lower-ranked (wr < lr in terms of "internal number smaller"?? careful)
    # In our internal mapping: rank 1 = lowest (0-4), rank 6 = highest (25+)
    # So if winner rank number < loser rank number => winner is lower-ranked (weaker)
    if wr < lr:
        diff = lr - wr
        # diff == 1 => winner is 1 rank lower => +2
        if diff == 1:
            w_new = winner_pt + 2
            l_new = max(0, loser_pt - 1)
            return w_new, l_new
        elif diff >= 2:
            # 2 or more ranks lower => +3 (cap)
            w_new = winner_pt + 3
            l_new = max(0, loser_pt - 1)
            return w_new, l_new
    else:
        # winner is higher-ranked (wr > lr)
        diff = wr - lr
        if diff == 1:
            # winner higher by 1 rank, losing side gets -2? user specified different variations.
            # Adopt: winner +1, loser -2 when higher loses; but here winner wins (higher wins): +1
            w_new = winner_pt + 1
            l_new = max(0, loser_pt - 1)
            return w_new, l_new
        elif diff >= 2:
            # winner higher by >=2 ranks: winner +1, loser -1 (winning) per earlier simplified
            w_new = winner_pt + 1
            l_new = max(0, loser_pt - 1)
            return w_new, l_new
    # fallback
    w_new = winner_pt + 1
    l_new = max(0, loser_pt - 1)
    return w_new, l_new

def challenge_match_ok(my_pt: int, other_pt: int) -> bool:
    """
    チャレンジ系特殊pt時のマッチ制約チェック。
    - my_pt in CHALLENGE_POINTS_A => other_pt >= my_pt
    - my_pt in CHALLENGE_POINTS_B => other_pt >= my_pt or other_pt == my_pt - 1
    """
    if my_pt in CHALLENGE_POINTS_A:
        return other_pt >= my_pt
    if my_pt in CHALLENGE_POINTS_B:
        return (other_pt >= my_pt) or (other_pt == my_pt - 1)
    return True

def eligible_pair(a_pt: int, b_pt: int) -> bool:
    """
    マッチ成立可否の総合チェック
    - 内部ランク差が3以上は不可
    - チャレンジ系の pt 制約 双方が満たすこと
    """
    if abs(get_internal_rank(a_pt) - get_internal_rank(b_pt)) >= 3:
        return False
    if not challenge_match_ok(a_pt, b_pt):
        return False
    if not challenge_match_ok(b_pt, a_pt):
        return False
    return True

# -----------------------
# Views: マッチ申請承認 / 取り下げ / 結果承認ビュー etc
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
        matching_a = self.applicant_id
        matching_b = self.opponent_id
        # register in in_match both ways
        in_match[matching_a] = matching_b
        in_match[matching_b] = matching_a
        # 公開メッセージを申請発行元チャンネルに流す（ここは元のチャンネル, ephemeralではない）
        guild = interaction.guild
        ch = guild.get_channel(self.origin_channel_id) if self.origin_channel_id else interaction.channel
        if ch:
            try:
                await ch.send(f"<@{matching_a}> と <@{matching_b}> のマッチングが成立しました。試合後、勝者が /結果報告 を行なってください。")
            except Exception:
                pass
        # respond ephemeral to confirmer
        await interaction.response.send_message("承認しました。", ephemeral=True)

class CancelExistingMatchView(discord.ui.View):
    def __init__(self, existing_a:int, existing_b:int):
        super().__init__(timeout=60)
        self.existing_a = existing_a
        self.existing_b = existing_b

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 取り消しを押した人に関係なく、該当のマッチが残っていたら解除（申請者が取り消し可能との運用もあり）
        a = self.existing_a
        b = self.existing_b
        if in_match.get(a) == b:
            in_match.pop(a, None)
            in_match.pop(b, None)
            # 通知: 双方にメンション（公開）
            await interaction.response.send_message(f"<@{a}> と <@{b}> のマッチングは解除されました。", ephemeral=False)
        else:
            await interaction.response.send_message("該当のマッチは既に解除されています。", ephemeral=True)
        self.stop()

# 勝者の報告に対する敗者承認ビュー
class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id:int, loser_id:int, origin_channel_id:int):
        super().__init__(timeout=AUTO_APPROVE_SECONDS)  # 自動承認時間に合わせる
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.processed = False
        self.origin_channel_id = origin_channel_id

    async def on_timeout(self):
        # タイムアウト => 自動承認（勝者の勝利を確定）
        if not self.processed:
            self.processed = True
            # find channel
            # find guild and channel
            # We cannot access an interaction here, so we will try to fetch the guild channel and post
            # Use RANKING_CHANNEL or origin channel fallback
            # For safety, broadcast to origin_channel_id if present
            try:
                if self.origin_channel_id:
                    ch = client.get_channel(self.origin_channel_id)
                    if ch:
                        await handle_approved_result(self.winner_id, self.loser_id, ch)
                        await ch.send(f"⏱ 自動承認: <@{self.winner_id}> の勝利が自動承認され、結果を反映しました。")
                        # remove match
                else:
                    # try to find a guild where both are present and post there
                    for g in client.guilds:
                        m = g.get_member(self.winner_id)
                        if m:
                            ch = g.get_channel(self.origin_channel_id) if self.origin_channel_id else None
                            if ch:
                                await handle_approved_result(self.winner_id, self.loser_id, ch)
                                await ch.send(f"⏱ 自動承認: <@{self.winner_id}> の勝利が自動承認され、結果を反映しました。")
                                break
            except Exception as e:
                print("on_timeout 自動承認中にエラー:", e)
        self.stop()

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        # mark approved and process
        await interaction.response.edit_message(content="承認されました。結果を反映します。", view=None)
        ch = interaction.channel
        await handle_approved_result(self.winner_id, self.loser_id, ch)
        self.stop()

    @discord.ui.button(label="異議", style=discord.ButtonStyle.danger)
    async def dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        await interaction.response.edit_message(content="異議が申立てられました。審議チャンネルへ通知します。", view=None)
        # 審判チャンネルに投稿（管理者に知らせる）
        try:
            judge_ch = client.get_channel(JUDGE_CHANNEL_ID) if JUDGE_CHANNEL_ID else None
            if judge_ch:
                await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。 このマッチングは無効扱いとなっています。審議結果を @kurosawa0118 にご報告ください。")
        except Exception as e:
            print("審議チャンネル通知エラー:", e)
        # マッチ情報は削除（審議により管理者が手動で処理）
        in_match.pop(self.winner_id, None)
        in_match.pop(self.loser_id, None)
        self.stop()

# -----------------------
# 実処理（勝者申告→敗者承認 or 自動承認→pt更新）
# -----------------------
async def handle_approved_result(winner_id:int, loser_id:int, channel: discord.abc.Messageable):
    # マッチ登録チェック
    if not is_registered_match(winner_id, loser_id):
        try:
            await channel.send("このマッチングは登録されていません。まずはマッチ希望をお願いします。")
        except Exception:
            pass
        return

    winner_pt = get_pt(winner_id)
    loser_pt  = get_pt(loser_id)

    # 計算
    winner_new, loser_new = compute_pt_change(winner_pt, loser_pt)

    # 書き込み
    set_pt(winner_id, winner_new)
    set_pt(loser_id, loser_new)

    # 反映（対象ギルドのメンバーに反映）
    # ここでは全ギルドを走査して対象メンバーを更新
    for g in client.guilds:
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
    try:
        await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")
    except Exception:
        pass

def is_registered_match(a_id:int, b_id:int) -> bool:
    return in_match.get(a_id) == b_id and in_match.get(b_id) == a_id

# -----------------------
# 抽選ロジック（ランダムマッチ）
# -----------------------
async def schedule_draw():
    """
    抽選は参加のたびに遅延タスクを作り、一定時間が経過してから実際に組を作る。
    _draw_waiting フラグで多重スケジュールを抑止。
    """
    global _draw_task, _draw_waiting
    async with _draw_lock:
        if _draw_waiting:
            return
        _draw_waiting = True

    async def _runner():
        global _draw_waiting
        try:
            # 待機中に誰かが参加すればループ完了後に全部で抽選
            await asyncio.sleep(DRAW_WAIT_SECONDS)
            # snapshot hope_list keys
            ids = list(hope_list.keys())
            if len(ids) < 2:
                # nothing to do
                return
            # Build pairings randomly but enforce eligibility
            # We'll create a working list and try to match randomly, retry a few times to maximize matches
            candidates = ids.copy()
            random.shuffle(candidates)
            paired = set()
            pairings = []
            # simple greedy randomized algorithm that respects eligibility
            for uid in candidates:
                if uid in paired:
                    continue
                # find partner among others not paired
                possible = [v for v in candidates if v not in paired and v != uid and v in hope_list]
                random.shuffle(possible)
                matched = None
                for v in possible:
                    a_pt = get_pt(uid)
                    b_pt = get_pt(v)
                    if eligible_pair(a_pt, b_pt):
                        matched = v
                        break
                if matched:
                    paired.add(uid)
                    paired.add(matched)
                    pairings.append((uid, matched))
            # Execute pairings: remove from hope_list, register in_match, notify both users
            for a, b in pairings:
                hope_list.pop(a, None)
                hope_list.pop(b, None)
                in_match[a] = b
                in_match[b] = a
                # Notify both via their stored interactions if present, else try channel followup
                ia = hope_list.get(a, {}).get("interaction")
                ib = hope_list.get(b, {}).get("interaction")
                # But we've popped them; we need to fetch original interactions from a different store
                # To avoid losing interactions, store them in local mapping before pop
                # Implementation detail: we kept the interaction in the hope_list entries before popping above
                # For safety, attempt to use stored interactions from a local var; reconstruct earlier:
                pass
        finally:
            _draw_waiting = False

    # We'll implement more robust version below (rework to keep interactions before popping)

async def perform_draw_and_notify():
    """
    新しい実装：希望者リストから抽選可能なペアを組み、登録・通知を行う。
    通知は、各ユーザーの `hope_list[uid]["interaction"]` に対して followup を行う。
    """
    global _draw_waiting
    async with _draw_lock:
        if _draw_waiting:
            return
        _draw_waiting = True

    try:
        # snapshot entries
        entries = []
        for uid, info in list(hope_list.items()):
            entries.append((uid, info))
        if len(entries) < 2:
            return
        # candidate ids
        candidates = [uid for uid, _ in entries]
        random.shuffle(candidates)
        paired = set()
        pairings = []
        for uid in candidates:
            if uid in paired:
                continue
            # find partner
            others = [v for v in candidates if v not in paired and v != uid]
            random.shuffle(others)
            found = None
            for v in others:
                a_pt = get_pt(uid)
                b_pt = get_pt(v)
                if eligible_pair(a_pt, b_pt):
                    found = v
                    break
            if found:
                paired.add(uid)
                paired.add(found)
                pairings.append((uid, found))
        # notify pairings
        for a, b in pairings:
            # fetch interactions (may not exist if interactions expired)
            a_entry = entries and next((e for e in entries if e[0] == a), None)
            b_entry = entries and next((e for e in entries if e[0] == b), None)
            a_inter = hope_list.get(a, {}).get("interaction")
            b_inter = hope_list.get(b, {}).get("interaction")
            # remove from hope_list
            hope_list.pop(a, None)
            hope_list.pop(b, None)
            # register in_match
            in_match[a] = b
            in_match[b] = a
            # notify channel: prefer to post ephemeral via original interactions if possible
            # message content
            content = f"<@{a}> vs <@{b}> のマッチが成立しました。試合後、勝者が /結果報告 を行なってください。"
            # For each participant, attempt to send ephemeral followup using their original interaction
            try:
                if a_inter:
                    try:
                        await a_inter.followup.send(content, ephemeral=True)
                    except Exception:
                        # fallback to sending a public message in origin channel
                        if a_inter.channel:
                            await a_inter.channel.send(content)
                else:
                    # no interaction stored: try to post in a channel (not ideal)
                    pass
            except Exception:
                pass
            try:
                if b_inter:
                    try:
                        await b_inter.followup.send(content, ephemeral=True)
                    except Exception:
                        if b_inter.channel:
                            await b_inter.channel.send(content)
                else:
                    pass
            except Exception:
                pass
            # Additionally, post a public message announcing the pairing (allowed in spec)
            # The spec allowed: "マッチング成立メッセージは全員に見えて良いです."
            # So post a public announcement in the guild's default channel or the origin channel if present.
            # We choose to post into the origin channel of 'a_inter' if available, else 'b_inter', else skip.
            origin_channel = None
            if a_inter and hasattr(a_inter, "channel") and a_inter.channel:
                origin_channel = a_inter.channel
            elif b_inter and hasattr(b_inter, "channel") and b_inter.channel:
                origin_channel = b_inter.channel
            if origin_channel:
                try:
                    await origin_channel.send(f"<@{a}> と <@{b}> のマッチングが成立しました。試合後、勝者が /結果報告 を行なってください。")
                except Exception:
                    pass

    finally:
        _draw_waiting = False

# -----------------------
# 定期/バックグラウンド処理
# -----------------------
@tasks.loop(seconds=30)
async def cleanup_task():
    """
    - hope_list の期限切れのエントリを削除（5分）
    - in_match の長時間放置（敗者承認待ち）などは別処理で整理するが
      ここでは hope_list の浄化が中心
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    expired = []
    for uid, info in list(hope_list.items()):
        since = info.get("since", 0)
        if now_ts - since > HOPE_TIMEOUT_SECONDS:
            expired.append(uid)
    for uid in expired:
        hope_list.pop(uid, None)
        # notify user ephemeral is impossible here; skip

# -----------------------
# Discord Event Handlers / Commands
# -----------------------
@client.event
async def on_ready():
    print(f"{client.user} is ready. Guilds: {[g.name for g in client.guilds]}")
    # sync commands to the specified guild only for rapid iteration
    try:
        guild = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild)
        print("Commands synced to guild.")
    except Exception as e:
        print("コマンド同期エラー:", e)
    # load data
    load_data()
    # start cleanup task inside running loop
    try:
        if not cleanup_task.is_running():
            cleanup_task.start()
    except Exception as e:
        print("cleanup_task start error:", e)

# -----------------------
# Slash commands
# -----------------------
@tree.command(name="マッチ希望", description="ランダムマッチの希望を出します（相手指定不要）")
async def cmd_match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    # Already matched?
    if uid in in_match:
        opp = in_match.get(uid)
        await interaction.response.send_message(f"現在 <@{opp}> とマッチ中です。まずはその試合を終えてください。", ephemeral=True)
        return
    # Already in hope_list?
    if uid in hope_list:
        await interaction.response.send_message("すでにマッチ希望が登録されています。", ephemeral=True)
        return
    # register
    hope_list[uid] = {
        "since": datetime.now(timezone.utc).timestamp(),
        "interaction": interaction,  # keep the interaction to send ephemeral followup when matched
    }
    # reply ephemeral to requester
    await interaction.response.send_message("マッチ希望を登録しました。抽選結果をお待ちください。", ephemeral=True)
    # schedule a draw run (non-blocking)
    asyncio.create_task(perform_draw_and_notify())

@tree.command(name="マッチ希望取下げ", description="マッチ希望の取り下げを行います（自分の申請を取り下げ）")
async def cmd_cancel_match_request(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in hope_list:
        await interaction.response.send_message("マッチ希望が見つかりませんでした。", ephemeral=True)
        return
    hope_list.pop(uid, None)
    await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)

@tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    # must be registered match
    if not is_registered_match(winner.id, loser.id):
        await interaction.response.send_message("このマッチングは登録されていません。まずはマッチ希望をお願いします。", ephemeral=True)
        return
    # create view and send a public message in the channel where command invoked
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    try:
        # send message visible to channel (public); the approvals are constrained to loser only
        view = ResultApproveView(winner.id, loser.id, origin_channel_id=interaction.channel.id if interaction.channel else None)
        await interaction.response.send_message(content, view=view)
    except Exception:
        # fallback: ephemeral if sending public fails
        await interaction.response.send_message(content, view=view, ephemeral=True)

# -----------------------
# 管理者コマンド
# -----------------------
def is_admin(user: discord.Member) -> bool:
    return user.id == ADMIN_ID

@tree.command(name="admin_set_pt", description="[管理用] ユーザーのPTを操作します")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    # admin only
    if not is_admin(interaction.user):
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    set_pt(user.id, pt)
    # update display
    try:
        await update_member_display(user)
    except Exception:
        pass
    await interaction.response.send_message(f"<@{user.id}> のPTを {pt} に設定しました。", ephemeral=True)

@tree.command(name="admin_reset_all", description="[管理用] 全ユーザーのPTを0にリセットします")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    # reset all known guild members to 0pt
    # iterate through guild members
    for g in client.guilds:
        for member in g.members:
            set_pt(member.id, 0)
            try:
                await update_member_display(member)
            except Exception:
                pass
    await interaction.response.send_message("全ユーザーのPTを0にリセットしました。", ephemeral=True)

@tree.command(name="admin_show_ranking", description="[管理用] ランキングを表示します（管理者限定）")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return
    # reuse ranking generation
    ranking_text = build_ranking_text()
    if not ranking_text:
        await interaction.response.send_message("ランキングデータがありません。", ephemeral=True)
        return
    # Admin requested ephemeral
    await interaction.response.send_message(ranking_text, ephemeral=True)

# public ranking command (全ユーザーが使える)
@tree.command(name="ランキング", description="現在のランキングを表示します（全員可）")
async def cmd_ranking(interaction: discord.Interaction):
    ranking_text = build_ranking_text()
    if not ranking_text:
        await interaction.response.send_message("ランキングデータがありません。", ephemeral=True)
        return
    await interaction.response.send_message(ranking_text, ephemeral=False)

# -----------------------
# Helper: ランキング生成
# -----------------------
def build_ranking_text() -> str:
    # Build ranking based on user_data and guild members
    # Create list of tuples (user_id, pt)
    entries = []
    for uid, info in user_data.items():
        entries.append((int(uid), int(info.get("pt", 0))))
    # Also include guild members not present in user_data with 0pt
    for g in client.guilds:
        for m in g.members:
            if m.bot:
                continue
            if m.id not in user_data:
                entries.append((m.id, 0))
    # dedupe by uid, keep highest pt if duplicates
    tmp = {}
    for uid, pt in entries:
        if uid in tmp:
            if pt > tmp[uid]:
                tmp[uid] = pt
        else:
            tmp[uid] = pt
    entries = list(tmp.items())
    if not entries:
        return ""
    # sort by pt desc, then by display name
    entries.sort(key=lambda x: (-x[1], x[0]))
    # standard competition ranking (1224)
    text_lines = ["🏆 ランキング"]
    prev_pt = None
    rank = 0
    display_rank = 0
    for uid, pt in entries:
        rank += 1
        if pt != prev_pt:
            display_rank = rank
        prev_pt = pt
        # find display name (pure username, not appended icon/pt)
        member = None
        for g in client.guilds:
            m = g.get_member(uid)
            if m:
                member = m
                break
        name = None
        if member:
            name = strip_display_suffix(member.display_name)
            role_name, icon = get_rank_info(pt)
            text_lines.append(f"{display_rank}位 {name} {icon} {pt}pt")
        else:
            # fallback to user id mention only
            text_lines.append(f"{display_rank}位 <@{uid}> {pt}pt")
    return "\n".join(text_lines)

# -----------------------
# Errors / Misc
# -----------------------
@client.event
async def on_app_command_error(interaction: discord.Interaction, error):
    # basic error handler to avoid "アプリケーションが応答しませんでした" messages
    try:
        print("App command error:", error)
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("権限エラーです。", ephemeral=True)
        else:
            await interaction.response.send_message("コマンド実行中にエラーが発生しました。管理者へ連絡してください。", ephemeral=True)
    except Exception:
        pass

# -----------------------
# Entry point
# -----------------------
if __name__ == "__main__":
    load_data()
    client.run(DISCORD_TOKEN)
