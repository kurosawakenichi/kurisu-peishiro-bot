import os
import asyncio
import aiohttp

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

async def delete_all_guild_commands():
    url = f"https://discord.com/api/v10/applications/@me"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}"
    }

    async with aiohttp.ClientSession() as session:
        # bot(app)情報取得
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if "id" not in data:
                print("❌ Bot情報の取得に失敗しました。トークンが正しいか確認してください。")
                print("返却データ:", data)
                return
            app_id = data["id"]

        # ギルドコマンド削除
        delete_url = f"https://discord.com/api/v10/applications/{app_id}/guilds/{GUILD_ID}/commands"
        async with session.get(delete_url, headers=headers) as resp:
            cmds = await resp.json()

        if isinstance(cmds, list):
            for cmd in cmds:
                cmd_id = cmd["id"]
                async with session.delete(f"{delete_url}/{cmd_id}", headers=headers):
                    print(f"✅ Deleted guild command: {cmd['name']} ({cmd_id})")
        else:
            print("⚠ 削除対象のギルドコマンドはありません")

        print("🎉 完了")

async def main():
    await delete_all_guild_commands()

if __name__ == "__main__":
    asyncio.run(main())
