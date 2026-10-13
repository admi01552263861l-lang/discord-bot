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

# أزرار الهدايا ونظام الدعوات المحدث (2 دعوات = 12 ساعة، 4 دعوات = 2 أيام)
class GiftView(discord.ui.View):
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
            script_code = 'loadstring(game:HttpGet("https://raw.githubusercontent.com/..."))()'
            await interaction.response.send_message(
                f"🎉 **Congratulations!** You unlocked the **2 Days** Script (Cost: 4 invites):\n```lua\n{script_code}\n```",
                ephemeral=True
            )
        elif total_invites >= 2:
            script_code = 'loadstring(game:HttpGet("https://raw.githubusercontent.com/..."))()'
            await interaction.response.send_message(
                f"🎉 **Congratulations!** You unlocked the **12 Hours** Script (Cost: 2 invites):\n```lua\n{script_code}\n```",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"⚠️ You need at least **2 invites** (for 12 hours) or **4 invites** (for 2 days). You currently have **{total_invites}** invites.", ephemeral=True)

# أزرار أمر /steals (نعم / لا) وإرسال الملف المطلوب
class StealConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes ✅", style=discord.ButtonStyle.success)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # محتوى الملف الذي طلبته مع توجيه وضع الـ Webhook
        lua_script_content = """-- ADD WEBHOOK: standalone Secret Brainrot steal notifier.
local WEBHOOK_URL = "PUT_YOUR_WEBHOOK_HERE"
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local HttpService = game:GetService("HttpService")
local player = Players.LocalPlayer
if not player then return end

local function getRequestFunction()
    if type(request) == "function" then return request end
    if type(http_request) == "function" then return http_request end
    if type(syn) == "table" and type(syn.request) == "function" then return syn.request end
    return nil
end

print("Secret notifier loaded. Put your webhook in the file!")
"""
        file = discord.File(io.BytesIO(lua_script_content.encode('utf-8')), filename="add_webhook.lua")
        await interaction.response.send_message(
            "📁 **Here is your file!** Put your webhook in the file (`PUT_YOUR_WEBHOOK_HERE`) and run it.", 
            file=file, 
            ephemeral=True
        )

    @discord.ui.button(label="No ❌", style=discord.ButtonStyle.danger)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Cancelled.", ephemeral=True)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands successfully.")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    print(f"Bot connected successfully as: {bot.user}")

@bot.tree.command(name="gift", description="Send the invite rewards panel")
@app_commands.checks.has_permissions(administrator=True)
async def gift_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎁 Invite Rewards System",
        description="• **2 Invites** = 12 Hours Script ⏳\n• **4 Invites** = 2 Days Script 🚀\n\nUse the buttons below to manage your invites and claim rewards.",
        color=0x2b2d31
    )
    view = GiftView()
    await interaction.response.send_message("Panel created successfully!", ephemeral=True)
    await interaction.channel.send(embed=embed, view=view)

@bot.tree.command(name="steals", description="Share your steal report")
async def steals_command(interaction: discord.Interaction):
    view = StealConfirmView()
    await interaction.response.send_message("Hey, do you wanna share what you steal today?", view=view, ephemeral=True)

@bot.tree.command(name="update", description="Send an update announcement")
@app_commands.describe(details="Enter update details")
async def update(interaction: discord.Interaction, details: str):
    ping_text = "@everyone @here"
    embed = discord.Embed(
        title="🚀 New Update Available!",
        description=f"```\n{details}\n```",
        color=0x00ff00
    )
    await interaction.response.send_message(content=ping_text, embed=embed)

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
