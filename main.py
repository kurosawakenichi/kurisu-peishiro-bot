import os
import asyncio
import aiohttp

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

async def delete_all_global_commands():
    url = "https://discord.com/api/v10/applications/@me"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

    async with aiohttp.ClientSession() as session:
        # Bot情報取得
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if "id" not in data:
                print("❌ Bot情報の取得に失敗しました。")
                print("返却データ:", data)
                return
            app_id = data["id"]

        # グローバルコマンド取得
        cmd_url = f"https://discord.com/api/v10/applications/{app_id}/commands"
        async with session.get(cmd_url, headers=headers) as resp:
            cmds = await resp.json()

        if isinstance(cmds, list):
            for cmd in cmds:
                cmd_id = cmd["id"]
                async with session.delete(f"{cmd_url}/{cmd_id}", headers=headers):
                    print(f"✅ Deleted global command: {cmd['name']} ({cmd_id})")
        else:
            print("⚠ 削除対象のグローバルコマンドはありません")

        print("🎉 完了（グローバルコマンド削除）")

async def main():
    await delete_all_global_commands()

if __name__ == "__main__":
    asyncio.run(main())
