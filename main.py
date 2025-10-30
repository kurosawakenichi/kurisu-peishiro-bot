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

# JSTタイムゾーン
JST = timezone(timedelta(hours=+9))
AUTO_APPROVE_SECONDS = 300  # 5分

# ----------------------------------------
# 内部データ
# ----------------------------------------
user_data = {}
matching = {}
waiting_list = {}
matching_channels = {}

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

async def post_active_event(event_type: str):
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

            matching[u1] = u2
            matching[u2] = u1

            for uid in [u1, u2]:
                task = waiting_list[uid]["task"]
                task.cancel()

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

            await battle_ch.send(
                f"<@{u1}> vs <@{u2}> のマッチングが成立しました。\n試合終了後、勝者は /勝利報告 を行ってください。\nこのチャンネルからは降参ボタンで即時敗北申告ができます（押した側が敗北）。",
                view=ForfeitView(u1, u2, battle_ch.id)
            )

            for uid in [u1, u2]:
                interaction = waiting_list.get(uid, {}).get("interaction")
                if interaction:
                    try:
                        await interaction.edit_original_response(
                            content=f"✅ マッチング成立！ 専用チャンネル <#{battle_ch.id}> で試合を行ってください。",
                            view=None
                        )
                    except Exception:
                        pass
                waiting_list.pop(uid, None)

            matched.update([u1, u2])
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
    asyncio.create_task(post_active_event("match_request"))

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
        await interaction.response.send_message(f"<@{loser}> が降参しました。<@{winner}> の勝利です。", ephemeral=False)
        await handle_approved_result(winner, loser, interaction.guild, self.channel_id)

# ----------------------------------------
# /勝利報告 コマンド
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
        matching.pop(self.winner_id, None)
        matching.pop(self.loser_id, None)
        await self.log_battle_result(interaction.guild,
            f"[異議発生] {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} - <@{self.winner_id}> vs <@{self.loser_id}>")
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

    matching.pop(winner_id, None)
    matching.pop(loser_id, None)

    log_ch = guild.get_channel(BATTLELOG_CHANNEL_ID)
    if log_ch:
        now_str = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
        await log_ch.send(f"[勝者確定] {now_str} - <@{winner_id}> 勝利 vs <@{loser_id}> 敗北")
        delta_w = winner_new - winner_pt
        delta_l = loser_new - loser_pt
        await log_ch.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

    battle_ch = guild.get_channel(battle_ch_id)
    if battle_ch:
        try:
            await battle_ch.send("このチャンネルは自動的に削除されます（10秒後）。")
        except Exception:
            pass
        await asyncio.sleep(10)
        try:
            await battle_ch.delete()
        except Exception:
            pass

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
        member = interaction.guild.get_member(uid)
        if member:
            lines.append(f"{rank}位: {member.display_name} ({pt}pt)")
    await interaction.response.send_message("\n".join(lines), ephemeral=False)

# ----------------------------------------
# ----------------------------------------
# イベントタイマー管理（単発・長期・無期限）＋開始/終了メッセージ
# ----------------------------------------
event_state = {
    "type": "none",       # none / single / long / unlimited
    "single": {"start": None, "end": None},
    "long": {"start_date": None, "end_date": None, "time_slots": []},
}

EVENT_NOTICE_CHANNEL_ID = 1427835216830926958
_event_last_status = None

async def post_event_notice(guild: discord.Guild):
    ch = guild.get_channel(EVENT_NOTICE_CHANNEL_ID)
    if not ch:
        return
    msg_lines = ["現在のイベント設定🔽"]
    if event_state["type"] == "single":
        start = event_state["single"]["start"].strftime("%m/%d %H:%M")
        end = event_state["single"]["end"].strftime("%m/%d %H:%M")
        msg_lines.append(f"{start}〜{end}のみマッチング可能です")
    elif event_state["type"] == "long":
        start_date = event_state["long"]["start_date"].strftime("%m/%d")
        end_date = event_state["long"]["end_date"].strftime("%m/%d")
        msg_lines.append(f"{start_date}〜{end_date}の期間中、以下の時間帯のみマッチング可能です")
        for s_time, e_time in event_state["long"]["time_slots"]:
            msg_lines.append(f"・{s_time.strftime('%H:%M')}〜{e_time.strftime('%H:%M')}")
    elif event_state["type"] == "unlimited":
        msg_lines.append("いつでもマッチング可能です")
    await ch.send("\n".join(msg_lines))

# /単発イベント
@bot.tree.command(name="単発イベント", description="単発イベントを設定")
@app_commands.describe(start="開始日時 YYYY/MM/DD/HH:MM", end="終了日時 YYYY/MM/DD/HH:MM")
async def cmd_single_event(interaction: discord.Interaction, start: str, end: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    start_dt = datetime.strptime(start, "%Y/%m/%d/%H:%M")
    end_dt   = datetime.strptime(end, "%Y/%m/%d/%H:%M")
    event_state["type"] = "single"
    event_state["single"]["start"] = start_dt
    event_state["single"]["end"] = end_dt
    await post_event_notice(interaction.guild)
    await interaction.response.send_message(f"単発イベントを設定しました: {start}〜{end}", ephemeral=True)

# /長期イベント
@bot.tree.command(name="長期イベント", description="長期イベントを設定")
@app_commands.describe(
    start_date="開始日 YYYY/MM/DD",
    end_date="終了日 YYYY/MM/DD",
    time_slots="時間帯 HH:MM-HH:MM をカンマ区切りで指定（例: 21:00-22:00,23:00-23:30）"
)
async def cmd_long_event(interaction: discord.Interaction, start_date: str, end_date: str, time_slots: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    s_date = datetime.strptime(start_date, "%Y/%m/%d").date()
    e_date = datetime.strptime(end_date, "%Y/%m/%d").date()
    slots = []
    for slot in time_slots.split(","):
        start_t, end_t = slot.split("-")
        s_time = datetime.strptime(start_t.strip(), "%H:%M").time()
        e_time = datetime.strptime(end_t.strip(), "%H:%M").time()
        slots.append((s_time, e_time))
    event_state["type"] = "long"
    event_state["long"]["start_date"] = s_date
    event_state["long"]["end_date"] = e_date
    event_state["long"]["time_slots"] = slots
    await post_event_notice(interaction.guild)
    await interaction.response.send_message(f"長期イベントを設定しました: {start_date}〜{end_date} 時間帯: {time_slots}", ephemeral=True)

# /無期限イベント
@bot.tree.command(name="無期限イベント", description="無期限イベントを設定")
async def cmd_unlimited_event(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    event_state["type"] = "unlimited"
    await post_event_notice(interaction.guild)
    await interaction.response.send_message("無期限イベントを設定しました。", ephemeral=True)

# タスク：チャンネル権限制御 + 開始/終了メッセージ
async def event_timer_task():
    global _event_last_status
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    ch = guild.get_channel(MATCHING_CHANNEL_ID)
    if not ch:
        return
    while True:
        now = datetime.now(JST)
        can_send = False
        if event_state["type"] == "single":
            start = event_state["single"]["start"]
            end = event_state["single"]["end"]
            if start <= now <= end:
                can_send = True
        elif event_state["type"] == "long":
            today = now.date()
            if event_state["long"]["start_date"] <= today <= event_state["long"]["end_date"]:
                for s_time, e_time in event_state["long"]["time_slots"]:
                    start_dt = datetime.combine(today, s_time)
                    end_dt   = datetime.combine(today, e_time)
                    if start_dt <= now <= end_dt:
                        can_send = True
                        break
        elif event_state["type"] == "unlimited":
            can_send = True

        await ch.set_permissions(guild.default_role, send_messages=can_send)

        if _event_last_status != can_send:
            if can_send:
                await ch.send("⏰ 対戦時間開始！このチャンネルで /マッチ希望 ができます")
            else:
                await ch.send("⏰ 対戦時間終了！ /マッチ希望 を締め切らせていただきます")
            _event_last_status = can_send

        await asyncio.sleep(30)

bot.loop.create_task(event_timer_task())

# ----------------------------------------
# 起動処理
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    await bot.tree.sync()

bot.run(DISCORD_TOKEN)
