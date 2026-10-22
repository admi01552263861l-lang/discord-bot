from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import json
import random
import string
import urllib.parse
import discord
from discord import app_commands
from discord.ext import commands

# اسم ملف التخزين المحلي
DATA_FILE = "data.json"

# دالة تحميل البيانات من الملف عند تشغيل البوت
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # إعادة تحويل القوائم إلى مجموعات (sets) بداخل الذاكرة
                for script_name, s_info in data.get("scripts", {}).items():
                    s_info["valid_keys"] = set(s_info.get("valid_keys", []))
                    s_info["whitelisted_users"] = set(s_info.get("whitelisted_users", []))
                    # تحويل المفاتيح النصية لـ user_keys لتناسب ID ديسكورد (int)
                    s_info["user_keys"] = {int(k): v for k, v in s_info.get("user_keys", {}).items()}
                return data.get("scripts", {}), set(data.get("blacklisted_users", []))
        except Exception as e:
            print(f"Error loading saved data: {e}")
    return {}, set()

# دالة حفظ البيانات في الملف تلقائياً عند أي تعديل
def save_data():
    try:
        serializable_scripts = {}
        for script_name, s_info in scripts_data.items():
            serializable_scripts[script_name] = {
                "version": s_info.get("version", "1.0.0"),
                "buyer_role_id": s_info.get("buyer_role_id"),
                "admin_role_id": s_info.get("admin_role_id"),
                "source_code": s_info.get("source_code", "-- Default Script"),
                "valid_keys": list(s_info.get("valid_keys", set())),
                "whitelisted_users": list(s_info.get("whitelisted_users", set())),
                "user_keys": {str(k): v for k, v in s_info.get("user_keys", {}).items()}
            }
        
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "scripts": serializable_scripts,
                "blacklisted_users": list(blacklisted_users)
            }, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving data: {e}")

# تحميل البيانات المحفوظة عند التشغيل
scripts_data, blacklisted_users = load_data()

# خادم الويب المدمج لخدمة السكربتات المباشرة مع فحص HWID
class DynamicScriptHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path_parts = parsed_path.path.strip("/").split("/")

        if len(path_parts) >= 2 and path_parts[0] == "script":
            script_name = urllib.parse.unquote(path_parts[1])
            if script_name in scripts_data:
                s_info = scripts_data[script_name]
                raw_source = s_info.get("source_code", "-- No source code loaded")
                
                valid_keys = list(s_info.get("valid_keys", set()))
                valid_keys_lua = "{" + ", ".join([f'["{k}"] = true' for k in valid_keys]) + "}"

                secured_wrapper = f"""
local valid_keys = {valid_keys_lua}
local user_key = rawget(_G, "script_key") or script_key

if not user_key or type(user_key) ~= "string" or not valid_keys[user_key] then
    local player = game:GetService("Players").LocalPlayer
    if player then player:Kick("key not found") end
    return
end

-- HWID and Executor Verification
local HttpService = game:GetService("HttpService")
local RbxAnalytics = game:GetService("RbxAnalyticsService")
local current_hwid = RbxAnalytics:GetClientId()
local current_executor = identifyexecutor and select(1, identifyexecutor()) or "Unknown"

_G.__HWID_DATA = _G.__HWID_DATA or {{}}
local user_hwid_info = _G.__HWID_DATA[user_key]

if not user_hwid_info then
    _G.__HWID_DATA[user_key] = {{ hwid = current_hwid, executor = current_executor }}
else
    if user_hwid_info.hwid ~= current_hwid or user_hwid_info.executor ~= current_executor then
        local player = game:GetService("Players").LocalPlayer
        if player then player:Kick("you need to reset hwid") end
        return
    end
end

-- Source Code Execution
{raw_source}
"""
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(secured_wrapper.encode("utf-8"))
                return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Crystal Hub API & Bot is online and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), DynamicScriptHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True

allowed_mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
bot = commands.Bot(command_prefix='!', intents=intents, allowed_mentions=allowed_mentions)

def generate_random_key(prefix="MUk5", length=32):
    chars = string.ascii_letters + string.digits
    return prefix + "".join(random.choice(chars) for _ in range(length - len(prefix)))

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
        scripts_data[self.script_name]["source_code"] = self.file_content
        save_data()
        await interaction.response.send_message(f"⚡ Source file saved & protected for `{self.script_name}`!", ephemeral=True)

    @discord.ui.button(label="No ❌", style=discord.ButtonStyle.danger)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        scripts_data[self.script_name]["source_code"] = self.file_content
        save_data()
        await interaction.response.send_message(f"✅ Source file attached for `{self.script_name}`!", ephemeral=True)


class PanelControlView(discord.ui.View):
    def __init__(self, script_name: str):
        super().__init__(timeout=None)
        self.script_name = script_name

    @discord.ui.button(label="Get Script 📜", style=discord.ButtonStyle.success)
    async def get_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("❌ Script details not found. Please re-run /create_script.", ephemeral=True)
            return

        user_id = interaction.user.id
        whitelisted = user_id in s_info["whitelisted_users"] or user_id in s_info["user_keys"]

        if not whitelisted:
            await interaction.response.send_message("❌ Access Denied! You must be whitelisted by an admin or redeem a valid key first.", ephemeral=True)
            return

        if user_id not in s_info["user_keys"]:
            new_key = generate_random_key()
            s_info["user_keys"][user_id] = new_key
            s_info["valid_keys"].add(new_key)
            save_data()

        user_key = s_info["user_keys"][user_id]
        app_url = os.getenv("RENDER_EXTERNAL_URL", "https://discord-bot-3u9m.onrender.com")
        custom_script_url = f"{app_url}/script/{urllib.parse.quote(self.script_name)}"

        response_text = (
            f"Here is your script:\n"
            f"```lua\n"
            f'script_key="{user_key}";\n'
            f'loadstring(game:HttpGet("{custom_script_url}"))()\n'
            f"```"
        )
        await interaction.response.send_message(response_text, ephemeral=True)

    @discord.ui.button(label="Redeem Key 🔑", style=discord.ButtonStyle.primary)
    async def redeem_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔑 Keys are tied to your account upon redemption. Contact admin or use your generated key.", ephemeral=True)

    @discord.ui.button(label="Reset HWID 🔄", style=discord.ButtonStyle.secondary)
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        user_id = interaction.user.id
        
        if s_info and user_id in s_info["user_keys"]:
            await interaction.response.send_message("🔄 HWID and Executor binding reset successfully! You can now launch from your new device/executor.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ You don't have an active key/script binding to reset.", ephemeral=True)

    @discord.ui.button(label="Get Buyer Role 👤", style=discord.ButtonStyle.secondary)
    async def get_buyer_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info or not s_info.get("buyer_role_id"):
            await interaction.response.send_message("❌ Buyer role is not configured for this script.", ephemeral=True)
            return

        user_id = interaction.user.id
        whitelisted = user_id in s_info["whitelisted_users"] or user_id in s_info["user_keys"]

        if not whitelisted:
            await interaction.response.send_message("❌ Access Denied! You cannot claim the buyer role unless you are whitelisted or activated a valid key.", ephemeral=True)
            return

        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role:
            if role in interaction.user.roles:
                await interaction.response.send_message("✅ You already have the buyer role!", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"🎉 Granted you the {role.mention} buyer role!", ephemeral=True)

    @discord.ui.button(label="Stats 📊", style=discord.ButtonStyle.secondary)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name, {})
        version = s_info.get("version", "1.0.0")
        await interaction.response.send_message(f"📊 **Script Stats:**\n• Name: `{self.script_name}`\n• Version: `{version}`\n• Status: `Active & HWID Protected`", ephemeral=True)


# ==================== Commands (Admin Protected) ==================== #

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands successfully.")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    print(f"Bot connected successfully as: {bot.user}")


# 1. إنشاء مفتاح جديد (Generate Key)
@bot.tree.command(name="generate_key", description="Generate a single-use key for a script")
@app_commands.describe(script="Target script")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def generate_key_command(interaction: discord.Interaction, script: str):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return

    new_key = generate_random_key()
    scripts_data[script]["valid_keys"].add(new_key)
    save_data()

    await interaction.response.send_message(
        f"🔑 **New Key Generated for `{script}`:**\n```text\n{new_key}\n```\n*This key is authorized for single-user activation.*",
        ephemeral=True
    )


# 2. إضافة إلى القائمة البيضاء (Whitelist User)
@bot.tree.command(name="whitelist", description="Whitelist a user for a script and notify them via DM")
@app_commands.describe(user="The member to whitelist", script="Target script")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_command(interaction: discord.Interaction, user: discord.Member, script: str):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return

    s_info = scripts_data[script]
    s_info["whitelisted_users"].add(user.id)
    
    new_key = generate_random_key()
    s_info["user_keys"][user.id] = new_key
    s_info["valid_keys"].add(new_key)
    save_data()

    if s_info.get("buyer_role_id"):
        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role and role not in user.roles:
            try:
                await user.add_roles(role)
            except Exception:
                pass

    try:
        dm_embed = discord.Embed(
            title="🎉 Whitelist Granted!",
            description=f"You have been whitelisted in **{interaction.guild.name}** for script: **{script}**!\n\nYour Key: `{new_key}`",
            color=0x00ff00
        )
        await user.send(embed=dm_embed)
        dm_status = "DM sent successfully."
    except Exception:
        dm_status = "Could not send DM (user DMs closed)."

    await interaction.response.send_message(f"✅ {user.mention} has been whitelisted for `{script}`! ({dm_status})", ephemeral=True)


# 3. إنشاء السكربت
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
        "source_code": "-- Default Script",
        "valid_keys": set(),
        "whitelisted_users": set(),
        "user_keys": {}
    }
    save_data()
    
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


# 4. ربط السورس بملف
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


# 5. تحديد رول الباير
@bot.tree.command(name="setbuyerrole", description="Change the buyer role for a script")
@app_commands.describe(script="Choose the script", buyer_role="Select the new buyer role")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def set_buyer_role_command(interaction: discord.Interaction, script: str, buyer_role: discord.Role):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    scripts_data[script]["buyer_role_id"] = buyer_role.id
    save_data()
    await interaction.response.send_message(f"✅ Buyer role for `{script}` updated to {buyer_role.mention}!", ephemeral=True)


# 6. إعداد البانل
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


# 7. تحديث السكربت
@bot.tree.command(name="update_script", description="Send a script update announcement")
@app_commands.describe(script="Choose the script", revision="New version (e.g. 1.0.1 or 99.0.0)", details="Enter update details")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def update_script_command(interaction: discord.Interaction, script: str, revision: str, details: str):
    if script in scripts_data:
        scripts_data[script]["version"] = revision
        save_data()

    ping_text = "@everyone @here"
    embed = discord.Embed(
        title=f"🚀 New Update for {script}!",
        description=f"___________________\n📌 **Version:** `{revision}`\n\n```\n{details}\n```\n___________________",
        color=0x00ff00
    )
    await interaction.response.send_message(content=ping_text, embed=embed)


# 8. مكافآت الدعوة
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


# 9. البلاك ليست
@bot.tree.command(name="blacklist", description="Add a user to the blacklist")
@app_commands.describe(user="The user to blacklist")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    blacklisted_users.add(user.id)
    save_data()
    await interaction.response.send_message(f"🚫 {user.mention} has been added to the blacklist.", ephemeral=True)

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN environment variable is missing!")
