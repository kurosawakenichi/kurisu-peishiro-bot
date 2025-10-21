import discord
from discord import app_commands
from discord.ext import commands
import asyncio

TOKEN = "YOUR_BOT_TOKEN"
ADMIN_ID = "YOUR_ADMIN_ID"  # 管理者ID

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ユーザーデータ
user_data = {}  # {user_id: pt}

# ランク設定
RANKS = [
    (0, 2, "Beginner", "🔰"),
    (3, 4, "SilverChallenge1", "🔰🔥"),
    (5, 7, "Silver", "🥈"),
    (8, 9, "GoldChallenge1", "🥈🔥"),
    (10, 12, "Gold", "🥇"),
    (13, 14, "MasterChallenge1", "🥇🔥"),
    (15, 17, "Master", "⚔️"),
    (18, 19, "GrandMasterChallenge1", "⚔️🔥"),
    (20, 22, "GrandMaster", "🪽"),
    (23, 24, "ChallengerChallenge1", "🪽🔥"),
    (25, 999, "Challenger", "😈")
]

def get_rank_by_pt(pt: int):
    for min_pt, max_pt, name, icon in RANKS:
        if min_pt <= pt <= max_pt:
            return name, icon
    return "Unknown", ""

async def update_member_role_and_name(member: discord.Member):
    pt = user_data.get(member.id, 0)
    rank_name, icon = get_rank_by_pt(pt)
    # ユーザー名更新
    try:
        base_name = member.name.split(" ")[0]
        new_name = f"{base_name} {icon} {pt}pt"
        await member.edit(nick=new_name)
    except Exception as e:
        print(f"Error updating name for {member}: {e}")
    # ロール更新
    await reset_user_role(member)

async def reset_user_role(member: discord.Member):
    for _, _, role_name, _ in RANKS:
        role = discord.utils.get(member.guild.roles, name=role_name)
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except Exception as e:
                print(f"Error removing role {role_name} from {member}: {e}")
    pt = user_data.get(member.id, 0)
    rank_name, _ = get_rank_by_pt(pt)
    role = discord.utils.get(member.guild.roles, name=rank_name)
    if role:
        try:
            await member.add_roles(role)
        except Exception as e:
            print(f"Error adding role {rank_name} to {member}: {e}")

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is ready and command tree synced.")

# 管理者用: 全ユーザー初期化
@bot.tree.command(name="admin_reset_all", description="全ユーザーのPT・ロール・名前を初期化")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if str(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)
        return

    if not interaction.response.is_done():
        await interaction.response.send_message("全ユーザーの初期化を開始します...", ephemeral=True)

    for member in interaction.guild.members:
        if member.bot:
            continue
        user_data[member.id] = 0
        await reset_user_role(member)
        await update_member_role_and_name(member)

    await interaction.followup.send("全ユーザーのPTと表示を初期化しました。")

# 管理者用: PT設定
@bot.tree.command(name="admin_set_pt", description="特定ユーザーのPTを設定")
@app_commands.describe(user="対象ユーザー", pt="設定するPT")
async def cmd_admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if str(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)
        return

    user_data[user.id] = pt
    await update_member_role_and_name(user)
    if not interaction.response.is_done():
        await interaction.response.send_message(f"{user.name} のPTを {pt} に設定しました。", ephemeral=True)
    else:
        await interaction.followup.send(f"{user.name} のPTを {pt} に設定しました。")

# 管理者用: ランキング表示
@bot.tree.command(name="admin_show_ranking", description="管理者用ランキング表示")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if str(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message("管理者専用コマンドです。", ephemeral=True)
        return

    ranking_list = []
    for member_id, pt in sorted(user_data.items(), key=lambda x: -x[1]):
        member = interaction.guild.get_member(member_id)
        if member:
            ranking_list.append(f"{member.name}")  # 純ユーザー名のみ

    if ranking_list:
        await interaction.response.send_message("\n".join(ranking_list), ephemeral=True)
    else:
        await interaction.response.send_message("ランキングデータが存在しません。", ephemeral=True)

# マッチ申請
@bot.tree.command(name="match_request", description="マッチ申請")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    await interaction.response.send_message(f"{interaction.user.mention} が {opponent.mention} にマッチ申請しました。", ephemeral=False)
    view = ApproveMatchView(origin_channel_id=interaction.channel.id, requester=interaction.user)
    try:
        await opponent.send("あなたにマッチ申請が届きました。承認してください。", view=view)
    except Exception as e:
        await interaction.followup.send(f"相手にDMを送信できません: {e}", ephemeral=True)

# 承認ボタンビュー
class ApproveMatchView(discord.ui.View):
    def __init__(self, origin_channel_id, requester):
        super().__init__(timeout=None)
        self.origin_channel_id = origin_channel_id
        self.requester = requester

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("これはあなたの試合ではありません", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{self.requester.mention} と {interaction.user.mention} のマッチングが成立しました。試合後、勝者が結果報告をしてください。"
        )

bot.run(TOKEN)
