from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# خادم وهمي لإبقاء Render مصدقاً أن التطبيق يعمل كـ Web Service
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()
# تشغيل الخادم الوهمي في الخلفية
threading.Thread(target=run_server, daemon=True).start()
import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True  # مهم جداً لمتابعة تغييرات الدعوات

bot = commands.Bot(command_prefix='!', intents=intents)

# قواميس مؤقتة لحفظ بيانات الدعوات والبلاك ليست
invites_tracker = {}
blacklisted_users = set()

# ----------------- واجهة الأزرار (Buttons View) -----------------
class GiftView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # 1. زر فحص الدعوات Check Invites
    @discord.ui.button(label="Check Invites 📊", style=discord.ButtonStyle.primary, custom_id="check_invites")
    async def check_invites_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("❌ You are blacklisted from using the reward system!", ephemeral=True)
            return

        total_invites = 0
        for invite in await interaction.guild.invites():
            if invite.inviter and invite.inviter.id == interaction.user.id:
                total_invites += invite.uses

        await interaction.response.send_message(f"📈 You currently have **{total_invites}** invite(s).", ephemeral=True)

    # 2. زر جلب رابط الدعوة Get Invite Link
    @discord.ui.button(label="Get Invite Link 🔗", style=discord.ButtonStyle.success, custom_id="get_invite_link")
    async def get_invite_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("❌ You are blacklisted!", ephemeral=True)
            return

        channel = interaction.channel
        # إنشاء رابط دعوة مخصص للعميل
        invite = await channel.create_invite(max_age=0, max_uses=0, unique=True, reason=f"Invite for {interaction.user}")
        await interaction.response.send_message(f"🔗 Here is your personal invite link:\n{invite.url}", ephemeral=True)

    # 3. زر استلام السكربت Redeem Script
    @discord.ui.button(label="Redeem Script 🎁", style=discord.ButtonStyle.danger, custom_id="redeem_script")
    async def redeem_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("❌ You are blacklisted!", ephemeral=True)
            return

        total_invites = 0
        for invite in await interaction.guild.invites():
            if invite.inviter and invite.inviter.id == interaction.user.id:
                total_invites += invite.uses

        # التأكد من وصوله لـ 2 دعوات على الأقل
        if total_invites >= 2:
            # هنا يوضع السكربت المطلوبة إرساله للمستخدم
            script_code = 'loadstring(game:HttpGet("https://raw.githubusercontent.com/..."))()'
            await interaction.response.send_message(
                f"🎉 **Congratulations!** Here is your 1-Hour Script:\n```lua\n{script_code}\n```\n*Note: This key/script is valid for 1 hour.*",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"⚠️ You need at least **2 invites** to redeem the script. You currently have **{total_invites}**.", ephemeral=True)


# ----------------- الأحداث والأوامر -----------------

@bot.event
async def on_ready():
    # تحديث قائمة الدعوات عند تشغيل البوت
    for guild in bot.guilds:
        invites_tracker[guild.id] = await guild.invites()
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    print(f"Bot connected as: {bot.user}")


# أمر إنشاء لوحة الأزرار في القناة /gift
@bot.tree.command(name="gift", description="Send the invite rewards panel")
@app_commands.checks.has_permissions(administrator=True)
async def gift_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎁 Invite Rewards System",
        description="Invite **2 members** to unlock the exclusive script for 1 hour!\n\nUse the buttons below to manage your invites and claim rewards.",
        color=0x2b2d31
    )
    view = GiftView()
    await interaction.response.send_message("Panel created!", ephemeral=True)
    await interaction.channel.send(embed=embed, view=view)


# أمر إضافة عضو إلى قائمة الحظر (Blacklist)
@bot.tree.command(name="blacklist", description="Add a user to the blacklist")
@app_commands.describe(user="The user to blacklist")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    blacklisted_users.add(user.id)
    await interaction.response.send_message(f"🚫 {user.mention} has been added to the blacklist.", ephemeral=True)


# متابعة مغادرة الأعضاء لإضافة الداعي للبلاك ليست تلقائياً
@bot.event
async def on_member_remove(member):
    # في حال مغادرة أي شخص يمكنك معالجة إلغاء الدعوة أو إضافة الشخص للبلاك ليست
    pass


# bot.run("MTU1OTI3NTU4MDM1OTMxNTUzNw.G3VwVn.u9cboSRl4F0WoiAyPN6aGHukl5W7Ezz05v5fZI")
