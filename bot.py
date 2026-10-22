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

DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for script_name, s_info in data.get("scripts", {}).items():
                    s_info["valid_keys"] = set(s_info.get("valid_keys", []))
                    s_info["whitelisted_users"] = set(s_info.get("whitelisted_users", []))
                    s_info["user_keys"] = {int(k): v for k, v in s_info.get("user_keys", {}).items()}
                return data.get("scripts", {}), set(data.get("blacklisted_users", []))
        except Exception as e:
            print(f"Error loading saved data: {e}")
    return {}, set()

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

scripts_data, blacklisted_users = load_data()

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

# دالة التحقق من صلاحيات الإدارة أو الأدمن
def is_admin_or_has_role(interaction: discord.Interaction, script_name: str = None) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if script_name and script_name in scripts_data:
        admin_role_id = scripts_data[script_name].get("admin_role_id")
        if admin_role_id:
            role = interaction.guild.get_role(admin_role_id)
            if role and role in interaction.user.roles:
                return True
    return False

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
            await interaction.response.send_message("❌ Script details not found.", ephemeral=True)
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


# ==================== Commands ==================== #

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands successfully.")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    print(f"Bot connected successfully as: {bot.user}")


# 1. إنشاء مفتاح جديد
@bot.tree.command(name="generate_key", description="Generate a single-use key for a script")
@app_commands.describe(script="Target script")
@app_commands.autocomplete(script=script_autocomplete)
async def generate_key_command(interaction: discord.Interaction, script: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return

    new_key = generate_random_key()
    scripts_data[script]["valid_keys"].add(new_key)
    save_data()

    await interaction.response.send_message(
        f"🔑 **New Key Generated for `{script}`:**\n```text\n{new_key}\n```",
        ephemeral=True
    )


# 2. وايت ليست مستخدم
@bot.tree.command(name="whitelist", description="Whitelist a user for a script and notify them via DM")
@app_commands.describe(user="The member to whitelist", script="Target script")
@app_commands.autocomplete(script=script_autocomplete)
async def whitelist_command(interaction: discord.Interaction, user: discord.Member, script: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=
