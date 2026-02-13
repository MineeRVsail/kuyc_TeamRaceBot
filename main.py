import discord
from discord.ext import commands
import os
from supabase import create_client, Client
from flask import Flask
from threading import Thread

# --- 設定（クラウドの環境変数から読み込む） ---
# ここには何も書き込まないでください！
TOKEN = os.environ.get('DISCORD_TOKEN')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

# --- 24時間稼働用サーバー（目覚まし時計） ---
app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- ここから下はいつものボットのコード ---

# Supabaseに接続
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG_FILE = "config.json" # 注: クラウド上では再起動で消えますが、リーダーボードの場所はDBに入れるなど工夫も可能です

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
waiting_players = []

# --- データ管理関数 ---
def load_json(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            import json
            return json.load(f)
    return {}

def save_json(filename, data):
    import json
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

def get_player_data(user_id, user_name="Unknown"):
    user_id = str(user_id)
    response = supabase.table("players").select("*").eq("id", user_id).execute()
    data = response.data
    if not data:
        new_user = {"id": user_id, "name": user_name, "rate": 0, "wins": 0, "losses": 0}
        supabase.table("players").insert(new_user).execute()
        return new_user
    if data[0]['name'] != user_name and user_name != "Unknown":
        supabase.table("players").update({"name": user_name}).eq("id", user_id).execute()
        data[0]['name'] = user_name
    return data[0]

def update_player_rate_db(user_id, is_win):
    user_id = str(user_id)
    current_data = get_player_data(user_id)
    current_rate = current_data["rate"]
    
    delta = 0
    if current_rate < 100: delta = 30 if is_win else -15
    elif current_rate < 300: delta = 30 if is_win else -24
    elif current_rate < 500: delta = 24 if is_win else -24
    elif current_rate < 800: delta = 24 if is_win else -30
    elif current_rate < 1000: delta = 20 if is_win else -30
    else: delta = 15 if is_win else -30

    new_rate = max(0, current_rate + delta)
    update_data = {
        "rate": new_rate,
        "wins": current_data["wins"] + (1 if is_win else 0),
        "losses": current_data["losses"] + (1 if not is_win else 0)
    }
    supabase.table("players").update(update_data).eq("id", user_id).execute()
    return current_rate, new_rate, delta

def get_rank_info(rate):
    if rate < 100: return "Iron", 0x434343, "⚫"
    elif rate < 300: return "Bronze", 0xcd7f32, "🟤"
    elif rate < 500: return "Silver", 0xc0c0c0, "⚪"
    elif rate < 800: return "Gold", 0xffd700, "🟡"
    elif rate < 1000: return "Diamond", 0x00bfff, "💎"
    else: return "Master", 0x800080, "👑"

async def update_leaderboard_display():
    # 注: クラウド上ではconfig.jsonが消えることがあるため、チャンネルIDを直書きするかDBで管理するのが理想です
    # 今回は簡易的にファイルのまま進めます
    config = load_json(CONFIG_FILE)
    if "leaderboard_channel" not in config or "leaderboard_message" not in config: return
    channel = bot.get_channel(config["leaderboard_channel"])
    if not channel: return
    try: msg = await channel.fetch_message(config["leaderboard_message"])
    except: return

    response = supabase.table("players").select("*").order("rate", desc=True).limit(20).execute()
    players = response.data
    text_lines = []
    top_emoji = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(players):
        rank_name, _, rank_icon = get_rank_info(p['rate'])
        position = top_emoji[i] if i < 3 else f"{i+1}."
        text_lines.append(f"{position} {rank_icon} **{p['name']}** : {p['rate']} ({rank_name})")
    if not text_lines: text_lines = ["まだデータがありません"]
    embed_color = 0xFFD700
    if players: _, embed_color, _ = get_rank_info(players[0]['rate'])
    embed = discord.Embed(title="🏆 サーバーランキング (Global)", description="\n".join(text_lines), color=embed_color)
    embed.set_footer(text="Powered by Supabase DB")
    await msg.edit(content=None, embed=embed)

def make_balanced_teams(player_ids):
    players = []
    for uid in player_ids:
        user = bot.get_user(uid)
        name = user.display_name if user else "Unknown"
        data = get_player_data(uid, name)
        players.append({"id": uid, "name": name, "rate": data["rate"]})
    players.sort(key=lambda x: x["rate"], reverse=True)
    team_a, team_b = [], []
    sum_a, sum_b = 0, 0
    for p in players:
        if sum_a < sum_b: team_a.append(p); sum_a += p["rate"]
        elif sum_b < sum_a: team_b.append(p); sum_b += p["rate"]
        else:
            if len(team_a) <= len(team_b): team_a.append(p); sum_a += p["rate"]
            else: team_b.append(p); sum_b += p["rate"]
    return team_a, team_b, sum_a, sum_b

class MatchResultView(discord.ui.View):
    def __init__(self, team_a, team_b):
        super().__init__(timeout=None)
        self.team_a = team_a
        self.team_b = team_b
    async def process_result(self, interaction, winner_team_name):
        self.stop()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(view=self)
        is_a_win = (winner_team_name == "A")
        result_msg = [f"🏆 **Team {winner_team_name} の勝利！**\n"]
        for p in self.team_a:
            old, new, diff = update_player_rate_db(p["id"], is_win=is_a_win)
            sign = "+" if diff >= 0 else ""
            result_msg.append(f"🅰️ {p['name']}: {old} → **{new}** ({sign}{diff})")
        for p in self.team_b:
            old, new, diff = update_player_rate_db(p["id"], is_win=not is_a_win)
            sign = "+" if diff >= 0 else ""
            result_msg.append(f"🅱️ {p['name']}: {old} → **{new}** ({sign}{diff})")
        await update_leaderboard_display()
        embed = discord.Embed(title="試合結果確定", description="\n".join(result_msg), color=0xffd700)
        
        # 1. 飛ばしたいチャンネルのID（さっきコピーした数字）
        RESULT_CHANNEL_ID = 1471854264543609018 
        
        # 2. チャンネルを取得する
        target_channel = interaction.client.get_channel(RESULT_CHANNEL_ID)
        
        if target_channel:
            # 指定したチャンネルにEmbedを送る
            # (viewがないなら view=... は書かなくてOKです)
            await target_channel.send(embed=embed)
            
            # 元のチャンネル（募集した場所）には「あっちに出したよ」とだけ伝える
            await interaction.response.send_message(f"試合が始まります！結果入力は {target_channel.mention} で行ってください。", ephemeral=True)
        else:
            # エラー防止：もしIDが間違ってたら元の場所に送る
            await interaction.channel.send(embed=embed)

        # ▲ここまで▲
    @discord.ui.button(label="Team A Win", style=discord.ButtonStyle.primary, emoji="🅰️")
    async def win_a(self, interaction, button): await self.process_result(interaction, "A")
    @discord.ui.button(label="Team B Win", style=discord.ButtonStyle.danger, emoji="🅱️")
    async def win_b(self, interaction, button): await self.process_result(interaction, "B")
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Canceled", embed=None, view=None)

class QueueView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    def update_embed(self):
        embed = discord.Embed(title="🎮 チーム対戦 募集中", description="参加ボタンを押してください", color=0x00ff00)
        if len(waiting_players) == 0: player_list = "現在 0人"
        else:
            names = []
            for uid in waiting_players:
                user = bot.get_user(uid)
                name = user.display_name if user else "Unknown"
                p_data = get_player_data(uid, name)
                _, _, r_icon = get_rank_info(p_data['rate'])
                names.append(f"• {r_icon} {name} (R:{p_data['rate']})")
            player_list = "\n".join(names)
        embed.add_field(name=f"参加者 ({len(waiting_players)}人)", value=player_list, inline=False)
        return embed
    @discord.ui.button(label="Join", style=discord.ButtonStyle.green, emoji="⚔️")
    async def join(self, interaction, button):
        if interaction.user.id not in waiting_players:
            waiting_players.append(interaction.user.id)
            get_player_data(interaction.user.id, interaction.user.display_name)
            await interaction.response.edit_message(embed=self.update_embed(), view=self)
        else: await interaction.response.send_message("参加済みです", ephemeral=True)
    @discord.ui.button(label="Leave", style=discord.ButtonStyle.red, emoji="👋")
    async def leave(self, interaction, button):
        if interaction.user.id in waiting_players:
            waiting_players.remove(interaction.user.id)
            await interaction.response.edit_message(embed=self.update_embed(), view=self)
        else: await interaction.response.send_message("参加していません", ephemeral=True)
    @discord.ui.button(label="Start", style=discord.ButtonStyle.blurple, emoji="🚀")
    async def start(self, interaction, button):
        if len(waiting_players) < 2:
            await interaction.response.send_message("人数不足です", ephemeral=True)
            return
        team_a, team_b, sum_a, sum_b = make_balanced_teams(waiting_players)
        waiting_players.clear()
        await interaction.response.edit_message(embed=self.update_embed(), view=self)
        embed = discord.Embed(title="⚔️ 試合開始！", color=0xff9900)
        names_a = "\n".join([f"• {p['name']} ({p['rate']})" for p in team_a])
        names_b = "\n".join([f"• {p['name']} ({p['rate']})" for p in team_b])
        embed.add_field(name=f"🅰️ Team A (Avg: {sum_a/len(team_a):.1f})", value=names_a, inline=True)
        embed.add_field(name=f"🅱️ Team B (Avg: {sum_b/len(team_b):.1f})", value=names_b, inline=True)
        await interaction.channel.send(embed=embed, view=MatchResultView(team_a, team_b))

@bot.command()
async def recruit(ctx):
    waiting_players.clear()
    view = QueueView()
    await ctx.send(embed=view.update_embed(), view=view)

@bot.command()
async def status(ctx):
    data = get_player_data(ctx.author.id, ctx.author.display_name)
    rank_name, rank_color, rank_icon = get_rank_info(data['rate'])
    embed = discord.Embed(title=f"{rank_icon} {ctx.author.display_name}", description=f"**Rank: {rank_name}**", color=rank_color)
    embed.add_field(name="Rate", value=str(data['rate'])); embed.add_field(name="W/L", value=f"{data['wins']}/{data['losses']}")
    await ctx.send(embed=embed)

@bot.command()
async def init_leaderboard(ctx):
    embed = discord.Embed(title="🏆 ランキング", description="...", color=0xFFD700)
    msg = await ctx.send(embed=embed)
    import json
    with open(CONFIG_FILE, "w") as f: json.dump({"leaderboard_channel": ctx.channel.id, "leaderboard_message": msg.id}, f)
    await update_leaderboard_display()
    await ctx.message.delete()

@bot.event
async def on_ready():
    print(f'{bot.user} is Ready on Cloud!')

# ★ここが重要：目覚まし時計を起動してからボットを起動する
keep_alive()
bot.run(TOKEN)
