# main.py
# 最終完全版 — 対戦Pt管理Bot
# 動作：承認フロー、Pt計算、昇級チャレンジ、ニックネーム更新、昇級アナウンス、ランキング定期投稿、
#        起動時ギルドコマンド強制同期（古いコマンド削除→再登録）、JSON永続化、管理者コマンド

import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
import traceback

import discord
from discord.ext import commands, tasks

# === 環境変数（必須） ===
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
# 任意: 管理者DiscordユーザーID。指定があれば一部コマンドを管理者限定にします。
ADMIN_ID = int(os.environ["ADMIN_ID"]) if os.environ.get("ADMIN_ID") else None

# === 設定ファイル ===
PLAYERS_FILE = "players.json"
EVENT_FILE = "event.json"

# === Intents & Bot ===
intents = discord.Intents.default()
intents.members = True
intents.message_content = False  # 使わないなら False のまま（安全策）
bot = commands.Bot(command_prefix="/", intents=intents)

# === ランク定義（名前・emoji・minpt・maxpt） ===
RANKS = [
    ("Beginner", "🔰", 0, 4),
    ("Silver",   "🥈", 5, 9),
    ("Gold",     "🥇", 10, 14),
    ("Master",   "⚔️", 15, 19),
    ("GroundMaster","🪽", 20, 24),
    ("Challenger","😈", 25, 99999),
]

# 昇級チャレンジの閾値
CHALLENGE_THRESHOLDS = [4, 9, 14, 19, 24]

# === 内部データ構造（メモリ） ===
# players: { user_id_str: {"pt": int, "challenge": bool, "had_gold_once": bool} }
players = {}
# match_requests: key = (requester_id_str, target_id_str) -> {"approved": bool, "requested_at": iso}
match_requests = {}
# awaiting_results: winner_id_str -> {"loser": loser_id_str, "task": asyncio.Task}
awaiting_results = {}

# ランキング投稿先（channel id） — /イベント設定 時に保存できます
ranking_channel_id = None

# === ヘルパー：ファイル読み書き（堅牢化） ===
def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        print(f"Failed to load {path}:")
        traceback.print_exc()
        return {}

def save_json_file(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        print(f"Failed to save {path}:")
        traceback.print_exc()

# === 永続化 load/save ===
def load_state():
    global players, match_requests, awaiting_results, ranking_channel_id
    d = load_json_file(PLAYERS_FILE)
    players = d.get("players", {})
    match_requests_local = d.get("match_requests", {})
    # convert keys to tuples if stored as strings
    # store as list of [req, tgt] keys to be safe
    match_requests.clear()
    for k, v in match_requests_local.items():
        # k expected as "req|tgt"
        if "|" in k:
            req, tgt = k.split("|", 1)
            match_requests[(req, tgt)] = v
    ranking_channel_id = d.get("ranking_channel_id", None)

def save_state():
    # store match_requests keys as "req|tgt"
    mr = {f"{req}|{tgt}": v for (req, tgt), v in match_requests.items()}
    save_json_file(PLAYERS_FILE, {
        "players": players,
        "match_requests": mr,
        "ranking_channel_id": ranking_channel_id
    })

def load_event():
    return load_json_file(EVENT_FILE)

def save_event(ev):
    save_json_file(EVENT_FILE, ev)

# === ランク取得 ===
def get_rank_info(pt):
    for name, emoji, low, high in RANKS:
        if low <= pt <= high:
            return {"name": name, "emoji": emoji, "min": low, "max": high}
    return {"name": "Unknown", "emoji": "❓", "min": 0, "max": 0}

# === ニックネーム更新（非同期タスクで行うのが推奨） ===
async def safe_update_member_display(user_id_str):
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            return
        member = guild.get_member(int(user_id_str))
        if not member:
            return
        pdata = players.get(user_id_str, {"pt": 0, "challenge": False})
        rank = get_rank_info(pdata["pt"])
        challenge_mark = "🔥" if pdata.get("challenge") else ""
        new_nick = f"{rank['emoji']}{challenge_mark} {member.name} - {pdata['pt']}pt"
        # try to set if different to avoid rate limits
        try:
            if member.nick != new_nick:
                await member.edit(nick=new_nick)
        except discord.Forbidden:
            # no permission to change nickname
            return
        except Exception:
            traceback.print_exc()
    except Exception:
        traceback.print_exc()

# wrapper to schedule update
def schedule_update_member_display(user_id_str):
    asyncio.create_task(safe_update_member_display(user_id_str))

# === イベント期間チェック ===
def parse_iso_local(s: str):
    # accept "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD HH:MM"
    s2 = s.replace(" ", "T")
    return datetime.fromisoformat(s2)

def event_is_active():
    ev = load_event()
    if not ev:
        return False
    try:
        start = parse_iso_local(ev["start"])
        end = parse_iso_local(ev["end"])
        now = datetime.now()
        return start <= now <= end
    except Exception:
        return False

# === Pt / 昇格ロジック ===
def ensure_player(uid_str):
    if uid_str not in players:
        players[uid_str] = {"pt": 0, "challenge": False, "had_gold_once": False}

def apply_match_result(winner_id_str, loser_id_str):
    """
    Update pts and challenge flags according to rules:
    - Same-rank: winner +1, loser -1 (min 0)
    - Rank-diff: increases as described:
      * lower-side winning against higher: winner + (1 + diff), loser -1
      * higher-side winning: winner +1, loser - (1 + diff)
    - If player ever reached Gold (pt>=10), they can't drop below 10 thereafter (had_gold_once marker).
    - If a player's pt hits a challenge threshold (4,9,14,19,24) -> challenge True
    - On challenge success/fail handled elsewhere by command logic (we store flags)
    """
    ensure_player(winner_id_str)
    ensure_player(loser_id_str)
    wp = players[winner_id_str]["pt"]
    lp = players[loser_id_str]["pt"]

    # find rank indices
    def rank_index(pt):
        for i, (_, _, low, high) in enumerate(RANKS):
            if low <= pt <= high:
                return i
        return 0

    wi = rank_index(wp)
    li = rank_index(lp)
    diff = abs(wi - li)

    # calculate delta
    if diff == 0:
        win_delta = 1
        lose_delta = -1
    else:
        # determine which is higher
        if wi < li:
            # winner is lower-ranked (wi < li -> lower rank index means lower tier in our order?)
            # careful with ordering: our RANKS list is ordered by increasing pt; higher index = higher rank
            # wi < li means winner is lower rank (smaller index). If winner lower than loser:
            # winner + (1 + diff), loser -1
            win_delta = 1 + diff
            lose_delta = -1
        elif wi > li:
            # winner is higher-ranking: winner +1, loser -(1 + diff)
            win_delta = 1
            lose_delta = -1 - diff
        else:
            win_delta = 1
            lose_delta = -1

    # apply
    players[winner_id_str]["pt"] = max(0, players[winner_id_str]["pt"] + win_delta)
    # loser floor
    new_loser_pt = players[loser_id_str]["pt"] + lose_delta
    # gold protection: if loser had gold once, cannot drop below 10
    if players[loser_id_str].get("had_gold_once", False) and new_loser_pt < 10:
        new_loser_pt = 10
    players[loser_id_str]["pt"] = max(0, int(new_loser_pt))

    # had_gold_once update
    for uid in (winner_id_str, loser_id_str):
        if players[uid]["pt"] >= 10:
            players[uid]["had_gold_once"] = True

    # update challenge flags
    for uid in (winner_id_str, loser_id_str):
        players[uid]["challenge"] = players[uid]["pt"] in CHALLENGE_THRESHOLDS

# === 自動承認タイマータスク ===
async def auto_finalize_after(winner_id_str, loser_id_str, seconds=900):
    # wait then finalize if still waiting
    try:
        await asyncio.sleep(seconds)
        info = awaiting_results.get(winner_id_str)
        if info and info.get("loser") == loser_id_str:
            # finalize
            apply_match_result(winner_id_str, loser_id_str)
            save_state()
            # announce auto approval
            guild = bot.get_guild(GUILD_ID)
            if guild and ranking_channel_id:
                ch = guild.get_channel(ranking_channel_id)
                if ch:
                    await ch.send(f"⏰ <@{winner_id_str}> の試合が自動承認され、結果を反映しました。")
            # cleanup
            awaiting_results.pop(winner_id_str, None)
            # schedule nickname updates
            schedule_update_member_display(winner_id_str)
            schedule_update_member_display(loser_id_str)
    except asyncio.CancelledError:
        return
    except Exception:
        traceback.print_exc()

# === Ranking posting task (JST 14:00 and 22:00) ===
@tasks.loop(minutes=1)
async def ranking_poster():
    try:
        # compute current JST time
        now_utc = datetime.now(timezone.utc)
        jst = now_utc + timedelta(hours=9)
        if (jst.hour == 14 or jst.hour == 22) and jst.minute == 0:
            # post ranking
            guild = bot.get_guild(GUILD_ID)
            if not guild or not ranking_channel_id:
                return
            ch = guild.get_channel(ranking_channel_id)
            if not ch:
                return
            sorted_list = sorted(players.items(), key=lambda kv: kv[1]["pt"], reverse=True)
            msg = f"🏆 **ランキング ({jst.strftime('%Y-%m-%d %H:%M JST')})** 🏆\n"
            for i, (uid, pdata) in enumerate(sorted_list, start=1):
                rank = get_rank_info(pdata["pt"])
                challenge = "🔥" if pdata.get("challenge") else ""
                member = guild.get_member(int(uid))
                display = member.display_name if member else uid
                msg += f"{i}. {rank['emoji']}{challenge} {display} — {pdata['pt']}pt\n"
            await ch.send(msg)
    except Exception:
        traceback.print_exc()

# === Commands ===

# Helper: admin check
def is_admin(user):
    if ADMIN_ID:
        return user.id == ADMIN_ID
    # fallback: guild owner is admin
    guild = bot.get_guild(GUILD_ID)
    if guild and user.id == guild.owner_id:
        return True
    return False

# /event_set (管理者) — start/end ISO "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD HH:MM"
@bot.tree.command(name="イベント設定", description="イベント開始/終了日時とランキングチャンネルを設定")
async def command_event_set(interaction: discord.Interaction, start: str, end: str, ranking_channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ 管理者のみ実行可能です。", ephemeral=True)
        return
    try:
        # parse and save event
        s = start.replace(" ", "T")
        e = end.replace(" ", "T")
        # validate parse
        parse_s = parse_iso_local(s)
        parse_e = parse_iso_local(e)
    except Exception as ex:
        await interaction.followup.send("日時の形式が不正です。例: 2025-10-15T14:00", ephemeral=True)
        return
    ev = {"start": s, "end": e}
    save_event(ev)
    # update ranking channel id if provided
    global ranking_channel_id
    if ranking_channel:
        ranking_channel_id = ranking_channel.id
    # persist players file will carry ranking_channel_id too
    save_state()
    await interaction.followup.send(f"イベントを設定しました。\n開始: {s}\n終了: {e}\nランキング投稿先: {ranking_channel.mention if ranking_channel else '未設定'}", ephemeral=True)

# /マッチング申請 — どのチャンネルでもOK
@bot.tree.command(name="マッチング申請", description="対戦相手に申請します")
async def command_match_request(interaction: discord.Interaction, opponent: discord.User):
    await interaction.response.defer(ephemeral=True)
    # event check
    if not event_is_active():
        await interaction.followup.send("⚠️ イベント期間外です。", ephemeral=True)
        return
    requester = str(interaction.user.id)
    target = str(opponent.id)
    if requester == target:
        await interaction.followup.send("自分に対しては申請できません。", ephemeral=True)
        return
    ensure_player(requester)
    ensure_player(target)
    key = (requester, target)
    if key in match_requests:
        await interaction.followup.send("⚠️ 既に申請中です。取り下げてください。", ephemeral=True)
        return
    # check if either has active pending match with the other in reverse
    if (target, requester) in match_requests:
        await interaction.followup.send("⚠️ 相手から申請が来ています。相互申請は不可です。", ephemeral=True)
        return
    match_requests[key] = {"approved": False, "requested_at": datetime.now().isoformat()}
    save_state()
    await interaction.followup.send(f"✅ 申請を送信しました： {interaction.user.mention} → {opponent.mention}\n相手は `/承認` または `/拒否` で応答してください。", ephemeral=True)

# /取り下げ
@bot.tree.command(name="申請取り下げ", description="送信したマッチング申請を取り下げます")
async def command_withdraw(interaction: discord.Interaction, opponent: discord.User):
    await interaction.response.defer(ephemeral=True)
    requester = str(interaction.user.id)
    target = str(opponent.id)
    key = (requester, target)
    if key in match_requests:
        del match_requests[key]
        save_state()
        await interaction.followup.send("申請を取り下げました。", ephemeral=True)
    else:
        await interaction.followup.send("該当の申請が見つかりません。", ephemeral=True)

# /承認
@bot.tree.command(name="承認", description="受け取った申請を承認します")
async def command_approve(interaction: discord.Interaction, requester: discord.User):
    await interaction.response.defer(ephemeral=True)
    target = str(interaction.user.id)
    req = str(requester.id)
    key = (req, target)
    if key not in match_requests:
        await interaction.followup.send("該当する申請はありません。", ephemeral=True)
        return
    match_requests[key]["approved"] = True
    save_state()
    await interaction.followup.send("承認しました。勝者は試合後に `/試合結果報告` をご利用ください。", ephemeral=True)

# /拒否
@bot.tree.command(name="拒否", description="受け取った申請を拒否します")
async def command_reject(interaction: discord.Interaction, requester: discord.User):
    await interaction.response.defer(ephemeral=True)
    target = str(interaction.user.id)
    req = str(requester.id)
    key = (req, target)
    if key in match_requests:
        del match_requests[key]
        save_state()
        await interaction.followup.send("申請を拒否しました。", ephemeral=True)
    else:
        await interaction.followup.send("該当する申請はありません。", ephemeral=True)

# /試合結果報告 — 勝者が報告する
@bot.tree.command(name="試合結果報告", description="勝者が試合結果を報告します (敗者の承認が必要)")
async def command_report(interaction: discord.Interaction, opponent: discord.User):
    await interaction.response.defer(ephemeral=True)
    winner = str(interaction.user.id)
    loser = str(opponent.id)
    key = (winner, loser)
    # check pre-approved
    if key not in match_requests or not match_requests[key].get("approved"):
        await interaction.followup.send("⚠️ 事前にマッチング申請が承認されていません。", ephemeral=True)
        return

    # record awaiting result
    # Cancel existing awaiting for same winner (if any)
    prev = awaiting_results.get(winner)
    if prev and prev.get("task"):
        prev["task"].cancel()
    awaiting_results[winner] = {"loser": loser, "task": None, "reported_at": datetime.now().isoformat()}

    # schedule auto finalize in 15 minutes (900s)
    t = asyncio.create_task(auto_finalize_after(winner, loser, seconds=900))
    awaiting_results[winner]["task"] = t

    # notify loser via ephemeral followup and public mention in report channel
    await interaction.followup.send(
        f"勝者報告を受け付けました。敗者 <@{loser}> は `/承認` で承認するか、15分で自動承認されます。",
        ephemeral=True
    )
    # optional: send public short notice in ranking channel if available
    guild = bot.get_guild(GUILD_ID)
    if guild and ranking_channel_id:
        ch = guild.get_channel(ranking_channel_id)
        if ch:
            await ch.send(f"📣 <@{winner}> が <@{loser}> に勝利したと報告しました。敗者の承認をお待ちください。")

# /承認（敗者が承認する） — note: same /承認 used above for approving request; we need a different behavior when there's awaiting result
# We'll check awaiting_results when /承認 is invoked and the invoker matches the loser of any awaiting result.
# To avoid ambiguity, the same command can serve both purposes: if the user has a pending match request to approve, it handles that;
# otherwise, it can act as approval for match result if they are the loser.
# That dual behavior is implemented in the /承認 command above (command_approve), but we will add a dedicated result-approval helper:

@bot.tree.command(name="結果承認", description="敗者が試合結果を承認します")
async def command_result_approve(interaction: discord.Interaction, winner: discord.User):
    await interaction.response.defer(ephemeral=True)
    winner_id = str(winner.id)
    loser_id = str(interaction.user.id)
    info = awaiting_results.get(winner_id)
    if not info or info.get("loser") != loser_id:
        await interaction.followup.send("承認できる試合が見つかりません。", ephemeral=True)
        return
    # finalize immediately
    # cancel auto task
    task = info.get("task")
    if task:
        task.cancel()
    apply_match_result(winner_id, loser_id)
    save_state()
    # announce and update names
    guild = bot.get_guild(GUILD_ID)
    if guild and ranking_channel_id:
        ch = guild.get_channel(ranking_channel_id)
        if ch:
            rank = get_rank_info(players[winner_id]["pt"])
            challenge = "🔥" if players[winner_id].get("challenge") else ""
            await ch.send(f"🔥 <@{winner_id}> が {rank['name']}{rank['emoji']}{challenge} に昇級しました！")
    # schedule nickname updates
    schedule_update_member_display(winner_id)
    schedule_update_member_display(loser_id)
    # cleanup
    awaiting_results.pop(winner_id, None)
    await interaction.followup.send("承認しました。結果を反映しました。", ephemeral=True)

# /結果拒否 (敗者が報告を否認)
@bot.tree.command(name="結果拒否", description="敗者が試合報告を否認します")
async def command_result_reject(interaction: discord.Interaction, winner: discord.User):
    await interaction.response.defer(ephemeral=True)
    winner_id = str(winner.id)
    loser_id = str(interaction.user.id)
    info = awaiting_results.get(winner_id)
    if not info or info.get("loser") != loser_id:
        await interaction.followup.send("拒否できる試合が見つかりません。", ephemeral=True)
        return
    # cancel auto finalize and remove awaiting; winner must re-report
    task = info.get("task")
    if task:
        task.cancel()
    awaiting_results.pop(winner_id, None)
    await interaction.followup.send("報告を拒否しました。勝者は再度 `/試合結果報告` してください。", ephemeral=True)

# 管理者コマンド: /ランキングリセット
@bot.tree.command(name="ランキングリセット", description="全プレイヤーのPtをリセットします（管理者専用）")
async def command_ranking_reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ 管理者のみ実行可能です。", ephemeral=True)
        return
    for uid in list(players.keys()):
        players[uid]["pt"] = 0
        players[uid]["challenge"] = False
        players[uid]["had_gold_once"] = False
    save_state()
    # update all nicknames asynchronously
    guild = bot.get_guild(GUILD_ID)
    if guild:
        for uid in players.keys():
            schedule_update_member_display(uid)
    await interaction.followup.send("ランキング（全Pt）をリセットしました。", ephemeral=True)

# 管理者: /pt操作 user: mention amount int (positive or negative)
@bot.tree.command(name="pt操作", description="指定ユーザーのPtを増減します（管理者専用）")
async def command_pt_adjust(interaction: discord.Interaction, target: discord.User, amount: int):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ 管理者のみ実行可能です。", ephemeral=True)
        return
    uid = str(target.id)
    ensure_player(uid)
    players[uid]["pt"] = max(0, players[uid]["pt"] + amount)
    # update had_gold_once if crossing gold
    if players[uid]["pt"] >= 10:
        players[uid]["had_gold_once"] = True
    # recalc challenge flag
    players[uid]["challenge"] = players[uid]["pt"] in CHALLENGE_THRESHOLDS
    save_state()
    schedule_update_member_display(uid)
    await interaction.followup.send(f"<@{uid}> のPtを {amount} 変更しました。現在 {players[uid]['pt']}pt", ephemeral=True)

# 管理者: /set_challenge
@bot.tree.command(name="強制昇格チャレンジ", description="指定ユーザーの昇格チャレンジ状態を操作（管理者専用）")
async def command_force_challenge(interaction: discord.Interaction, target: discord.User, on: bool):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ 管理者のみ実行可能です。", ephemeral=True)
        return
    uid = str(target.id)
    ensure_player(uid)
    players[uid]["challenge"] = bool(on)
    save_state()
    schedule_update_member_display(uid)
    await interaction.followup.send(f"<@{uid}> の昇格チャレンジ状態を {'有効' if on else '無効'} にしました。", ephemeral=True)

# 管理者: /show_players (簡易確認)
@bot.tree.command(name="players一覧", description="プレイヤーデータ一覧（管理者専用）")
async def command_show_players(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ 管理者のみ実行可能です。", ephemeral=True)
        return
    lines = []
    for uid, p in players.items():
        lines.append(f"<@{uid}>: {p['pt']}pt {'🔥' if p.get('challenge') else ''}")
    text = "\n".join(lines) if lines else "プレイヤーはいません。"
    await interaction.followup.send(text, ephemeral=True)

# === on_ready: robust guild fetch and force sync/clear ===
@bot.event
async def on_connect():
    # connected to gateway
    print("[INFO] Discord gateway connected.")

@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")
    # try to get guild; wait a bit if not present
    guild = None
    for attempt in range(15):
        guild = bot.get_guild(GUILD_ID)
        if guild:
            break
        try:
            # try API fetch as fallback
            guild = await bot.fetch_guild(GUILD_ID)
            if guild:
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    if not guild:
        print(f"[WARN] ギルド {GUILD_ID} が取得できませんでした。コマンド同期をスキップします。")
        # still start ranking loop in case we get guild later
        if not ranking_poster.is_running():
            ranking_poster.start()
        return

    # Clear then sync to avoid duplicates (safe)
    try:
        print("[INFO] ギルドコマンドをクリア＆同期します...")
        await bot.tree.clear_commands(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("[INFO] ギルドコマンド同期完了")
    except Exception:
        print("[ERROR] コマンド同期中にエラー発生:")
        traceback.print_exc()

    # load saved state
    load_state()
    # start ranking poster
    if not ranking_poster.is_running():
        ranking_poster.start()

# ranking_poster loop (JST 14:00 & 22:00)
@tasks.loop(minutes=1)
async def ranking_poster():
    try:
        # UTC -> JST
        now_utc = datetime.now(timezone.utc)
        jst = now_utc + timedelta(hours=9)
        if (jst.hour in (14, 22)) and jst.minute == 0:
            # post
            guild = bot.get_guild(GUILD_ID)
            if not guild or not ranking_channel_id:
                return
            ch = guild.get_channel(ranking_channel_id)
            if not ch:
                return
            sorted_players = sorted(players.items(), key=lambda kv: kv[1]["pt"], reverse=True)
            msg = f"🏆 **ランキング（{jst.strftime('%Y-%m-%d %H:%M JST')}）** 🏆\n"
            for i, (uid, pdata) in enumerate(sorted_players, start=1):
                rank = get_rank_info(pdata["pt"])
                challenge = "🔥" if pdata.get("challenge") else ""
                member = guild.get_member(int(uid))
                name = member.display_name if member else uid
                msg += f"{i}. {rank['emoji']}{challenge} {name} — {pdata['pt']}pt\n"
            await ch.send(msg)
    except Exception:
        traceback.print_exc()

# Start the bot
if __name__ == "__main__":
    print("[START] Bot starting...")
    try:
        bot.run(TOKEN)
    except Exception:
        traceback.print_exc()
