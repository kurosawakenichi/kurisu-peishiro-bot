import os
import json
import asyncio
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import traceback

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

STATE_FILE = "state.json"

# ============================================================
# 状態管理
# ============================================================

state = {
    "players": {},
    "event_start": None,
    "event_end": None,
    "ranking_channel": None,
}

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4)

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            state.update(data)
    except FileNotFoundError:
        pass

# ============================================================
# ランキング自動投稿
# ============================================================

@tasks.loop(hours=8)
async def ranking_poster():
    if not state["ranking_channel"]:
        return
    now = datetime.now()
    if state["event_start"] and state["event_end"]:
        if not (state["event_start"] <= now <= state["event_end"]):
            return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(state["ranking_channel"])
    if not channel:
        return
    sorted_players = sorted(
        state["players"].items(),
        key=lambda x: x[1]["pt"],
        reverse=True
    )
    if not sorted_players:
        await channel.send("まだランキングはありません。")
        return
    lines = ["🏆 現在のランキング 🏆\n"]
    for i, (uid, info) in enumerate(sorted_players, 1):
        member = guild.get_member(int(uid))
        name = member.display_name if member else f"ユーザーID:{uid}"
        lines.append(f"{i}. {name} - {info['pt']}pt")
    await channel.send("\n".join(lines))

# ============================================================
# コマンド群
# ============================================================

@bot.tree.command(name="イベント設定", description="イベントの開始・終了・ランキングチャンネルを設定します（管理者専用）")
@commands.has_permissions(administrator=True)
async def イベント設定(interaction: discord.Interaction, 
                 start: str, end: str, ranking_channel: discord.TextChannel):
    """
    例: /イベント設定 start:2025-10-15T00:00 end:2025-10-20T23:59 ranking_channel:#ランキング
    """
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        state["event_start"] = start_dt
        state["event_end"] = end_dt
        state["ranking_channel"] = ranking_channel.id
        save_state()
        await interaction.response.send_message(
            f"✅ イベントを設定しました。\n開始: {start_dt}\n終了: {end_dt}\nランキングチャンネル: {ranking_channel.mention}",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"エラーが発生しました: {e}", ephemeral=True)

@bot.tree.command(name="ランキングリセット", description="ランキングデータを全リセットします（管理者専用）")
@commands.has_permissions(administrator=True)
async def ランキングリセット(interaction: discord.Interaction):
    state["players"].clear()
    save_state()
    await interaction.response.send_message("ランキングをリセットしました。", ephemeral=True)

# ============================================================
# 起動イベント
# ============================================================

@bot.event
async def on_ready():
    print(f"[INFO] {bot.user} is ready.")
    guild = None
    for attempt in range(15):
        guild = bot.get_guild(GUILD_ID)
        if guild:
            break
        try:
            guild = await bot.fetch_guild(GUILD_ID)
            if guild:
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    if not guild:
        print(f"[WARN] ギルド {GUILD_ID} が取得できませんでした。コマンド同期をスキップします。")
        if not ranking_poster.is_running():
            ranking_poster.start()
        return

    try:
        print("[INFO] ギルドコマンドをクリア＆同期します...")
        # ✅ clear_commands は await 不要
        bot.tree.clear_commands(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("[INFO] ギルドコマンド同期完了")
    except Exception:
        print("[ERROR] コマンド同期中にエラー発生:")
        traceback.print_exc()

    load_state()
    if not ranking_poster.is_running():
        ranking_poster.start()

    print(f"✅ {bot.user} が起動しました。")

# ============================================================

bot.run(TOKEN)
