# ================================
# Discord Slash Command FULL DELETE
# ================================
import os
import aiohttp
import asyncio

TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # Railway / Render 等の環境変数そのまま利用
GUILD_ID = 1427541907009044502  # ユーザーGuild

API_BASE = "https://discord.com/api/v10"


async def delete_all_guild_commands():
    headers = {
        "Authorization": f"Bot {TOKEN}"
    }

    # BotアプリIDを取得
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}/users/@me", headers=headers) as r:
            data = await r.json()
            app_id = data["id"]

        # ギルドコマンド一覧取得
        async with session.get(
            f"{API_BASE}/applications/{app_id}/guilds/{GUILD_ID}/commands",
            headers=headers
        ) as r:
            cmds = await r.json()

        print(f"【削除対象コマンド数】{len(cmds)} 件")

        # 削除処理
        for cmd in cmds:
            cmd_id = cmd["id"]
            async with session.delete(
                f"{API_BASE}/applications/{app_id}/guilds/{GUILD_ID}/commands/{cmd_id}",
                headers=headers
            ):
                print(f" → 削除: {cmd['name']}")

        print("✅ ギルド内コマンド 完全削除完了")


async def main():
    await delete_all_guild_commands()

asyncio.run(main())
