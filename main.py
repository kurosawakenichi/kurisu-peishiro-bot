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
BATTLE_CATEGORY_ID = 1427541907579605012  # 固定
ACTIVE_CHANNEL_ID = int(os.environ["ACTIVE_CHANNEL_ID"])  # #アクティブ0名 チャンネルID

# JSTタイムゾーン
JST = timezone(timedelta(hours=+9))
AUTO_APPROVE_SECONDS = 300  # 5分

# ----------------------------------------
# 内部データ
# ----------------------------------------
user_data = {}               # user_id -> {"pt": int}
matching = {}                # 現在マッチ中のプレイヤー組
waiting_list = {}            # user_id -> {"expires": datetime, "task": asyncio.Task, "interaction": discord.Interaction}
matching_channels = {}       # user_id -> 専用チャンネルID（v2用）

# ----------------------------------------
# ランク定義（表示用）6段階
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

async def update_active_player_count():
    """アクティブプレイヤー数（待機中＋対戦中）の更新"""
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(ACTIVE_CHANNEL_ID)
    if channel:
        count = len(waiting_list) + len(matching)
        # matching はペアで入っているので /2
        count = len(waiting_list) + len(matching)//2
        try:
            await channel.edit(name=f"アクティブ{count}名")
        except Exception as e:
            print(f"Failed to update active count: {e}")

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

            # 待機タスク削除
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

            # 降参ボタン付きマッチ開始メッセージ
            await battle_ch.send(
                f"<@{u1}> vs <@{u2}> のマッチングが成立しました。\n"
                f"勝者は /勝利報告 を使用してください。",
                view=ConcedeView(u1, u2, battle_ch.id)
            )

            # 待機メッセージ更新
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
            await update_active_player_count()
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
        await update_active_player_count()

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
    # 待機タイマーリセット
    for uid2, info in waiting_list.items():
        info["task"].cancel()
        info["task"] = asyncio.create_task(waiting_timer(uid2))
        info["interaction"] = info.get("interaction", interaction)
    await asyncio.sleep(5)
    await try_match_users()
    await update_active_player_count()

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
            await update_active_player_count()
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
# /勝利報告 コマンド
# ----------------------------------------
class ConcedeView(discord.ui.View):
    def __init__(self, user1:int, user2:int, battle_ch_id:int):
        super().__init__(timeout=None)
        self.user1 = user1
        self.user2 = user2
        self.battle_ch_id = battle_ch_id
        self.processed = False

    @discord.ui.button(label="降参", style=discord.ButtonStyle.danger)
    async def concede(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [self.user1, self.user2]:
            await interaction.response.send_message("この試合の当事者ではありません。", ephemeral=True)
            return
        if self.processed:
            await interaction.response.send_message("既に処理済みです。", ephemeral=True)
            return
        self.processed = True
        winner_id = self.user2 if interaction.user.id == self.user1 else self.user1
        loser_id = interaction.user.id
        await handle_approved_result(winner_id, loser_id, interaction.guild, self.battle_ch_id)
        await interaction.response.edit_message(content=f"<@{loser_id}> が降参しました。試合終了です。", view=None)

@bot.tree.command(name="勝利報告", description="自分が勝者であることを報告します")
async def cmd_victory_report(interaction: discord.Interaction):
    winner_id = interaction.user.id
    if winner_id not in matching_channels:
        await interaction.response.send_message("あなたは現在試合中ではありません。", ephemeral=True)
        return
    battle_ch_id = matching_channels[winner_id]
    loser_id = matching.get(winner_id)
    if not loser_id:
        await interaction.response.send_message("対戦相手が見つかりません。", ephemeral=True)
        return
    await handle_approved_result(winner_id, loser_id, interaction.guild, battle_ch_id)
    await interaction.response.send_message("勝利報告を受け付けました。結果を反映しました。", ephemeral=True)
    await update_active_player_count()

async def handle_approved_result(winner_id:int, loser_id:int, guild: discord.Guild, battle_ch_id:int):
    if not is_registered_match(winner_id, loser_id):
        return
    winner_pt = user_data.get(winner_id, {}).get("pt", 0)
    loser_pt  = user_data.get(loser_id, {}).get("pt", 0)
    winner_new = calculate_pt(winner_pt, loser_pt, "win")
    loser_new  = calculate_pt(loser_pt, winner_pt, "lose")
    user_data.setdefault(winner_id, {})["pt"] = winner_new
    user_data.setdefault(loser_id, {})["pt"] = loser_new

    # メンバー表示更新
    w_member = guild.get_member(winner_id)
    l_member = guild.get_member(loser_id)
    if w_member:
        await update_member_display(w_member)
    if l_member:
        await update_member_display(l_member)

    matching.pop(winner_id, None)
    matching.pop(loser_id, None)
    matching_channels.pop(winner_id, None)
    matching_channels.pop(loser_id, None)

    # 対戦ログ記録
    log_ch = guild.get_channel(BATTLELOG_CHANNEL_ID)
    if log_ch:
        await log_ch.send(
            f"[勝者確定] {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} - <@{winner_id}> 勝利 vs <@{loser_id}> 敗北"
        )

    # 専用チャンネル削除
    battle_ch = guild.get_channel(battle_ch_id)
    if battle_ch:
        await battle_ch.delete()

    delta_w = winner_new - winner_pt
    delta_l = loser_new - loser_pt
    if log_ch:
        await log_ch.send(f"✅ <@{winner_id}> に +{delta_w}pt／<@{loser_id}> に {delta_l}pt の反映を行いました。")

    await update_active_player_count()

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

# ----------------------------------------
# 起動処理
# ----------------------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is ready. Guilds: {[g.name for g in bot.guilds]}")
    await bot.tree.sync()
    await update_active_player_count()

bot.run(DISCORD_TOKEN)
