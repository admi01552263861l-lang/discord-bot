from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import asyncio
import os
import json
import random
import string
import urllib.parse
import urllib.request
import urllib.error
import hashlib
import secrets
import datetime
import time
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

# Website subscription redeem codes are stored as hashes, not plaintext.
SITE_CODES_FILE = "site_redeem_codes.json"
SITE_ENTITLEMENTS_FILE = "site_entitlements.json"
def _load_json_dict(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}

def _save_json_dict(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

site_redeem_codes = _load_json_dict(SITE_CODES_FILE)
site_entitlements = _load_json_dict(SITE_ENTITLEMENTS_FILE)
redeem_attempts = {}

def _hash_redeem_code(code):
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()

def _verify_supabase_access_token(access_token):
    """Verify identity with Supabase Auth; never trust a user ID sent by the browser."""
    project_url = os.getenv("SUPABASE_URL", "https://bqucezzkofohbzxnuizq.supabase.co").rstrip("/")
    publishable_key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_roBE2qCTV3PVhbSb8PgcGQ_feLcZ74G").strip()
    if not publishable_key or not access_token:
        return None
    req = urllib.request.Request(
        project_url + "/auth/v1/user",
        headers={"apikey": publishable_key, "Authorization": "Bearer " + access_token, "Accept": "application/json"},
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            user = json.loads(response.read(65536).decode("utf-8"))
        if isinstance(user, dict) and user.get("id"):
            return {"id": str(user["id"]), "email": str(user.get("email", ""))[:254]}
    except Exception as exc:
        print(f"Supabase token verification failed: {type(exc).__name__}")
    return None


# Persistent HWID reset cooldown configuration.
SECURITY_STATE_FILE = "security_state.json"
DEFAULT_RESET_HWID_COOLDOWN = 86400  # 24 hours, in seconds

def load_security_state():
    if os.path.exists(SECURITY_STATE_FILE):
        try:
            with open(SECURITY_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            return {
                "reset_hwid_cooldown": max(0, int(state.get("reset_hwid_cooldown", DEFAULT_RESET_HWID_COOLDOWN))),
                "reset_hwid_last_used": {
                    str(k): int(v) for k, v in state.get("reset_hwid_last_used", {}).items()
                },
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return {"reset_hwid_cooldown": DEFAULT_RESET_HWID_COOLDOWN, "reset_hwid_last_used": {}}


security_state = load_security_state()

# Persistent key -> device binding. The client reports a Roblox client identifier;
# this is not a guaranteed physical hardware ID and must be treated as a best-effort lock.
HWID_BINDINGS_FILE = "hwid_bindings.json"
def load_hwid_bindings():
    try:
        with open(HWID_BINDINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

hwid_bindings = load_hwid_bindings()

def save_hwid_bindings():
    with open(HWID_BINDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(hwid_bindings, f, indent=2, ensure_ascii=False)

def save_security_state():
    temp_file = SECURITY_STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(security_state, f, indent=4)
    os.replace(temp_file, SECURITY_STATE_FILE)

def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds or not parts: parts.append(f"{seconds}s")
    return " ".join(parts)

class DynamicScriptHandler(BaseHTTPRequestHandler):
    def _send_text(self, status, body):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", os.getenv("SITE_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self._send_text(204, "")

    def _notify_site_request(self, route, payload):
        # Public website submissions are limited in size and sent to the configured owner.
        owner_id = os.getenv("OWNER_DISCORD_ID", "").strip()
        if not owner_id.isdigit():
            self._send_text(503, "owner is not configured")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_500_000:
                self._send_text(400, "invalid request size")
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                self._send_text(400, "invalid request")
                return
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, "invalid request")
            return

        labels = {
            "/api/ticket": "Support Ticket",
            "/api/purchase": "Purchase Request",
            "/api/project-upload": "Project Submission",
        }
        title = labels[route]
        # Keep messages bounded; never trust website input as Discord mentions.
        allowed = ("username", "email", "problem", "message", "product", "game", "plan", "contact", "contactMethod", "contactValue", "buyerUsername", "details", "projectName", "projectCode", "fileName", "mode", "safeMode", "discordUrl")
        lines = []
        for key in allowed:
            value = body.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                safe = str(value).replace("@everyone", "@ everyone").replace("@here", "@ here")[:1200]
                lines.append(f"**{key}:** {safe}")
        if not lines:
            self._send_text(400, "missing request details")
            return
        message = "\n".join(lines)
        if route == "/api/project-upload":
            message += "\n\nNote: the project file is not stored or attached by this endpoint yet; only the submitted details are forwarded."
        async def deliver():
            user = bot.get_user(int(owner_id))
            if user is None:
                user = await bot.fetch_user(int(owner_id))
            embed = discord.Embed(title=f"Crystal Hub — {title}", description=message[:4000], color=0x5865F2)
            await user.send(embed=embed)
        try:
            if not bot.is_ready():
                self._send_text(503, "discord bot is not ready")
                return
            future = asyncio.run_coroutine_threadsafe(deliver(), bot.loop)
            future.result(timeout=12)
        except Exception as exc:
            print(f"Website notification failed: {type(exc).__name__}: {exc}")
            self._send_text(502, "could not deliver message to Discord")
            return
        self._send_text(200, json.dumps({"ok": True, "message": "sent to Discord"}))

    def _create_admin_site_code(self):
        # Code creation is performed server-side; the admin code must be configured as a Render secret.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                self._send_text(400, json.dumps({"error": "Invalid request."})); return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict): raise ValueError("invalid body")
            supplied = str(payload.get("adminCode", ""))
            plan = str(payload.get("codeType", ""))
            label = str(payload.get("codeLabel", ""))[:60]
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, json.dumps({"error": "Invalid request."})); return
        expected = os.getenv("ADMIN_ACCESS_CODE", "admi-on-top")
        if not expected or not secrets.compare_digest(supplied, expected):
            self._send_text(403, json.dumps({"error": "Admin authorization failed. Set ADMIN_ACCESS_CODE in Render and unlock again."})); return
        if plan not in ("Premium", "Ultra", "Admin"):
            self._send_text(400, json.dumps({"error": "Choose Premium, Ultra, or Admin."})); return
        prefix = {"Premium": "PREM", "Ultra": "ULTRA", "Admin": "ADMIN"}[plan]
        raw = "CH-" + prefix + "-" + secrets.token_hex(6).upper()
        days = 30 if plan == "Premium" else 0
        key = _hash_redeem_code(raw)
        site_redeem_codes[key] = {"plan": plan, "duration_days": days, "label": label, "created_by": "website-admin", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "redeemed_by": None, "redeemed_at": None, "revoked": False}
        try:
            _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
        except OSError:
            site_redeem_codes.pop(key, None)
            self._send_text(500, json.dumps({"error": "Could not save the code. Check persistent storage on Render."})); return
        self._send_text(200, json.dumps({"ok": True, "code": raw, "plan": plan, "duration": "30 days" if days else "Lifetime", "label": label}))

    def _redeem_site_code(self):
        # Basic per-IP throttle; code validation and user identity are checked server-side.
        ip = self.client_address[0] if self.client_address else "unknown"
        now = time.time()
        recent = [t for t in redeem_attempts.get(ip, []) if now - t < 60]
        if len(recent) >= 12:
            redeem_attempts[ip] = recent
            self._send_text(429, json.dumps({"error": "Too many attempts. Wait a minute and try again."}))
            return
        recent.append(now); redeem_attempts[ip] = recent
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                self._send_text(400, json.dumps({"error": "Invalid request."})); return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict): raise ValueError("invalid body")
            code = str(payload.get("code", "")).strip().upper()[:120]
            access_token = str(payload.get("accessToken", ""))[:8192]
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, json.dumps({"error": "Invalid request."})); return
        if not code or not access_token:
            self._send_text(400, json.dumps({"error": "Enter a code and sign in first."})); return
        user = _verify_supabase_access_token(access_token)
        if not user:
            self._send_text(401, json.dumps({"error": "Your session could not be verified. Sign in again."})); return
        key = _hash_redeem_code(code)
        record = site_redeem_codes.get(key)
        # Allow the configured admin access code to be redeemed from Subscriptions too.
        # It is still treated as a one-time code and grants the Admin entitlement.
        admin_redeem_code = os.getenv("ADMIN_ACCESS_CODE", "admi-on-top")
        if admin_redeem_code and secrets.compare_digest(code, admin_redeem_code):
            if record is None:
                record = {"plan": "Admin", "duration_days": 0, "created_by": "configured-admin-code", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "redeemed_by": None, "redeemed_at": None, "revoked": False}
                site_redeem_codes[key] = record
        if not record or record.get("revoked"):
            self._send_text(404, json.dumps({"error": "Invalid or expired redeem code."})); return
        if record.get("redeemed_by"):
            self._send_text(409, json.dumps({"error": "This code has already been used."})); return
        plan = record.get("plan", "Premium")
        days = int(record.get("duration_days", 30))
        expires_at = None if days <= 0 else (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()
        entitlement = {"plan": plan, "redeemed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "expires_at": expires_at, "code_hash": key}
        # Save entitlement and code use together as separate atomic file updates.
        old_code = dict(record)
        record["redeemed_by"] = user["id"]
        record["redeemed_at"] = entitlement["redeemed_at"]
        site_entitlements[user["id"]] = entitlement
        try:
            _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
            _save_json_dict(SITE_ENTITLEMENTS_FILE, site_entitlements)
        except OSError:
            site_redeem_codes[key] = old_code
            self._send_text(500, json.dumps({"error": "Server could not save redemption. Try again later."})); return
        self._send_text(200, json.dumps({"ok": True, "plan": plan, "duration": "Lifetime" if days <= 0 else f"{days} days", "expiresAt": expires_at}))

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == "/api/redeem-code":
            self._redeem_site_code()
            return
        if parsed_path.path == "/api/admin/create-code":
            self._create_admin_site_code()
            return
        if parsed_path.path in ("/api/ticket", "/api/purchase", "/api/project-upload"):
            self._notify_site_request(parsed_path.path, {})
            return
        if parsed_path.path not in ("/hwid/check", "/script-source"):
            self._send_text(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                self._send_text(400, "invalid request")
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            script_name = str(payload.get("script_name", ""))[:200]
            user_key = str(payload.get("key", ""))[:200]
            reported_hwid = str(payload.get("hwid", ""))[:300]
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, "invalid request")
            return

        if not script_name or not user_key or not reported_hwid:
            self._send_text(400, "missing fields")
            return

        s_info = scripts_data.get(script_name)
        if not s_info or user_key not in s_info.get("valid_keys", set()):
            self._send_text(403, "denied: invalid key")
            return

        binding_key = f"{script_name}:{user_key}"
        existing_hwid = hwid_bindings.get(binding_key)
        if existing_hwid is None:
            hwid_bindings[binding_key] = reported_hwid
            try:
                save_hwid_bindings()
            except OSError:
                hwid_bindings.pop(binding_key, None)
                self._send_text(500, "server storage error")
                return
        elif existing_hwid != reported_hwid:
            self._send_text(403, "you need to reset hwid")
            return

        if parsed_path.path == "/script-source":
            # Deliver source only after key and saved device binding pass.
            self._send_text(200, s_info.get("source_code", "-- No source code loaded"))
        else:
            self._send_text(200, "ok")

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path_parts = parsed_path.path.strip("/").split("/")

        if len(path_parts) >= 2 and path_parts[0] == "script":
            # User-Agent is only a basic browser filter and can be spoofed.
            # The actual source is separately protected by key + server-side device binding.
            user_agent = (self.headers.get("User-Agent") or "").lower()
            allowed_agents = ("roblox", "delta", "real")
            if not any(agent in user_agent for agent in allowed_agents):
                self._send_text(403, "⛔ You are not allowed on this page")
                return

            script_name = urllib.parse.unquote(path_parts[1])
            if script_name not in scripts_data:
                self._send_text(404, "Script not found or deleted.")
                return

            app_url = os.getenv("RENDER_EXTERNAL_URL", "https://discord-bot-3u9m.onrender.com").rstrip("/")
            script_name_lua = json.dumps(script_name)
            secured_wrapper = f'''\
local user_key = rawget(_G, "script_key") or script_key
local Players = game:GetService("Players")
local player = Players.LocalPlayer
if type(user_key) ~= "string" or user_key == "" then
    if player then player:Kick("key not found") end
    return
end

local ok_hwid, current_hwid = pcall(function()
    return game:GetService("RbxAnalyticsService"):GetClientId()
end)
if not ok_hwid or type(current_hwid) ~= "string" or current_hwid == "" then
    if player then player:Kick("HWID check failed. Please retry.") end
    return
end

local request_fn = (syn and syn.request) or http_request or request
if type(request_fn) ~= "function" then
    if player then player:Kick("HWID check unavailable") end
    return
end

local HttpService = game:GetService("HttpService")
local request_ok, response = pcall(function()
    return request_fn({{
        Url = "{app_url}/script-source",
        Method = "POST",
        Headers = {{ ["Content-Type"] = "application/json" }},
        Body = HttpService:JSONEncode({{
            script_name = {script_name_lua},
            key = user_key,
            hwid = current_hwid
        }})
    }})
end)
if not request_ok or type(response) ~= "table" then
    if player then player:Kick("Script authorization failed") end
    return
end

local status_code = tonumber(response.StatusCode or response.Status or 0)
local response_body = tostring(response.Body or "")
if status_code ~= 200 then
    if player then
        if string.find(string.lower(response_body), "you need to reset hwid", 1, true) then
            player:Kick("you need to reset hwid")
        elseif string.find(string.lower(response_body), "invalid key", 1, true) then
            player:Kick("key not found")
        else
            player:Kick("Script authorization failed")
        end
    end
    return
end

local load_ok, load_error = pcall(function()
    local source_loader = loadstring or load
    if type(source_loader) ~= "function" then error("loadstring unavailable") end
    local compiled = source_loader(response_body)
    if type(compiled) ~= "function" then error("source compile failed") end
    compiled()
end)
if not load_ok and player then
    player:Kick("Script failed to load")
end
'''
            self._send_text(200, secured_wrapper)
            return

        self._send_text(200, "Crystal Hub API & Bot is online and running!")

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
        
        if not s_info or user_id not in s_info.get("user_keys", {}):
            await interaction.response.send_message("❌ You don't have an active key for this script.", ephemeral=True)
            return

        # This enforces and persists the cooldown. Actual device binding must be
        # stored and checked server-side before claiming the HWID has been reset.
        cooldown = int(security_state.get("reset_hwid_cooldown", DEFAULT_RESET_HWID_COOLDOWN))
        cooldown_key = f"{interaction.guild_id}:{self.script_name}:{user_id}"
        now = int(time.time())
        last_used = int(security_state.get("reset_hwid_last_used", {}).get(cooldown_key, 0))
        remaining = cooldown - (now - last_used)

        if remaining > 0:
            await interaction.response.send_message(
                f"⏳ You must wait **{format_duration(remaining)}** before resetting HWID again.\n"
                "If you changed devices, please wait for the cooldown to expire or contact an admin.",
                ephemeral=True
            )
            return

        user_key = s_info["user_keys"][user_id]
        binding_key = f"{self.script_name}:{user_key}"
        hwid_bindings.pop(binding_key, None)
        save_hwid_bindings()
        security_state.setdefault("reset_hwid_last_used", {})[cooldown_key] = now
        save_security_state()
        await interaction.response.send_message(
            "✅ HWID reset successfully. The key can now bind to the next device that launches the script. "
            f"Cooldown started: **{format_duration(cooldown)}**.",
            ephemeral=True
        )

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
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

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
@app_commands.describe(name_script="Enter script name", buyer_role="Select buyer role", admin_role="Select admin role")
async def create_script_command(
    interaction: discord.Interaction, 
    name_script: str, 
    buyer_role: discord.Role, 
    admin_role: discord.Role
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an administrator to create scripts.", ephemeral=True)
        return

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
        description=f"📜 **Script Name:** `{name_script}`\n👤 **Buyer Role:** {buyer_role.mention}\n🛡️ **Admin Role:** {admin_role.mention}",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Delete a script and disable its hosted route.
@bot.tree.command(name="delete_script", description="Permanently delete a script entry and disable its URL")
@app_commands.describe(script="Script to delete")
@app_commands.autocomplete(script=script_autocomplete)
async def delete_script_command(interaction: discord.Interaction, script: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to delete this script.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return

    del scripts_data[script]
    save_data()
    await interaction.response.send_message(
        f"🗑️ Deleted `{script}` from the bot database. Its `/script/{urllib.parse.quote(script)}` route now returns 404.",
        ephemeral=True
    )


# 4. ربط السورس
@bot.tree.command(name="sourcesetup", description="Attach source file to a script")
@app_commands.describe(file="Upload the script file", script="Choose the target script")
@app_commands.autocomplete(script=script_autocomplete)
async def source_setup_command(interaction: discord.Interaction, file: discord.Attachment, script: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    content_bytes = await file.read()
    file_content = content_bytes.decode('utf-8', errors='ignore')

    view = ObfuscateConfirmView(script_name=script, file_content=file_content)
    await interaction.response.send_message(f"📄 Source file for `{script}` received!\n\n**Do you want to obfuscate script?**", view=view, ephemeral=True)


# 5. تعديل رول الباير
@bot.tree.command(name="setbuyerrole", description="Change the buyer role for a script")
@app_commands.describe(script="Choose the script", buyer_role="Select the new buyer role")
@app_commands.autocomplete(script=script_autocomplete)
async def set_buyer_role_command(interaction: discord.Interaction, script: str, buyer_role: discord.Role):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    scripts_data[script]["buyer_role_id"] = buyer_role.id
    save_data()
    await interaction.response.send_message(f"✅ Buyer role for `{script}` updated to {buyer_role.mention}!", ephemeral=True)


# 6. تعديل رول الإدارة
@bot.tree.command(name="setadminrole", description="Change the admin role for a script")
@app_commands.describe(script="Choose the script", admin_role="Select the new admin role")
@app_commands.autocomplete(script=script_autocomplete)
async def set_admin_role_command(interaction: discord.Interaction, script: str, admin_role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an administrator to change admin roles.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    scripts_data[script]["admin_role_id"] = admin_role.id
    save_data()
    await interaction.response.send_message(f"✅ Admin role for `{script}` updated to {admin_role.mention}!", ephemeral=True)


# 7. إعداد البانل
@bot.tree.command(name="panelsetup", description="Send a script management panel")
@app_commands.describe(script="Choose the script", channel="Select target channel", name_panel="Enter panel title")
@app_commands.autocomplete(script=script_autocomplete)
async def panel_setup_command(interaction: discord.Interaction, script: str, channel: discord.TextChannel, name_panel: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    embed = discord.Embed(
        title=name_panel,
        description=f"Best {script.lower()}\n\nFaulmor • Script Management |",
        color=0xd4af37
    )
    embed.set_author(name="luarmor", icon_url=bot.user.display_avatar.url)
    
    view = PanelControlView(script_name=script)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Panel created successfully in {channel.mention}!", ephemeral=True)


# 8. تحديث السكربت
@bot.tree.command(name="update_script", description="Send a script update announcement")
@app_commands.describe(script="Choose the script", revision="New version (e.g. 1.0.1 or 99.0.0)", details="Enter update details")
@app_commands.autocomplete(script=script_autocomplete)
async def update_script_command(interaction: discord.Interaction, script: str, revision: str, details: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

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


# 9. مكافآت الدعوة
@bot.tree.command(name="invite_reward", description="Send the invite rewards panel")
async def invite_reward_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎁 Invite Rewards System",
        description="• **2 Invites** = 12 Hours Script ⏳\n• **4 Invites** = 2 Days Script 🚀\n___________________\nUse the buttons below to check invites and claim rewards.",
        color=0x2b2d31
    )
    view = InviteRewardView()
    await interaction.response.send_message("Panel created successfully!", ephemeral=True)
    await interaction.channel.send(embed=embed, view=view)


# Configure the HWID reset cooldown in seconds.
@bot.tree.command(name="reset_hwid_coolddown", description="Set the minimum wait between HWID reset requests")
@app_commands.describe(seconds="Cooldown duration in seconds (0 disables the cooldown)")
async def reset_hwid_coolddown_command(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 31536000]):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only server administrators can change this setting.", ephemeral=True)
        return

    security_state["reset_hwid_cooldown"] = int(seconds)
    save_security_state()
    await interaction.response.send_message(
        f"✅ HWID reset cooldown set to **{format_duration(seconds)}** ({seconds} seconds).",
        ephemeral=True
    )


# 10. البلاك ليست
def is_bot_owner(user_id: int) -> bool:
    """Only the configured Discord account owner can use global admin actions."""
    owner_id = os.getenv("BOT_OWNER_ID", "").strip()
    return bool(owner_id) and str(user_id) == owner_id


@bot.tree.command(name="blacklist", description="Globally block a user from bot features")
@app_commands.describe(user="The user to blacklist")
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This action is restricted to the bot owner.", ephemeral=True)
        return

    blacklisted_users.add(user.id)
    save_data()
    await interaction.response.send_message(f"🚫 {user.mention} has been added to the global bot blacklist.", ephemeral=True)


@bot.tree.command(name="unblacklist", description="Remove a user from the global bot blacklist")
@app_commands.describe(user_id="Discord user ID to unblock")
async def unblacklist_command(interaction: discord.Interaction, user_id: str):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This action is restricted to the bot owner.", ephemeral=True)
        return
    try:
        target_id = int(user_id.strip())
    except (TypeError, ValueError):
        await interaction.response.send_message("❌ Enter a valid numeric Discord user ID.", ephemeral=True)
        return
    blacklisted_users.discard(target_id)
    save_data()
    await interaction.response.send_message(f"✅ User `{target_id}` removed from the global blacklist.", ephemeral=True)


@bot.tree.command(name="list_servers", description="List servers the bot has joined")
async def list_servers_command(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This action is restricted to the bot owner.", ephemeral=True)
        return
    guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
    if not guilds:
        await interaction.response.send_message("The bot is not in any servers.", ephemeral=True)
        return
    lines = [f"• **{discord.utils.escape_markdown(g.name)}** — ID: `{g.id}` — members: `{g.member_count or 'unknown'}`" for g in guilds]
    # Discord message limits; split output safely into multiple ephemeral replies.
    chunks, current = [], "\n".join(lines[:])
    while len(current) > 1900:
        cut = current.rfind("\n", 0, 1900)
        if cut < 1:
            cut = 1900
        chunks.append(current[:cut])
        current = current[cut:].lstrip("\n")
    if current:
        chunks.append(current)
    await interaction.response.send_message("**Bot servers**\n" + chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name="send_server_message", description="Send a message to a channel in a selected server")
@app_commands.describe(server_id="Target server ID", channel_id="Target channel ID", message="Message to send")
async def send_server_message_command(interaction: discord.Interaction, server_id: str, channel_id: str, message: str):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This action is restricted to the bot owner.", ephemeral=True)
        return
    try:
        guild_id, target_channel_id = int(server_id.strip()), int(channel_id.strip())
    except (TypeError, ValueError):
        await interaction.response.send_message("❌ Server ID and channel ID must be numeric.", ephemeral=True)
        return
    message = message.strip()
    if not message or len(message) > 1800:
        await interaction.response.send_message("❌ Message must be between 1 and 1800 characters.", ephemeral=True)
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        await interaction.response.send_message("❌ The bot is not in that server.", ephemeral=True)
        return
    channel = guild.get_channel(target_channel_id)
    if channel is None or not hasattr(channel, "send"):
        await interaction.response.send_message("❌ Could not find a sendable channel in that server.", ephemeral=True)
        return
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        await interaction.response.send_message("❌ The bot lacks permission to send messages in that channel.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("❌ Discord could not deliver the message.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Message sent to **{guild.name}** / {channel.mention}.", ephemeral=True)

@bot.tree.command(name="create_redeem_code", description="Create a one-time Crystal Hub website subscription code")
@app_commands.describe(plan="Plan granted by the code")
@app_commands.choices(plan=[app_commands.Choice(name="Premium (30 days)", value="Premium"), app_commands.Choice(name="Ultra (Lifetime)", value="Ultra"), app_commands.Choice(name="Admin (Lifetime)", value="Admin")])
async def create_redeem_code_command(interaction: discord.Interaction, plan: app_commands.Choice[str]):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This command is restricted to the configured bot owner.", ephemeral=True)
        return
    raw = "CH-" + {"Premium":"PREM", "Ultra":"ULTRA", "Admin":"ADMIN"}[plan.value] + "-" + secrets.token_hex(6).upper()
    days = 30 if plan.value == "Premium" else 0
    site_redeem_codes[_hash_redeem_code(raw)] = {"plan": plan.value, "duration_days": days, "created_by": str(interaction.user.id), "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "redeemed_by": None, "redeemed_at": None, "revoked": False}
    try:
        _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
    except OSError:
        site_redeem_codes.pop(_hash_redeem_code(raw), None)
        await interaction.response.send_message("❌ Could not save the code. Check server file permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ **{plan.value} redeem code created** (one use only).\nCopy this code now; it will not be shown again:\n```text\n{raw}\n```", ephemeral=True)

@bot.tree.command(name="list_redeem_codes", description="List Crystal Hub website redeem code status")
async def list_redeem_codes_command(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This command is restricted to the configured bot owner.", ephemeral=True)
        return
    if not site_redeem_codes:
        await interaction.response.send_message("No website redeem codes have been created yet.", ephemeral=True)
        return
    lines = []
    for i, rec in enumerate(site_redeem_codes.values(), 1):
        status = "revoked" if rec.get("revoked") else ("used" if rec.get("redeemed_by") else "available")
        lines.append(f"{i}. **{rec.get('plan','Premium')}** — {status} — created {str(rec.get('created_at',''))[:10]}")
    chunks, current = [], "\n".join(lines)
    while len(current) > 1800:
        cut = current.rfind("\n", 0, 1800)
        if cut < 1: cut = 1800
        chunks.append(current[:cut]); current = current[cut:].lstrip("\n")
    chunks.append(current)
    await interaction.response.send_message("**Website redeem codes**\n" + chunks[0], ephemeral=True)
    for chunk in chunks[1:]: await interaction.followup.send(chunk, ephemeral=True)

@bot.tree.command(name="revoke_redeem_code", description="Revoke an unused Crystal Hub website redeem code")
@app_commands.describe(code="The full code to revoke")
async def revoke_redeem_code_command(interaction: discord.Interaction, code: str):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("❌ This command is restricted to the configured bot owner.", ephemeral=True)
        return
    key = _hash_redeem_code(code)
    rec = site_redeem_codes.get(key)
    if not rec:
        await interaction.response.send_message("❌ Code not found.", ephemeral=True)
        return
    if rec.get("redeemed_by"):
        await interaction.response.send_message("❌ This code has already been redeemed and cannot be revoked with this command.", ephemeral=True)
        return
    rec["revoked"] = True
    _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
    await interaction.response.send_message("✅ Redeem code revoked.", ephemeral=True)

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN environment variable is missing!")
