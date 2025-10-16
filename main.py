# main.py — フル実装完全版
import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import asyncio

# --- 必須設定（環境変数） ---
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RANKING_CHANNEL_ID = 1427542200614387846  # 指定済み

# --- タイムゾーン ---
JST = ZoneInfo("Asia/Tokyo")

# --- 階級設定（5pt刻み、25pt以上はChallenger）---
RANKS = [
    {"name": "Beginner", "min": 0,  "max": 4,  "emoji": "🔰"},
    {"name": "Silver",   "min": 5,  "max": 9,  "emoji": "🥈"},
    {"name": "Gold",     "min": 10, "max": 14, "emoji": "🥇"},
    {"name": "Master",   "min": 15, "max": 19, "emoji": "⚔️"},
    {"name": "GroundMaster", "min": 20, "max": 24, "emoji": "🪽"},
    {"name": "Challenger",   "min": 25, "max": 10**9, "emoji": "😈"},
]

PROMOTION_THRESHOLDS = [4, 9, 14, 19, 24]  # チャレンジ突入pt

# --- Bot 初期化 ---
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- 内部データ（メモリ） ---
# players[user_id] = {
#   "pt": int,
#   "ever_reached_gold": bool,
#   "challenge": bool,
#   "challenge_start_pt": int,
#   "rank_index": int
# }
players = {}

# pending_matches: challenger_id -> opponent_id  (申請済、未承認)
pending_matches = {}

# approved_matches: (challenger, opponent) -> True (事前承認済)
approved_matches = {}  # key = (challenger_id, opponent_id)

# pending_reports: (winner_id, loser_id) -> {"time": datetime, "task": asyncio.Task}
# created when winner reports; loser must approve within 30 minutes else auto-approved
pending_reports = {}

# helpers ------------------------------------------------
def get_rank_index_by_pt(pt: int) -> int:
    for i, r in enumerate(RANKS):
        if r["min"] <= pt <= r["max"]:
            return i
    return 0

def rank_text_by_index(idx: int) -> str:
    r = RANKS[idx]
    return f"{r['emoji']} {r['name']}"

def ensure_player(user_id: int):
    if user_id not in players:
        players[user_id] = {
            "pt": 0,
            "ever_reached_gold": False,
            "challenge": False,
            "challenge_start_pt": None,
            "rank_index": get_rank_index_by_pt(0)
        }

async def ensure_rank_roles(guild: discord.Guild):
    """
    サーバーにランクロールがなければ作る（BotにManage Roles権限が必要）。
    ロール名は RANKS[].name を使用します。
    """
    existing = {r.name: r for r in guild.roles}
    created = False
    for r in RANKS:
        if r["name"] not in existing:
            try:
                await guild.create_role(name=r["name"], reason="Auto-create rank role for ranking bot")
                created = True
            except Exception as e:
                print(f"[WARN] failed to create role {r['name']}: {e}")
    if created:
        print("[INFO] Some rank roles were created. Please check their position/permissions.")

async def set_roles_for_member(guild: discord.Guild, member: discord.Member, rank_idx: int):
    """対象メンバーに該当ランクロールを付与し、他のランクロールを外す."""
    try:
        rank_role = discord.utils.get(guild.roles, name=RANKS[rank_idx]["name"])
        if rank_role is None:
            return
        # remove other rank roles if present
        to_remove = [r for r in guild.roles if r.name in [x["name"] for x in RANKS] and r in member.roles and r != rank_role]
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="Update rank roles")
            except Exception as e:
                pass
        if rank_role not in member.roles:
            try:
                await member.add_roles(rank_role, reason="Assign rank role")
            except Exception as e:
                pass
    except Exception as e:
        print(f"[WARN] set_roles_for_member error: {e}")

async def update_member_display(user_id: int):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    member = guild.get_member(user_id)
    if not member:
        return
    ensure_player(user_id)
    info = players[user_id]
    rank_idx = get_rank_index_by_pt(info["pt"])
    info["rank_index"] = rank_idx
    # format nickname: "<base name>  (emoji pt 🔥?)"
    emoji = RANKS[rank_idx]["emoji"]
    flame = " 🔥" if info.get("challenge") else ""
    suffix = f"{emoji} {info['pt']}pt{flame}"
    # Try to preserve base name (split at ' | ' if we used that format before)
    base = member.name
    # Set nick
    try:
        await member.edit(nick=f"{base} | {suffix}", reason="Update rank display")
    except Exception as e:
        # likely missing Manage Nicknames or role hierarchy
        print(f"[WARN] cannot edit nick for {member}: {e}")
    # set roles
    await set_roles_for_member(guild, member, rank_idx)

# pt calculation rules -----------------------------------
def can_match(u1_id: int, u2_id: int) -> (bool, str):
    """マッチ可否チェック: 3階級以上離れていないか、チャレンジ中制限など"""
    ensure_player(u1_id); ensure_player(u2_id)
    r1 = players[u1_id]["rank_index"]
    r2 = players[u2_id]["rank_index"]
    if abs(r1 - r2) >= 3:
        return False, "3階級以上離れた相手とはマッチできません。"
    # チャレンジ中は"同階級以上"のみマッチ可
    if players[u1_id].get("challenge"):
        if players[u2_id]["rank_index"] < players[u1_id]["rank_index"]:
            return False, "昇級チャレンジ中は同階級以上の相手としかマッチできません。"
    if players[u2_id].get("challenge"):
        if players[u1_id]["rank_index"] < players[u2_id]["rank_index"]:
            return False, "相手は昇級チャレンジ中のためマッチできません。"
    return True, ""

def apply_pt_change(winner_id: int, loser_id: int):
    """
    ルール:
    - 同階級: winner +1 , loser -1
    - 階級差あり:
       - 低い側（winner が rank_index 小さい）:
         勝てば: +1 + 階級差分
         負ければ: -1
       - 高い側:
         勝てば: +1
         負ければ: -1 - 階級差分
    - Ptは下限0。Gold到達後は10pt以下に下がらない（ever_reached_goldルール）
    """
    ensure_player(winner_id); ensure_player(loser_id)
    w = players[winner_id]; l = players[loser_id]
    w_idx = w["rank_index"]; l_idx = l["rank_index"]
    diff = w_idx - l_idx  # positive if winner higher
    if diff == 0:
        w["pt"] += 1
        l["pt"] = max(l["pt"] - 1, 0)
    elif diff < 0:
        # winner was lower-ranked
        gain = 1 + abs(diff)
        w["pt"] += gain
        l["pt"] = max(l["pt"] - 1, 0)
    else:
        # winner was higher-ranked
        w["pt"] += 1
        l["pt"] = max(l["pt"] - (1 + diff), 0)
    # Gold到達一度あれば10pt以下には下がらない
    for uid in (winner_id, loser_id):
        if players[uid]["pt"] >= 10:
            players[uid]["ever_reached_gold"] = True
        if players[uid].get("ever_reached_gold") and players[uid]["pt"] < 10:
            players[uid]["pt"] = 10

    # 昇級チャレンジ判定: 到達ptが 4,9,14,19,24 のときチャレンジ突入
    for uid in (winner_id, loser_id):
        pt = players[uid]["pt"]
        if pt in PROMOTION_THRESHOLDS:
            players[uid]["challenge"] = True
            players[uid]["challenge_start_pt"] = pt
        # if in challenge but lost once — failure rules handled by report flow
    # update rank_index
    players[winner_id]["rank_index"] = get_rank_index_by_pt(players[winner_id]["pt"])
    players[loser_id]["rank_index"] = get_rank_index_by_pt(players[loser_id]["pt"])

# match/report flow ------------------------------------------------
async def schedule_auto_approve(winner_id: int, loser_id: int, report_key):
    """敗者が30分承認しなければ自動承認（pt反映）"""
    await asyncio.sleep(30*60)  # 30 minutes
    # if still pending, auto apply
    if report_key in pending_reports:
        # perform finalization
        await finalize_report(winner_id, loser_id)
        # notify channel / DM
        guild = bot.get_guild(GUILD_ID)
        chan = guild.get_channel(RANKING_CHANNEL_ID)
        if chan:
            await chan.send(f"自動承認: <@{winner_id}> vs <@{loser_id}> の報告を自動で承認しました。")

async def finalize_report(winner_id: int, loser_id: int):
    # apply pt changes & update displays, clear pending_reports
    apply_pt_change(winner_id, loser_id)
    await update_member_display(winner_id)
    await update_member_display(loser_id)
    # clear any pending_reports entry
    key = (winner_id, loser_id)
    if key in pending_reports:
        task = pending_reports[key]["task"]
        if not task.done():
            task.cancel()
    pending_reports.pop(key, None)

# --- Slash commands & views --- #
@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} ready")
    g = bot.get_guild(GUILD_ID)
    if g:
        # ensure rank roles exist (optional)
        await ensure_rank_roles(g)
    # sync commands to guild
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("[INFO] command tree synced to guild")
    except Exception as e:
        print("[WARN] command sync:", e)
    # start ranking task (checks JST times)
    ranking_task.start()

# /イベント設定 (管理者専用)
@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="イベント設定", description="イベントの開始・終了日時を設定(YYYY-MM-DD HH:MM)")
@app_commands.describe(start="開始 (YYYY-MM-DD HH:MM, JST)", end="終了 (YYYY-MM-DD HH:MM, JST)")
async def cmd_event_setting(interaction: discord.Interaction, start: str, end: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ実行可", ephemeral=True)
        return
    try:
        # parse in JST
        s = datetime.fromisoformat(start.replace(" ", "T"))
        e = datetime.fromisoformat(end.replace(" ", "T"))
        # store or announce
        await interaction.response.send_message(f"イベント設定: {s} ～ {e}", ephemeral=True)
    except Exception as ex:
        await interaction.response.send_message(f"日時形式エラー: {ex}", ephemeral=True)

# /マッチング申請
@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="マッチング申請", description="対戦相手に申請する")
@app_commands.describe(opponent="対戦相手(メンバー)")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    challenger = interaction.user
    opponent_member = opponent
    if challenger.id == opponent_member.id:
        await interaction.response.send_message("自分には申請できません", ephemeral=True)
        return
    ensure_player(challenger.id); ensure_player(opponent_member.id)
    ok, reason = can_match(challenger.id, opponent_member.id)
    if not ok:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    # register pending match
    pending_matches[challenger.id] = opponent_member.id

    # build ApproveView (only opponent can press)
    class ApproveView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
        @discord.ui.button(label="承認", style=discord.ButtonStyle.green)
        async def approve(self, button: discord.ui.Button, i: discord.Interaction):
            # i is Interaction
            if i.user.id != opponent_member.id:
                await i.response.send_message("あなたは承認できません", ephemeral=True)
                return
            # mark approved
            approved_matches[(challenger.id, opponent_member.id)] = True
            # remove from pending_matches
            pending_matches.pop(challenger.id, None)
            await i.response.send_message(f"{opponent_member.display_name} が承認しました。試合後は勝者が /試合結果報告 を実行してください。", ephemeral=True)
            # Optionally notify challenger
            try:
                await challenger.send(f"{opponent_member.display_name} によりマッチング承認されました。")
            except:
                pass

    await interaction.response.send_message(f"{opponent_member.mention} にマッチング申請しました。承認を待ってください。", view=ApproveView(), ephemeral=True)

# /試合結果報告 — 勝者が報告（敗者が承認 or 30分で自動承認）
@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="試合結果報告", description="勝者が試合結果を報告する")
@app_commands.describe(loser="敗者(申請済の対戦相手)")
async def cmd_report_result(interaction: discord.Interaction, loser: discord.Member):
    winner = interaction.user
    loser_member = loser
    # require that this pair was approved
    if not approved_matches.get((winner.id, loser_member.id)):
        await interaction.response.send_message("事前承認済みのマッチングではありません（/マッチング申請→承認 を行ってください）", ephemeral=True)
        return
    # create approval prompt to loser (buttons approve/reject) and start 30-min auto
    key = (winner.id, loser_member.id)
    if key in pending_reports:
        await interaction.response.send_message("既に報告が出されています。承認待ちです。", ephemeral=True)
        return

    view = discord.ui.View(timeout=None)
    async def loser_approve_callback(i: discord.Interaction):
        if i.user.id != loser_member.id:
            await i.response.send_message("あなた以外は承認できません", ephemeral=True)
            return
        # finalize immediately
        await finalize_report(winner.id, loser_member.id)
        pending_reports.pop(key, None)
        await i.response.send_message("敗者による承認を受領しました。結果を反映しました。", ephemeral=True)
        # remove approved_matches entry
        approved_matches.pop(key, None)

    async def loser_reject_callback(i: discord.Interaction):
        if i.user.id != loser_member.id:
            await i.response.send_message("あなた以外は拒否できません", ephemeral=True)
            return
        # rejection: cancel pending, notify
        pending_reports.pop(key, None)
        approved_matches.pop(key, None)
        await i.response.send_message("敗者が結果に異議を申し立てました。管理者が審議してください。", ephemeral=True)
        # notify admin
        try:
            admin = bot.get_user(ADMIN_ID)
            if admin:
                await admin.send(f"対戦結果に異議あり: {winner.mention} vs {loser.mention}. 審議をお願いします.")
        except:
            pass

    approve_button = discord.ui.Button(label="承認（敗者）", style=discord.ButtonStyle.green)
    reject_button = discord.ui.Button(label="異議を申し立てる", style=discord.ButtonStyle.red)
    approve_button.callback = loser_approve_callback
    reject_button.callback = loser_reject_callback
    view.add_item(approve_button)
    view.add_item(reject_button)

    # send ephemeral confirmation to winner and message to loser with buttons
    await interaction.response.send_message("勝利報告を受け付けました。敗者の承認を待ちます（30分で自動承認）", ephemeral=True)
    try:
        # post to loser in guild channel (or DM). We'll try to DM first:
        await loser_member.send(f"{winner.display_name} があなたに対して勝利報告をしました。承認または異議を選択してください。", view=view)
    except:
        # fallback: post in same channel (ephemeral not suitable), send guild message
        chan = interaction.channel
        await chan.send(f"{loser_member.mention} に対し {winner.display_name} が勝利報告を行いました。承認または異議を選択してください。", view=view)

    # schedule auto-approve
    task = asyncio.create_task(schedule_auto_approve(winner.id, loser_member.id, key))
    pending_reports[key] = {"time": datetime.now(), "task": task}

# /pt操作 (管理者)
@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="pt操作", description="管理者がユーザーのptを操作")
@app_commands.describe(target="対象ユーザー", pt="設定または増減 (例: +3, -1, 10)")
async def cmd_pt_operation(interaction: discord.Interaction, target: discord.Member, pt: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ実行可", ephemeral=True)
        return
    # interpret pt param: if startswith + or -, treat as delta, else absolute set
    try:
        if pt.startswith(("+", "-")):
            delta = int(pt)
            ensure_player(target.id)
            players[target.id]["pt"] += delta
        else:
            val = int(pt)
            ensure_player(target.id)
            players[target.id]["pt"] = val
        # update rank flags
        if players[target.id]["pt"] >= 10:
            players[target.id]["ever_reached_gold"] = True
        players[target.id]["rank_index"] = get_rank_index_by_pt(players[target.id]["pt"])
        await update_member_display(target.id)
        await interaction.response.send_message(f"{target.display_name} のptを更新しました（現在 {players[target.id]['pt']}pt）", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

# /ランキング手動表示 (管理者/ボット)
@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="ランキング表示", description="現在のランキングを表示")
async def cmd_show_ranking(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ実行可", ephemeral=True)
        return
    # build and send ranking
    lines = []
    sorted_players = sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True)
    for uid, data in sorted_players:
        member = bot.get_guild(GUILD_ID).get_member(uid)
        if member:
            flame = " 🔥" if data.get("challenge") else ""
            idx = data.get("rank_index", get_rank_index_by_pt(data["pt"]))
            lines.append(f"{member.mention}: {RANKS[idx]['emoji']} {data['pt']}pt{flame}")
    if not lines:
        await interaction.response.send_message("プレイヤーデータがありません", ephemeral=True)
    else:
        chan = bot.get_channel(RANKING_CHANNEL_ID)
        if chan:
            await chan.send("**ランキング**\n" + "\n".join(lines))
        await interaction.response.send_message("ランキングを投稿しました", ephemeral=True)

# ranking_task: post at JST 13:00 and 22:00
@tasks.loop(time=[time(13,0,tzinfo=JST), time(22,0,tzinfo=JST)])
async def ranking_task():
    chan = bot.get_channel(RANKING_CHANNEL_ID)
    if not chan:
        print("[WARN] ranking channel not found")
        return
    # build ranking text
    lines = []
    for uid, data in sorted(players.items(), key=lambda x: x[1]["pt"], reverse=True):
        member = bot.get_guild(GUILD_ID).get_member(uid)
        if member:
            idx = data.get("rank_index", get_rank_index_by_pt(data["pt"]))
            flame = " 🔥" if data.get("challenge") else ""
            lines.append(f"{member.mention}: {RANKS[idx]['emoji']} {data['pt']}pt{flame}")
    if not lines:
        await chan.send("ランキング（参加者データがありません）")
    else:
        await chan.send("**ランキング**\n" + "\n".join(lines))

# error handler for app commands
@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    try:
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message("権限がありません", ephemeral=True)
        else:
            await interaction.response.send_message(f"エラーが発生しました: {error}", ephemeral=True)
    except:
        pass

# run
if __name__ == "__main__":
    bot.run(TOKEN)
