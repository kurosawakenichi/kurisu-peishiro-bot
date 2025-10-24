# main.py
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import tasks

# ---------------------------
# 環境変数（Railway の Variables に登録してください）
# ---------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])

# ---------------------------
# 設定
# ---------------------------
DATA_FILE = "data.json"
AUTO_APPROVE_SECONDS = 5 * 60  # 5分（承認ボタン期限）
MATCH_HOPE_TIMEOUT = 5 * 60  # 5分（マッチ希望のタイムアウト）
DRAW_WAIT_SECONDS = 5  # 抽選の待機時間（3->5秒に調整済み）
PT_MIN = 0

# ランク表示（ライト仕様: Challengeなし、チャレンジロールなし）
# 表示用アイコン（ユーザー名に付与）
RANK_ROLES_SIMPLE = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GroundMaster", "🪽"),
    (25, 10**9, "Challenger", "😈"),
]

# 内部ランク（rank1..rank6）: マッチ判定と簡略化用
INTERNAL_RANKS = {
    1: range(0, 5),    # 0-4
    2: range(5, 10),   # 5-9
    3: range(10, 15),  # 10-14
    4: range(15, 20),  # 15-19
    5: range(20, 25),  # 20-24
    6: range(25, 10000),  # 25+
}

# ---------------------------
# ユーザーデータ管理
# ---------------------------
# structure:
# {
#   "users": {
#       "<user_id>": {"pt": int}
#   }
# }
# ---------------------------
def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"users": {}}
    return {"users": {}}


def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


data_lock = asyncio.Lock()
data = load_data()

# in-memory runtime structures (not persisted except user_data)
hope_list: Dict[int, float] = {}  # user_id -> timestamp of application
draw_list: List[int] = []  # currently in current draw
in_match: Dict[int, int] = {}  # user_id -> opponent_id
# lock to avoid race conditions
state_lock = asyncio.Lock()

# ---------------------------
# ユーティリティ
# ---------------------------
def get_user_pt(uid: int) -> int:
    return data.get("users", {}).get(str(uid), {}).get("pt", 0)


def set_user_pt(uid: int, pt: int) -> None:
    if "users" not in data:
        data["users"] = {}
    data["users"].setdefault(str(uid), {})["pt"] = max(PT_MIN, int(pt))
    save_data(data)


def get_rank_info(pt: int) -> Tuple[str, str]:
    """pt -> (role_name, icon)"""
    for start, end, role, icon in RANK_ROLES_SIMPLE:
        if start <= pt <= end:
            return role, icon
    # fallback
    return "Challenger", "😈"


def get_internal_rank(pt: int) -> int:
    for r, rng in INTERNAL_RANKS.items():
        if pt in rng:
            return r
    return 6


def rank_diff_allowed(pt_a: int, pt_b: int) -> bool:
    """内部ランク差が3以上なら不可"""
    return abs(get_internal_rank(pt_a) - get_internal_rank(pt_b)) < 3


def compute_pt_change(winner_pt: int, loser_pt: int) -> Tuple[int, int]:
    """ライト仕様: 常に勝者+1, 敗者-1（下限0）"""
    new_w = winner_pt + 1
    new_l = max(PT_MIN, loser_pt - 1)
    return new_w, new_l


async def update_member_display(member: discord.Member):
    """ユーザー名（ニック）とロールをPTに合わせて更新する。
    ニックネームは base_name + " {icon} {pt}pt"
    ただし既に base_name 以外の付与がある場合は整形して上書き。
    """
    try:
        uid = member.id
        pt = get_user_pt(uid)
        role_name, icon = get_rank_info(pt)
        guild = member.guild

        # decide base display name: use account name (member.name) not nickname
        base_name = member.name

        new_nick = f"{base_name} {icon} {pt}pt"

        # attempt to set nickname if possible
        try:
            # Some servers prevent changing certain members; catch exceptions
            if guild.me.guild_permissions.manage_nicknames and member.nick != new_nick:
                await member.edit(nick=new_nick)
        except discord.Forbidden:
            # ignore if can't change nickname
            pass
        except Exception:
            pass

        # manage roles: ensure the role exists in guild
        # remove other rank roles and add the correct one
        target_role = discord.utils.get(guild.roles, name=role_name)
        if target_role:
            # remove any other roles in the rank set
            to_remove = []
            for _, _, rname, _ in RANK_ROLES_SIMPLE:
                r = discord.utils.get(guild.roles, name=rname)
                if r and r in member.roles and r != target_role:
                    to_remove.append(r)
            try:
                if to_remove:
                    await member.remove_roles(*to_remove, reason="rank update")
                if target_role not in member.roles:
                    await member.add_roles(target_role, reason="rank update")
            except discord.Forbidden:
                # skip if insufficient permissions
                pass
            except Exception:
                pass
    except Exception:
        # log somewhere if needed
        return

# ---------------------------
# Bot setup
# ---------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = False

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    print(f"{datetime.now(timezone.utc).astimezone().isoformat()} - {bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    # sync commands to guild
    try:
        guild = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild)
        print("Commands synced to guild.")
    except Exception as e:
        try:
            await tree.sync()
            print("Commands synced globally.")
        except Exception:
            print("Failed to sync commands:", e)


# ---------------------------
# Views: マッチ希望取下げ / 結果承認ビュー etc
# ---------------------------

class CancelHopeView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=None)
        self.uid = uid

    @discord.ui.button(label="取り下げ", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("これはあなたの操作ではありません。", ephemeral=True)
            return
        async with state_lock:
            if self.uid in hope_list:
                hope_list.pop(self.uid, None)
                await interaction.response.send_message("マッチ希望を取り下げました。", ephemeral=True)
            else:
                await interaction.response.send_message("既にマッチ希望は存在しません。", ephemeral=True)


class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id: int, loser_id: int, origin_channel_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.origin_channel_id = origin_channel_id
        self.processed = False

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        # only loser can press
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message("これはあなたの試合ではないようです。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        # acknowledge and process
        await interaction.response.edit_message(content="承認されました。結果を反映します。", view=None)
        ch = interaction.channel
        # perform update
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
        await interaction.response.edit_message(content="異議が申立てられました。審議チャンネルへ通知します。", view=None)
        # notify judge channel publicly
        guild = interaction.guild
        judge_ch = guild.get_channel(JUDGE_CHANNEL_ID)
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。\nこのマッチングは無効扱いとなっています。審議結果を <@{ADMIN_ID}> にご報告ください。")
        # remove match from in_match
        async with state_lock:
            in_match.pop(self.winner_id, None)
            in_match.pop(self.loser_id, None)


# ---------------------------
# Core handlers
# ---------------------------

def is_registered_match(a: int, b: int) -> bool:
    return in_match.get(a) == b and in_match.get(b) == a


async def handle_approved_result(winner_id: int, loser_id: int, channel: discord.abc.Messageable):
    """承認が発生したときの実処理。pt更新、表示更新、マッチ解除、結果通知。"""
    async with state_lock:
        if not is_registered_match(winner_id, loser_id):
            await channel.send("このマッチングは登録されていません。まずはマッチ希望で抽選してから試合を行ってください。")
            return

        winner_pt = get_user_pt(winner_id)
        loser_pt = get_user_pt(loser_id)

        new_w, new_l = compute_pt_change(winner_pt, loser_pt)

        # write
        set_user_pt(winner_id, new_w)
        set_user_pt(loser_id, new_l)

        # reflect to guild members (update nick + roles)
        for g in bot.guilds:
            w_member = g.get_member(winner_id)
            l_member = g.get_member(loser_id)
            if w_member:
                await update_member_display(w_member)
            if l_member:
                await update_member_display(l_member)

        # remove match
        in_match.pop(winner_id, None)
        in_match.pop(loser_id, None)

    delta_w = new_w - winner_pt
    delta_l = new_l - loser_pt
    await channel.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")


# ---------------------------
# Commands
# ---------------------------

def is_admin(user: discord.User) -> bool:
    return user.id == ADMIN_ID


@tree.command(name="マッチ希望", description="ランダムで対戦相手を探します（相手指定不要）")
async def cmd_match_request(interaction: discord.Interaction):
    user = interaction.user
    uid = user.id

    await interaction.response.defer(ephemeral=True)

    async with state_lock:
        # already in match?
        if uid in in_match:
            opp = in_match.get(uid)
            await interaction.followup.send(f"現在 <@{opp}> とマッチ中です。まずはその試合が完了するのをお待ちください。", ephemeral=True)
            return

        # already in hope list?
        if uid in hope_list:
            await interaction.followup.send("既にマッチ希望を出しています。/マッチ希望取下げ で取り下げ可能です。", ephemeral=True)
            return

        # add to hope list
        hope_list[uid] = datetime.now(timezone.utc).timestamp()

    # notify only the user (ephemeral) that matching has started
    await interaction.followup.send("マッチ希望を受け付けました。抽選を開始します…（最大5分間有効）", ephemeral=True)

    # attempt to find a partner among existing hope_list eligible now
    await try_matchmaking_after_delay()


async def try_matchmaking_after_delay():
    """簡易なランダムマッチング処理：DRAW_WAIT_SECONDS 後に抽選を行う。
    複数同時に呼ばれる可能性に備え、state_lock で保護している。
    """
    await asyncio.sleep(DRAW_WAIT_SECONDS)
    async with state_lock:
        # build candidate list from hope_list (filter expired)
        now_ts = datetime.now(timezone.utc).timestamp()
        candidates = []
        expired = []
        for uid, ts in list(hope_list.items()):
            if now_ts - ts > MATCH_HOPE_TIMEOUT:
                expired.append(uid)
            else:
                candidates.append(uid)
        # remove expired (and notify them)
        for uid in expired:
            hope_list.pop(uid, None)
            # can't send ephemeral here; best-effort send via DM/channel is not used by spec
            # so we skip notifying to avoid errors. The user will find their ephemeral expired.
        if len(candidates) < 2:
            return

        # shuffle and pair randomly but respecting rank_diff_allowed
        import random
        random.shuffle(candidates)
        paired = set()
        pairs = []
        for uid in candidates:
            if uid in paired:
                continue
            # try find partner
            for uid2 in candidates:
                if uid2 == uid or uid2 in paired:
                    continue
                # check allowed
                if rank_diff_allowed(get_user_pt(uid), get_user_pt(uid2)):
                    pairs.append((uid, uid2))
                    paired.add(uid)
                    paired.add(uid2)
                    break
            # if no partner found, leave unpaired (remain in hope_list)
        # register pairs: remove from hope_list and add to in_match
        for a, b in pairs:
            hope_list.pop(a, None)
            hope_list.pop(b, None)
            in_match[a] = b
            in_match[b] = a
            # notify both players privately (ephemeral impossible here because not in interaction context)
            # We'll try to DM; if DM fails, we will post in the ranking channel as fallback but ephemeral to avoid spam.
            # Spec: only notify target users. We'll attempt DM first.
            for uid, opp in ((a, b), (b, a)):
                member = None
                for g in bot.guilds:
                    member = g.get_member(uid)
                    if member:
                        break
                if member:
                    try:
                        await member.send(f"<@{uid}> vs <@{opp}> のマッチングが成立しました。試合後、勝者が /結果報告 を行ってください。")
                    except Exception:
                        # DM unavailable -> send ephemeral fallback to ranking channel via mention (not ideal),
                        # but spec asked no public; instead, try to send a normal message in a shared channel (ranking channel) but public is OK?
                        # However spec asks "対象ユーザーのみに通知". Since we cannot send ephemeral without interaction,
                        # fallback: send a visible message in ranking channel but minimal.
                        try:
                            ch = bot.get_channel(RANKING_CHANNEL_ID)
                            if ch:
                                await ch.send(f"マッチ成立: <@{uid}> vs <@{opp}> （通知を確認してください）")
                        except Exception:
                            pass
        # done


@tree.command(name="マッチ希望取下げ", description="マッチ希望を取り下げます（申請者のみ）")
async def cmd_cancel_match_request(interaction: discord.Interaction):
    user = interaction.user
    uid = user.id
    async with state_lock:
        if uid in hope_list:
            hope_list.pop(uid, None)
            view = CancelHopeView(uid)
            await interaction.response.send_message("マッチ希望を取り下げました。", view=view, ephemeral=True)
        else:
            await interaction.response.send_message("現在マッチ希望は出ていません。", ephemeral=True)


@tree.command(name="結果報告", description="（勝者用）対戦結果を報告します。敗者を指定してください。")
@app_commands.describe(opponent="敗者のメンバー")
async def cmd_report_result(interaction: discord.Interaction, opponent: discord.Member):
    winner = interaction.user
    loser = opponent
    await interaction.response.defer(ephemeral=False)

    async with state_lock:
        if not is_registered_match(winner.id, loser.id):
            await interaction.followup.send("このマッチングは登録されていません。まずはマッチ希望で抽選してから試合を行ってください。", ephemeral=True)
            return

    # post approval view in the channel where command was used (spec: no DM)
    view = ResultApproveView(winner.id, loser.id, origin_channel_id=interaction.channel.id if interaction.channel else None)
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？（承認：勝者の申告どおり／異議：審判へ）"
    sent_msg = None
    try:
        sent_msg = await interaction.channel.send(content, view=view)
    except Exception:
        await interaction.followup.send("承認メッセージの送信に失敗しました。", ephemeral=True)
        return

    await interaction.followup.send("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)

    # schedule auto-approve after AUTO_APPROVE_SECONDS if not processed
    async def auto_approve_task():
        await asyncio.sleep(AUTO_APPROVE_SECONDS)
        async with state_lock:
            if is_registered_match(winner.id, loser.id):
                # perform approval automatically
                try:
                    await handle_approved_result(winner.id, loser.id, interaction.channel)
                except Exception:
                    pass

    asyncio.create_task(auto_approve_task())


@tree.command(name="ランキング", description="現在のランキングを表示します（全ユーザー使用可能）")
async def cmd_show_ranking(interaction: discord.Interaction):
    # build ranking list
    users = data.get("users", {})
    # list of tuples (uid, pt)
    items = [(int(uid), info.get("pt", 0)) for uid, info in users.items()]
    # ensure all guild members are represented even with 0pt? We show only known users
    # sort by pt desc, then user id
    items.sort(key=lambda x: (-x[1], x[0]))

    # compute standard competition ranking (1,2,2,4)
    ranking_output = []
    last_pt = None
    rank = 0
    display_rank = 0
    for uid, pt in items:
        rank += 1
        if pt != last_pt:
            display_rank = rank
            last_pt = pt
        # fetch member's display name (prefer nickname)
        name = None
        for g in bot.guilds:
            m = g.get_member(uid)
            if m:
                name = m.display_name
                break
        if not name:
            name = f"<@{uid}>"
        # ensure only one icon/pt in display by using display_name directly
        role_name, icon = get_rank_info(pt)
        ranking_output.append(f"{display_rank}位 {name} {icon} {pt}pt")

    if not ranking_output:
        await interaction.response.send_message("ランキング登録者がいません。", ephemeral=True)
        return

    # send as a single message (may be long, but acceptable)
    try:
        await interaction.response.send_message("\n".join(ranking_output), ephemeral=False)
    except Exception:
        # fallback
        await interaction.followup.send("\n".join(ranking_output), ephemeral=False)


# ---------------------------
# 管理者コマンド
# ---------------------------

@tree.command(name="admin_set_pt", description="管理者: 指定ユーザーのptを設定します")
@app_commands.describe(user="対象ユーザー", pt="新しいPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("このコマンドは管理者のみ実行できます。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    # set pt and update display
    set_user_pt(user.id, pt)
    # update display for this user in all guilds found
    for g in bot.guilds:
        member = g.get_member(user.id)
        if member:
            await update_member_display(member)
    await interaction.followup.send(f"{user.display_name} の PT を {pt} に設定しました。", ephemeral=True)


@tree.command(name="admin_reset_all", description="管理者: 全ユーザーのptと表示を初期化します")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("このコマンドは管理者のみ実行できます。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    # reset all users PT to 0 and update displays
    async with data_lock:
        data["users"] = {}
        save_data(data)
    for g in bot.guilds:
        for member in g.members:
            # skip bots
            if member.bot:
                continue
            set_user_pt(member.id, 0)
            await update_member_display(member)
    await interaction.followup.send("全ユーザーのPTと表示を初期化しました。", ephemeral=True)


# ---------------------------
# Startup persistence / background tasks if needed
# ---------------------------

# No automatic ranking posting per today's specification

# ---------------------------
# Run
# ---------------------------

if __name__ == "__main__":
    bot.run(TOKEN)
