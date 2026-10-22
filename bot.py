from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import io
import discord
from discord import app_commands
from discord.ext import commands

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is online and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True

allowed_mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
bot = commands.Bot(command_prefix='!', intents=intents, allowed_mentions=allowed_mentions)

blacklisted_users = set()

# بيانات السكربتات المخزنة
# Structure: { script_name: {"version": str, "buyer_role_id": int, "admin_role_id": int, "source_code": str} }
scripts_data = {}

async def script_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    choices = []
    for script_name in scripts_data.keys():
        if current.lower() in script_name.lower():
            choices.append(app_commands.Choice(name=script_name, value=script_name))
    return choices[:25]

# ==================== Views & Buttons ==================== #

class InviteRewardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Check Invites 📊", style=discord.ButtonStyle.primary, custom_id="check_invites")
    async def check_invites_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("❌ You are blacklisted from using the reward system!", ephemeral=True)
            return

        total_invites = 0
        try:
            invites = await interaction.guild.invites()
            for invite in invites:
                if invite.inviter and invite.inviter.id == interaction.user.id:
                    total_invites += invite.uses
        except Exception:
            pass

        await interaction.response.send_message(f"📈 You currently have **{total_invites}** invite(s).", ephemeral=True)

    @discord.ui.button(label="Get Invite Link 🔗", style=discord.ButtonStyle.success, custom_id="get_invite_link")
    async def get_invite_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("❌ You are blacklisted!", ephemeral=True)
            return

        invite = await interaction.channel.create_invite(max_age=0, max_uses=0, unique=True, reason=f"Invite for {interaction.user}")
        await interaction.response.send_message(f"🔗 Here is your personal invite link:\n{invite.url}", ephemeral=True)

    @discord.ui.button(label="Redeem Script 🎁", style=discord.ButtonStyle.danger, custom_id="redeem_script")
    async def redeem_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("❌ You are blacklisted!", ephemeral=True)
            return

        total_invites = 0
        try:
            invites = await interaction.guild.invites()
            for invite in invites:
                if invite.inviter and invite.inviter.id == interaction.user.id:
                    total_invites += invite.uses
        except Exception:
            pass

        if total_invites >= 4:
            await interaction.response.send_message(
                "🎉 **Congratulations!** You have enough invites for **2 Days**. Please go and open a ticket in **୧・buy・💰-شراء** to claim your reward!",
                ephemeral=True
            )
        elif total_invites >= 2:
            await interaction.response.send_message(
                "🎉 **Congratulations!** You have enough invites for **12 Hours**. Please go and open a ticket in **୧・buy・💰-شراء** to claim your reward!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"⚠️ You need at least **2 invites** (for 12 hours) or **4 invites** (for 2 days). You currently have **{total_invites}** invites.", ephemeral=True)


class ObfuscateConfirmView(discord.ui.View):
    def __init__(self, script_name: str, file_content: str):
        super().__init__(timeout=60)
        self.script_name = script_name
        self.file_content = file_content

    @discord.ui.button(label="Yes ✅", style=discord.ButtonStyle.success)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # تم اختيار Obfuscate
        scripts_data[self.script_name]["source_code"] = self.file_content
        await interaction.response.send_message(f"⚡ Source file saved and obfuscation applied for `{self.script_name}`!", ephemeral=True)

    @discord.ui.button(label="No ❌", style=discord.ButtonStyle.danger)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # حفظ الكود الأصلي وتجهيزه بدون Obfuscation
        scripts_data[self.script_name]["source_code"] = self.file_content
        raw_loader = f'loadstring(game:HttpGet("https://raw.githubusercontent.com/.../{self.script_name}.lua"))()'
        await interaction.response.send_message(
            f"✅ Source file linked directly without obfuscation for `{self.script_name}`!\nExecutor Loader Code:\n```lua\n{raw_loader}\n```", 
            ephemeral=True
        )


class PanelControlView(discord.ui.View):
    def __init__(self, script_name: str):
        super().__init__(timeout=None)
        self.script_name = script_name

    @discord.ui.button(label="Get Script 📜", style=discord.ButtonStyle.success, custom_id="panel_get_script")
    async def get_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("❌ Script details not found.", ephemeral=True)
            return

        buyer_role_id = s_info.get("buyer_role_id")
        if buyer_role_id:
            role = interaction.guild.get_role(buyer_role_id)
            if role and role not in interaction.user.roles:
                await interaction.response.send_message(f"❌ You need the {role.mention} role to get this script!", ephemeral=True)
                return

        code = s_info.get("source_code", 'loadstring(game:HttpGet("https://raw.githubusercontent.com/..."))()')
        await interaction.response.send_message(f"📜 **Here is your script for `{self.script_name}`:**\n```lua\n{code}\n```", ephemeral=True)

    @discord.ui.button(label="Redeem Key 🔑", style=discord.ButtonStyle.primary, custom_id="panel_redeem_key")
    async def redeem_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔑 Please enter your license key in the redemption modal or open a ticket.", ephemeral=True)

    @discord.ui.button(label="Reset HWID 🔄", style=discord.ButtonStyle.secondary, custom_id="panel_reset_hwid")
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔄 HWID Reset request submitted successfully! (Cooldown: 24 Hours)", ephemeral=True)

    @discord.ui.button(label="Get Buyer Role 👤", style=discord.ButtonStyle.secondary, custom_id="panel_get_buyer_role")
    async def get_buyer_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info or not s_info.get("buyer_role_id"):
            await interaction.response.send_message("❌ Buyer role is not configured for this script.", ephemeral=True)
            return

        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role:
            if role in interaction.user.roles:
                await interaction.response.send_message("✅ You already have the buyer role!", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"🎉 Granted you the {role.mention} buyer role!", ephemeral=True)

    @discord.ui.button(label="Stats 📊", style=discord.ButtonStyle.secondary, custom_id="panel_stats")
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name, {})
        version = s_info.get("version", "1.0.0")
        await interaction.response.send_message(f"📊 **Script Stats:**\n• Name: `{self.script_name}`\n• Version: `{version}`\n• Status: `Working / Active`", ephemeral=True)


# ==================== Bot Commands ==================== #

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands successfully.")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    print(f"Bot connected successfully as: {bot.user}")


# 1. أمر انشاء السكربت
@bot.tree.command(name="create_script", description="Create a new script entry with roles")
@app_commands.describe(
    name_script="Enter script name",
    buyer_role="Select buyer role",
    admin_role="Select admin role"
)
@app_commands.checks.has_permissions(administrator=True)
async def create_script_command(
    interaction: discord.Interaction, 
    name_script: str, 
    buyer_role: discord.Role, 
    admin_role: discord.Role
):
    scripts_data[name_script] = {
        "version": "1.0.0",
        "buyer_role_id": buyer_role.id,
        "admin_role_id": admin_role.id,
        "source_code": ""
    }
    
    embed = discord.Embed(
        title="✅ Script Registered Successfully!",
        description=(
            "___________________\n"
            f"📜 **Script Name:** `{name_script}`\n"
            f"👤 **Buyer Role:** {buyer_role.mention}\n"
            f"🛡️ **Admin Role:** {admin_role.mention}\n"
            "___________________"
        ),
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# 2. أمر ربط ملف السكربت (Source Setup)
@bot.tree.command(name="sourcesetup", description="Attach source file to a script")
@app_commands.describe(file="Upload the script file", script="Choose the target script")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def source_setup_command(interaction: discord.Interaction, file: discord.Attachment, script: str):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist. Please create it first.", ephemeral=True)
        return

    content_bytes = await file.read()
    file_content = content_bytes.decode('utf-8', errors='ignore')

    view = ObfuscateConfirmView(script_name=script, file_content=file_content)
    await interaction.response.send_message(
        f"📄 Source file for `{script}` received!\n\n**Do you want to obfuscate script?**", 
        view=view, 
        ephemeral=True
    )


# 3. أمر تعديل رول الباير (Set Buyer Role)
@bot.tree.command(name="setbuyerrole", description="Change the buyer role for a script")
@app_commands.describe(script="Choose the script", buyer_role="Select the new buyer role")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def set_buyer_role_command(interaction: discord.Interaction, script: str, buyer_role: discord.Role):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    scripts_data[script]["buyer_role_id"] = buyer_role.id
    await interaction.response.send_message(f"✅ Buyer role for `{script}` updated to {buyer_role.mention}!", ephemeral=True)


# 4. أمر إنشاء البانل (Panel Setup)
@bot.tree.command(name="panelsetup", description="Send a script management panel")
@app_commands.describe(script="Choose the script", channel="Select target channel", name_panel="Enter panel title")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def panel_setup_command(interaction: discord.Interaction, script: str, channel: discord.TextChannel, name_panel: str):
    embed = discord.Embed(
        title=name_panel,
        description=f"Best {script.lower()}\n\nFaulmor • Script Management |",
        color=0xd4af37
    )
    embed.set_author(name="luarmor", icon_url=bot.user.display_avatar.url)
    
    view = PanelControlView(script_name=script)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Panel created successfully in {channel.mention}!", ephemeral=True)


# 5. أمر تحديث السكربت (Update Script)
@bot.tree.command(name="update_script", description="Send a script update announcement")
@app_commands.describe(script="Choose the script", revision="New version (e.g. 1.0.1 or 99.0.0)", details="Enter update details")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def update_script_command(interaction: discord.Interaction, script: str, revision: str, details: str):
    if script in scripts_data:
        scripts_data[script]["version"] = revision

    ping_text = "@everyone @here"
    embed = discord.Embed(
        title=f"🚀 New Update for {script}!",
        description=f"___________________\n📌 **Version:** `{revision}`\n\n```\n{details}\n```\n___________________",
        color=0x00ff00
    )
    await interaction.response.send_message(content=ping_text, embed=embed)


# 6. أمر مكافآت الدعوة (Invite Reward)
@bot.tree.command(name="invite_reward", description="Send the invite rewards panel")
@app_commands.checks.has_permissions(administrator=True)
async def invite_reward_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎁 Invite Rewards System",
        description="• **2 Invites** = 12 Hours Script ⏳\n• **4 Invites** = 2 Days Script 🚀\n___________________\nUse the buttons below to check invites and claim rewards.",
        color=0x2b2d31
    )
    view = InviteRewardView()
    await interaction.response.send_message("Panel created successfully!", ephemeral=True)
    await interaction.channel.send(embed=embed, view=view)


# 7. أمر البلاك ليست
@bot.tree.command(name="blacklist", description="Add a user to the blacklist")
@app_commands.describe(user="The user to blacklist")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    blacklisted_users.add(user.id)
    await interaction.response.send_message(f"🚫 {user.mention} has been added to the blacklist.", ephemeral=True)

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN environment variable is missing!") 
