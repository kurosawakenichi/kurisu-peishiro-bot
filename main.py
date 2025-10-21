import discord
from discord.ext import commands, tasks
from discord import app_commands
import os

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# 内部データ
user_data = {}  # {user_id: {"pt": int, "role": str, "rank": int}}
active_matches = {}  # {(winner_id, loser_id): {"channel_id": int, "message_id": int}}

# ランクとアイコン
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
    (25, float("inf"), "Challenger", "😈")
]

# rank差計算用内部rank
def get_internal_rank(pt):
    if pt <= 4:
        return 1
    elif pt <= 9:
        return 2
    elif pt <= 14:
        return 3
    elif pt <= 19:
        return 4
    elif pt <= 24:
        return 5
    else:
        return 6

def get_rank_role(pt):
    for min_pt, max_pt, role_name, icon in RANKS:
        if min_pt <= pt <= max_pt:
            return role_name, icon
    return "Challenger", "😈"

def adjust_pt_after_loss(pt):
    if pt in [3, 4]:
        return 2
    if pt in [8, 9]:
        return 7
    if pt in [13, 14]:
        return 12
    if pt in [18, 19]:
        return 17
    return pt

# --- ユーザー名とロール同期 ---
async def update_user_display(member: discord.Member):
    data = user_data.get(member.id)
    if not data:
        return
    role_name, icon = get_rank_role(data["pt"])
    # ロール付与
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role:
        for r in member.roles:
            if r.name in [rname for _, _, rname, _ in RANKS]:
                await member.remove_roles(r)
        await member.add_roles(role)
    # ユーザー名更新
    new_name = f"{member.name} {icon} {data['pt']}pt"
    if member.display_name != new_name:
        try:
            await member.edit(nick=new_name)
        except discord.Forbidden:
            pass

# --- 管理者コマンド ---
@bot.tree.command(name="admin_reset_all")
async def cmd_admin_reset_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    for guild in bot.guilds:
        for member in guild.members:
            user_data[member.id] = {"pt": 0, "role": "Beginner"}
            await update_user_display(member)
    await interaction.response.send_message("全ユーザーのPTと表示を初期化しました。", ephemeral=True)

@bot.tree.command(name="admin_set_pt")
@app_commands.describe(user="ユーザー", pt="PT値")
async def cmd_admin_set_pt(interaction: discord.Interaction, user: discord.Member, pt: int):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    user_data[user.id] = {"pt": pt, "role": get_rank_role(pt)[0]}
    await update_user_display(user)
    await interaction.response.send_message(f"{user.name} のPTを {pt} に設定しました。", ephemeral=True)

@bot.tree.command(name="admin_show_ranking")
async def cmd_admin_show_ranking(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者専用です。", ephemeral=True)
        return
    ranking_list = []
    for uid, data in sorted(user_data.items(), key=lambda x: -x[1]["pt"]):
        member = interaction.guild.get_member(uid)
        if member:
            ranking_list.append(f"{member.name} {data['pt']}pt")
    if not ranking_list:
        ranking_list.append("ランキングはまだありません。")
    await interaction.response.send_message("\n".join(ranking_list), ephemeral=True)

# --- マッチ申請・承認 ---
class ApproveMatchView(discord.ui.View):
    def __init__(self, opponent_id, origin_channel_id=None):
        super().__init__(timeout=None)
        self.opponent_id = opponent_id
        self.origin_channel_id = origin_channel_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("これはあなたの試合ではありません。", ephemeral=True)
            return
        # マッチ成立処理
        await interaction.response.send_message("マッチングが承認されました。試合後、勝者が結果報告をしてください。", ephemeral=False)

@bot.tree.command(name="match_request")
@app_commands.describe(opponent="対戦相手")
async def cmd_match_request(interaction: discord.Interaction, opponent: discord.Member):
    if interaction.user.id == opponent.id:
        await interaction.response.send_message("自分には申請できません。", ephemeral=True)
        return
    # 重複確認
    for (w, l) in active_matches.keys():
        if interaction.user.id in (w, l) or opponent.id in (w, l):
            await interaction.response.send_message(f"{opponent.name} とのマッチはすでに存在します。取り消しますか？", ephemeral=False)
            return
    view = ApproveMatchView(opponent.id)
    await interaction.response.send_message(
        f"{interaction.user.mention} が {opponent.mention} にマッチ申請を送りました。",
        view=view
    )

# --- イベント ---
@bot.event
async def on_ready():
    print(f"{bot.user} is ready")

bot.run(os.environ["DISCORD_TOKEN"])
