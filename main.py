@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました。")
    guild = discord.Object(id=GUILD_ID)
    # 既存のギルドコマンドを全削除してから同期
    await bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    print("ギルドにコマンド強制同期完了")
