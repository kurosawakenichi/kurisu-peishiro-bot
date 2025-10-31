import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone
import random

# ----------------------------------------
# 環境変数
# ----------------------------------------
ADMIN_ID = int(os.environ["ADMIN_ID"])
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RANKING_CHANNEL_ID = int(os.environ["RANKING_CHANNEL_ID"])
JUDGE_CHANNEL_ID = int(os.environ["JUDGE_CHANNEL_ID"])
MATCHING_CHANNEL_ID = int(os.environ["MATCHING_CHANNEL_ID"])
BATTLELOG_CHANNEL_ID = int(os.environ["BATTLELOG_CHANNEL_ID"])
BATTLE_CATEGORY_ID = 1427541907579605012
ACTIVE_LOG_CHANNEL_ID = int(os.environ.get("ACTIVE_LOG_CHANNEL_ID", "0"))

JST = timezone(timedelta(hours=+9))
AUTO_APPROVE_SECONDS = 300  # 5分

# ----------------------------------------
# 内部データ
# ----------------------------------------
user_data = {}           # user_id -> {"pt": int}
matching = {}            # 現在マッチ中のプレイヤー組
waiting_list = {}        # user_id -> {"expires": datetime, "task": asyncio.Task, "interaction": discord.Interaction}
matching_channels = {}   # user_id -> 専用チャンネルID

# ========================================
# イベント設定
# ========================================
event_config = {
    "type": None,        # "single" / "long" / "unlimited"
    "dates": None,       # 単発 or 長期イベントの日付範囲
    "times": None,       # 長期イベントの時間帯リスト [(start, end), ...]
    "active": False
}

def now_jst():
    return datetime.now(JST)

# ----------------------------------------
# ランク定義
# ----------------------------------------
rank_roles = [
    (0, 4, "Beginner", "🔰"),
    (5, 9, "Silver", "🥈"),
    (10, 14, "Gold", "🥇"),
    (15, 19, "Master", "⚔️"),
    (20, 24, "GrandMaster", "🪽"),
    (25, 9999, "Challenger", "😈"),
]

rank_ranges_internal = {
    1: range(0, 5),
    2: range(5, 10),
    3: range(10, 15),
    4: range(15, 20),
    5: range(20, 25),
    6: range(25, 10000),
}

# ----------------------------------------
# ボット初期化
# ----------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ----------------------------------------
# ユーティリティ
# ----------------------------------------
def get_rank_info(pt: int):
    for start, end, role, icon in rank_roles:
        if start <= pt <= end:
            return role, icon
    return "Unknown", "❓"

def get_internal_rank(pt: int):
    for rank, rrange in rank_ranges_internal.items():
        if pt in rrange:
            return rank
    return 1

def calculate_pt(my_pt: int, opp_pt: int, result: str) -> int:
    delta = 1 if result == "win" else -1
    return max(my_pt + delta, 0)

async def update_member_display(member: discord.Member):
    pt = user_data.get(member.id, {}).get("pt", 0)
    role_name, icon = get_rank_info(pt)
    try:
        await member.edit(nick=f"{member.display_name.split(' ')[0]} {icon} {pt}pt")
        guild = member.guild
        for r in rank_roles:
            role = discord.utils.get(guild.roles, name=r[2])
            if role and role in member.roles:
                await member.remove_roles(role)
        new_role = discord.utils.get(guild.roles, name=role_name)
        if new_role:
            await member.add_roles(new_role)
    except Exception as e:
        print(f"Error updating {member}: {e}")

def is_registered_match(a: int, b: int):
    return matching.get(a) == b and matching.get(b) == a

# ========================================
# イベントチャンネル制御
# ========================================
async def set_matching_channel_permission(bot, allow: bool):
    """
    MATCHING_CHANNEL を一般ユーザー向けに公開／非公開化する
    allow=True で全員が書き込み可能、False でBot/管理者のみ
    """
    channel = bot.get_channel(MATCHING_CHANNEL_ID)
    if not channel:
        print("[ERROR] MATCHING_CHANNEL が見つかりません。")
        return

    guild = channel.guild
    everyone = guild.default_role
    admin_member = guild.get_member(ADMIN_ID)

    try:
        if allow:
            # 公開: everyone が閲覧・送信可能
            overwrites = {
                everyone: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                bot.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            }
            if admin_member:
                overwrites[admin_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            await channel.edit(overwrites=overwrites)
            print("[イベント制御] MATCHING_CHANNEL を公開しました。")
        else:
            # 非公開: everyone は不可、Bot と管理者だけ可
            overwrites = {
                everyone: discord.PermissionOverwrite(view_channel=False),
                bot.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            }
            if admin_member:
                overwrites[admin_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            await channel.edit(overwrites=overwrites)
            print("[イベント制御] MATCHING_CHANNEL をプライベート化しました。")

        event_config["active"] = allow

    except Exception as e:
        print(f"[ERROR] チャンネル公開/非公開切替に失敗しました: {e}")


async def post_event_notice(bot, message: str, to_matching_channel: bool = False):
    """
    イベント通知
    - to_matching_channel=True なら MATCHING_CHANNEL に送信
    - デフォルトは #お知らせ に送信
    """
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    if to_matching_channel:
        ch = guild.get_channel(MATCHING_CHANNEL_ID)
    else:
        ch = guild.get_channel(1427835216830926958)  # #お知らせ

    if ch:
        await ch.send(message)


# ========================================
# イベントスケジューラー（修正版）
# ========================================
async def event_scheduler_loop(bot):
    await bot.wait_until_ready()
    while True:
        now = now_jst()

        # 単発イベント
        if event_config["type"] == "single":
            start, end = event_config["dates"]
            if start <= now < end and not event_config["active"]:
                await set_matching_channel_permission(bot, True)
                await post_event_notice(bot, "対戦開始！このチャンネルでマッチングが可能です", to_matching_channel=True)
            elif now >= end and event_config["active"]:
                await set_matching_channel_permission(bot, False)
                await post_event_notice(bot, "対戦終了！マッチ希望を締め切ります", to_matching_channel=True)

        # 長期イベント（複数時間帯対応）
        elif event_config["type"] == "long":
            start_date, end_date = event_config["dates"]
            today = now.date()

            if start_date <= today <= end_date:
                active_in_any = False
                for t_start, t_end in event_config["times"]:
                    start_dt = datetime.combine(today, t_start, JST)
                    end_dt = datetime.combine(today, t_end, JST)
                    if start_dt <= now < end_dt:
                        active_in_any = True
                        break  # 1つでも該当時間帯があればOK

                if active_in_any and not event_config["active"]:
                    event_config["active"] = True
                    await set_matching_channel_permission(bot, True)
                    await post_event_notice(bot, "対戦開始！このチャンネルでマッチングが可能です", to_matching_channel=True)
                elif not active_in_any and event_config["active"]:
                    event_config["active"] = False
                    await set_matching_channel_permission(bot, False)
                    await post_event_notice(bot, "対戦終了！マッチ希望を締め切ります", to_matching_channel=True)

        # 無制限イベント
        elif event_config["type"] == "unlimited" and not event_config["active"]:
            await set_matching_channel_permission(bot, True)
            await post_event_notice(bot, "いつでもマッチング可能です", to_matching_channel=True)

        await asyncio.sleep(30)





# ----------------------------------------
# アクティブ状況ログ投稿（イベント別）
# ----------------------------------------
async def post_active_event(event_type: str):
    """
    event_type:
      - "match_request" : /マッチ希望 が出たとき -> "マッチ希望が出ました"
      - "match_end"     : 対戦が終了したとき -> "対戦が終了しました"
    This posts a new message to ACTIVE_LOG_CHANNEL_ID (if set).
    """
    if not ACTIVE_LOG_CHANNEL_ID:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(ACTIVE_LOG_CHANNEL_ID)
    if not ch:
        return
    try:
        if event_type == "match_request":
            await ch.send("マッチ希望が出ました")
        elif event_type == "match_end":
            await ch.send("対戦が終了しました")
    except Exception as e:
        print(f"Failed to post active event ({event_type}): {e}")

# ----------------------------------------
# マッチング処理
# ----------------------------------------
async def try_match_users():
    users = list(waiting_list.keys())
    random.shuffle(users)
    matched = set()
    for i in range(len(users)):
        if users[i] in matched:
            continue
        for j in range(i + 1, len(users)):
            if users[j] in matched:
                continue
            u1, u2 = users[i], users[j]
            pt1 = user_data.get(u1, {}).get("pt", 0)
            pt2 = user_data.get(u2, {}).get("pt", 0)
            rank1 = get_internal_rank(pt1)
            rank2 = get_internal_rank(pt2)
            if abs(rank1 - rank2) >= 3:
                continue

            # マッチ成立
            matching[u1] = u2
            matching[u2] = u1

            # 待機タスク削除（ただし interaction は保持しておき、下で編集）
            for uid in [u1, u2]:
                task = waiting_list[uid]["task"]
                task.cancel()

            # 専用チャンネル作成
            guild = bot.get_guild(GUILD_ID)
            category = guild.get_channel(BATTLE_CATEGORY_ID)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.get_member(u1): discord.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.get_member(u2): discord.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.get_member(ADMIN_ID): discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
            }
            channel_name = f"battle-{u1}-vs-{u2}"
            battle_ch = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
            matching_channels[u1] = battle_ch.id
            matching_channels[u2] = battle_ch.id

            # 降参ボタンを含む初期メッセージ
            await battle_ch.send(
                f"<@{u1}> vs <@{u2}> のマッチングが成立しました。\n試合終了後、勝者は /勝利報告 を行ってください。\nこのチャンネルからは降参ボタンで即時敗北申告ができます（押した側が敗北）。",
                view=ForfeitView(u1, u2, battle_ch.id)
            )

            # 待機メッセージ更新（元の ephemeral メッセージの差し替えを試みる）
            for uid in [u1, u2]:
                interaction = waiting_list.get(uid, {}).get("interaction")
                if interaction:
                    try:
                        await interaction.edit_original_response(
                            content=f"✅ マッチング成立！ 専用チャンネル <#{battle_ch.id}> で試合を行ってください。",
                            view=None
                        )
                    except Exception:
                        # interaction が無効（ブラウザ更新など）なら無視
                        pass
                # remove from waiting list now
                waiting_list.pop(uid, None)

            matched.update([u1, u2])
            # NOTE: Do not post "match_request" here; we post on request creation.
            break

# ----------------------------------------
# 待機処理
# ----------------------------------------
async def remove_waiting(user_id: int):
    if user_id in waiting_list:
        interaction = waiting_list[user_id]["interaction"]
        try:
            view = RetryView(user_id)
            await interaction.edit_original_response(content=f"⏱ <@{user_id}> さん、マッチング相手が見つかりませんでした。", view=view)
        except Exception:
            pass
        waiting_list.pop(user_id, None)

async def waiting_timer(user_id: int):
    try:
        await asyncio.sleep(300)
        await remove_waiting(user_id)
    except asyncio.CancelledError:
        pass

async def start_match_wish(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in matching:
        await interaction.response.send_message("すでにマッチ済みです。", ephemeral=True)
        return
    if uid in waiting_list:
        await interaction.response.send_message("すでに待機中です。", ephemeral=True)
        return
    task = asyncio.create_task(waiting_timer(uid))
    waiting_list[uid] = {"expires": datetime.now(JST)+timedelta(seconds=300), "task": task, "interaction": interaction}
    view = CancelWaitingView(uid)
    await interaction.response.send_message("マッチング中です…", ephemeral=True, view=view)

    # post a short log to ACTIVE_LOG channel that a match request appeared
    # (user requested this behavior)
    asyncio.create_task(post_active_event("match_request"))

    # 待機タイマーリセット（既存の待機ユーザーの timer を再起動）
    for uid2, info in list(waiting_list.items()):
        info["task"].cancel()
        info["task"] = asyncio.create_task(waiting_timer(uid2))
        info["interaction"] = info.get("interaction", interaction)
    await asyncio.sleep(5)
    await try_match_users()

# ----------------------------------------
# /マッチ希望 コマンド & ボタンビュー
# ----------------------------------------
class CancelWaitingView(discord.ui.View):
    def __init__(self, user_id:int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user_id in waiting_list:
            waiting_list[self.user_id]["task"].cancel()
            waiting_list.pop(self.user_id, None)
            await interaction.response.send_message("待機リストから削除しました。", ephemeral=True)
        self.stop()

class RetryView(discord.ui.View):
    def __init__(self, user_id:int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="リトライ", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_match_wish(interaction)
        self.stop()

@bot.tree.command(name="マッチ希望", description="ランダムマッチ希望")
async def cmd_match_wish(interaction: discord.Interaction):
    if interaction.channel.id != MATCHING_CHANNEL_ID:
        await interaction.response.send_message(f"このコマンドは <#{MATCHING_CHANNEL_ID}> でのみ使用可能です。", ephemeral=True)
        return
    await start_match_wish(interaction)

# ----------------------------------------
# 降参ボタンビュー（専用チャンネル初期メッセージ用）
# ----------------------------------------
class ForfeitView(discord.ui.View):
    def __init__(self, user1:int, user2:int, channel_id:int):
        super().__init__(timeout=None)
        self.user1 = user1
        self.user2 = user2
        self.channel_id = channel_id

    @discord.ui.button(label="降参", style=discord.ButtonStyle.danger)
    async def forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in [self.user1, self.user2]:
            await interaction.response.send_message("あなたの試合ではありません。", ephemeral=True)
            return
        winner = self.user2 if uid == self.user1 else self.user1
        loser = uid
        # 公開で降参通知
        await interaction.response.send_message(f"<@{loser}> が降参しました。<@{winner}> の勝利です。", ephemeral=False)
        # handle result (this will log to BATTLELOG and remove matching, delete channel, and also post active-event)
        await handle_approved_result(winner, loser, interaction.guild, self.channel_id)
        # handle_approved_result will post match_end to ACTIVE_LOG channel,
        # so no extra post here to avoid duplication.

# ----------------------------------------
# /勝利報告 コマンド（相手指定不要）
# ----------------------------------------
@bot.tree.command(name="勝利報告", description="勝者用：対戦結果を報告します")
async def cmd_victory_report(interaction: discord.Interaction):
    winner = interaction.user
    battle_ch_id = matching_channels.get(winner.id)
    if not battle_ch_id or interaction.channel.id != battle_ch_id:
        await interaction.response.send_message("このコマンドは専用対戦チャンネル内でのみ使用可能です。", ephemeral=True)
        return
    loser_id = matching.get(winner.id)
    if not loser_id:
        await interaction.response.send_message("相手情報が見つかりません。", ephemeral=True)
        return
    content = f"この試合の勝者は <@{winner.id}> です。結果に同意しますか？"
    await interaction.channel.send(content, view=ResultApproveView(winner.id, loser_id, battle_ch_id))
    await interaction.response.send_message("結果報告を受け付けました。敗者の承認を待ちます。", ephemeral=True)
    # 自動承認タスク（異議が無ければ5分後に自動処理）
    asyncio.create_task(auto_approve_result(winner.id, loser_id, interaction.guild, battle_ch_id))

# ----------------------------------------
# 結果承認・異議ビュー
# ----------------------------------------
class ResultApproveView(discord.ui.View):
    def __init__(self, winner_id:int, loser_id:int, battle_ch_id:int):
        super().__init__(timeout=None)
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.battle_ch_id = battle_ch_id
        self.processed = False

    async def log_battle_result(self, guild: discord.Guild, result_text: str):
        log_ch = guild.get_channel(BATTLELOG_CHANNEL_ID)
        if log_ch:
            await log_ch.send(result_text)

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
        await handle_approved_result(self.winner_id, self.loser_id, interaction.guild, self.battle_ch_id)
        # handle_approved_result posts match_end

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
        judge_ch = interaction.guild.get_channel(JUDGE_CHANNEL_ID)
        if judge_ch:
            await judge_ch.send(f"⚖️ 審議依頼: <@{self.winner_id}> vs <@{self.loser_id}> に異議が出ました。結論が出たら<@{ADMIN_ID}> に連絡してください。")
        # 内部的にマッチ解除（対戦チャンネルは維持）
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)
        await self.log_battle_result(interaction.guild,
            f"[異議発生] {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} - <@{self.winner_id}> vs <@{self.loser_id}>")
        # post that a match ended (by dispute) to ACTIVE_LOG channel
        asyncio.create_task(post_active_event("match_end"))

# ----------------------------------------
# 結果反映処理
# ----------------------------------------
async def handle_approved_result(winner_id:int, loser_id:int, guild: discord.Guild, battle_ch_id:int):
    if not is_registered_match(winner_id, loser_id):
        return
    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt  = user_data.get(loser_id, {}).get("pt", 0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new  = calculate_pt(loser_pt, winner_pt, "lose")
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new

    w_member = guild.get_member(winner_id)
    l_member = guild.get_member(loser_id)
    if w_member:
        await update_member_display(w_member)
    if l_member:
        await update_member_display(l_member)

    # 内部マッチ削除
    matching.pop(winner_id, None)
    matching.pop(loser_id, None)

    log_ch = guild.get_channel(BATTLELOG_CHANNEL_ID)
    # 対戦ログ記録（勝者確定）
    if log_ch:
        # include timestamps and mention formatting similar to user's request
        now_str = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
        await log_ch.send(f"[勝者確定] {now_str} - <@{winner_id}> 勝利 vs <@{loser_id}> 敗北")
        delta_w = winner_new - winner_pt
        delta_l = loser_new - loser_pt
        await log_ch.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

    # 専用チャンネル削除（ただし10秒の事前通知）
    battle_ch = guild.get_channel(battle_ch_id)
    if battle_ch:
        try:
            await battle_ch.send("このチャンネルは自動的に削除されます（10秒後）。")
        except Exception:
            pass
        # wait 10 seconds, then delete
        await asyncio.sleep(10)
        try:
            await battle_ch.delete()
        except Exception:
            pass

    # post active-event: match ended
    asyncio.create_task(post_active_event("match_end"))

async def auto_approve_result(winner_id:int, loser_id:int, guild: discord.Guild, battle_ch_id:int):
    await asyncio.sleep(AUTO_APPROVE_SECONDS)
    if is_registered_match(winner_id, loser_id):
        await handle_approved_result(winner_id, loser_id, guild, battle_ch_id)

# ----------------------------------------
# ランキング表示
# ----------------------------------------
def standard_competition_ranking():
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("pt",0), reverse=True)
    result = []
    prev_pt = None
    rank = 0
    display_rank = 0
    for uid, data in sorted_users:
        pt = data.get("pt",0)
        display_rank += 1
        if pt != prev_pt:
            rank = display_rank
            prev_pt = pt
        result.append((rank, uid, pt))
    return result

@bot.tree.command(name="ランキング", description="PT順にランキング表示")
async def cmd_ranking(interaction: discord.Interaction):
    if interaction.channel.id != RANKING_CHANNEL_ID:
        await interaction.response.send_message(f"このコマンドは <#{RANKING_CHANNEL_ID}> でのみ使用可能です。", ephemeral=True)
        return
    rankings = standard_competition_ranking()
    lines = []
    for rank, uid, pt in rankings:
        role, icon = get_rank_info(pt)
        member = interaction.guild.get_member(uid)
        if member:
            words = member.display_name.split()
            base_name = " ".join(words[:-2]) if len(words) > 2 else member.display_name
            lines.append(f"{rank}位 {base_name} {icon} {pt}pt")
    await interaction.response.send_message("🏆 ランキング\n" + "\n".join(lines))

# ----------------------------------------
# 管理コマンド
# ----------------------------------------
@bot.tree.command(name="admin_set_pt", description="指定ユーザーのPTを設定")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    user_data.setdefault(user.id, {})["pt"] = pt
    await update_member_display(user)
    await interaction.response.send_message(f"{user.display_name} のPTを {pt} に設定しました。", ephemeral=True)

@bot.tree.command(name="admin_reset_all", description="全ユーザーのPTを0にリセット")
async def admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    guild = bot.get_guild(GUILD_ID)
    for member in guild.members:
        if member.bot:
            continue
        user_data.setdefault(member.id, {})["pt"] = 0
        await update_member_display(member)
    await interaction.response.send_message("全ユーザーのPTを0にリセットしました。", ephemeral=True)

# /単発イベント /長期イベント /無期限イベント コマンド
@bot.tree.command(name="単発イベント", description="単発イベント設定")
@app_commands.describe(start="開始日時 YYYY-MM-DD HH:MM", end="終了日時 YYYY-MM-DD HH:MM")
async def cmd_single_event(interaction: discord.Interaction, start: str, end: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return

    start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    end_dt   = datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    event_config.update({"type": "single", "dates": (start_dt, end_dt), "active": False})

    # --- 現在の時間に応じてチャンネルを制御 ---
    now = now_jst()
    if start_dt <= now < end_dt:
        await set_matching_channel_permission(bot, True)
        await post_event_notice(bot, "対戦開始！このチャンネルでマッチングが可能です")
        event_config["active"] = True
    else:
        await set_matching_channel_permission(bot, False)
        event_config["active"] = False
    # --------------------------------

    await post_event_notice(bot, f"現在のイベント設定🔽\n{start}〜{end}のみマッチング可能です")
    await interaction.response.send_message("単発イベントを設定しました。", ephemeral=True)


@bot.tree.command(name="長期イベント", description="長期イベント設定")
@app_commands.describe(start_date="開始日 YYYY-MM-DD", end_date="終了日 YYYY-MM-DD", times="時間帯 HH:MM-HH:MM,複数可カンマ区切り")
async def cmd_long_event(interaction: discord.Interaction, start_date: str, end_date: str, times: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return

    s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    time_list = []
    for t in times.split(","):
        s, e = t.split("-")
        s_dt = datetime.strptime(s.strip(), "%H:%M").time()
        e_dt = datetime.strptime(e.strip(), "%H:%M").time()
        time_list.append((s_dt, e_dt))

    event_config.update({"type": "long", "dates": (s_date, e_date), "times": time_list, "active": False})

    notice = f"現在のイベント設定🔽\n{start_date}〜{end_date}の期間中、以下の時間帯のみマッチング可能です\n"
    for s, e in time_list:
        notice += f"・{s.strftime('%H:%M')}〜{e.strftime('%H:%M')}\n"
    await post_event_notice(bot, notice)

    # --- 現在の時間に応じてチャンネルを制御 ---
    now = now_jst()
    today = now.date()
    active_now = False
    if s_date <= today <= e_date:
        for t_start, t_end in time_list:
            start_dt = datetime.combine(today, t_start, JST)
            end_dt = datetime.combine(today, t_end, JST)
            if start_dt <= now < end_dt:
                active_now = True
                break
    if active_now:
        await set_matching_channel_permission(bot, True)
        await post_event_notice(bot, "対戦開始！このチャンネルでマッチングが可能です")
        event_config["active"] = True
    else:
        await set_matching_channel_permission(bot, False)
        event_config["active"] = False
    # --------------------------------

    await interaction.response.send_message("長期イベントを設定しました。", ephemeral=True)


@bot.tree.command(name="無期限イベント", description="無期限イベント設定")
async def cmd_unlimited_event(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    event_config.update({"type": "unlimited", "active": True})
    await set_matching_channel_permission(bot, True)
    await post_event_notice(bot, "現在のイベント設定🔽\nいつでもマッチング可能です")
    await interaction.response.send_message("無期限イベントを設定しました。", ephemeral=True)



# ----------------------------------------
# 起動処理
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    await bot.tree.sync()
    if not hasattr(bot, "event_scheduler_started"):
        bot.event_scheduler_started = True
        asyncio.create_task(event_scheduler_loop(bot))
        print("[INFO] イベントスケジューラーを起動しました")

bot.run(DISCORD_TOKEN)
