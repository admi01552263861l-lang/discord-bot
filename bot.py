from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import asyncio
import os
import json
import random
import re
import string
import urllib.parse
import urllib.request
import urllib.error
import hashlib
import secrets
import datetime
import difflib
import time
import io
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

DATA_FILE = "data.json"

# One server-wide admin role controls bot management actions across all scripts.
ADMIN_ROLE_CONFIG_FILE = "crystal_hub_admin_roles.json"

def _load_global_admin_roles():
    try:
        with open(ADMIN_ROLE_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        roles = raw.get("guild_roles", {}) if isinstance(raw, dict) else {}
        return {str(guild_id): int(role_id) for guild_id, role_id in roles.items()
                if str(guild_id).isdigit() and str(role_id).isdigit()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}

GLOBAL_ADMIN_ROLE_IDS = _load_global_admin_roles()

def _save_global_admin_roles():
    try:
        with open(ADMIN_ROLE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"guild_roles": GLOBAL_ADMIN_ROLE_IDS}, f, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        print(f"Could not save global admin roles: {exc}")
        return False

def _global_admin_role_id(guild_id):
    return GLOBAL_ADMIN_ROLE_IDS.get(str(guild_id)) if guild_id else None

def _has_global_admin_access(interaction):
    """Only members with the configured server-wide admin role can manage bot features.
    Discord Administrator permission is required to SET this role, but does not replace it
    for day-to-day bot management. This role never grants subscription benefits.
    """
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    role_id = _global_admin_role_id(interaction.guild.id)
    if not role_id:
        return False
    role = interaction.guild.get_role(role_id)
    return bool(role and role in interaction.user.roles)

def _global_admin_status(interaction):
    if not interaction.guild:
        return False, "This command can only be used inside a server."
    if not _global_admin_role_id(interaction.guild.id):
        return False, "❌ Set the server-wide admin role first with `/set-admin-role`."
    if _has_global_admin_access(interaction):
        return True, ""
    return False, "❌ You need the configured admin role or Discord Administrator permission."

# Discord admin redeem access
ADMIN_REDEEM_FILE = "discord_admin_redeems.json"
ADMIN_REDEEM_CODE = os.getenv("ADMIN_REDEEM_CODE", "admi-on-top").strip().upper()

def load_admin_redeems():
    try:
        with open(ADMIN_REDEEM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(str(x) for x in data.get("users", []))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()

def save_admin_redeems():
    with open(ADMIN_REDEEM_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": sorted(admin_redeemed_users)}, f, indent=2)

admin_redeemed_users = load_admin_redeems()

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for script_name, s_info in data.get("scripts", {}).items():
                    s_info["valid_keys"] = set(s_info.get("valid_keys", []))
                    s_info["whitelisted_users"] = set(s_info.get("whitelisted_users", []))
                    s_info["user_keys"] = {int(k): v for k, v in s_info.get("user_keys", {}).items()}
                    # Persistent panel location/message so /panel-setup does not create duplicates.
                    s_info.setdefault("panel_guild_id", None)
                    s_info.setdefault("panel_channel_id", None)
                    s_info.setdefault("panel_message_id", None)
                    s_info.setdefault("panel_name", None)
                    s_info.setdefault("panel_description", "")
                    s_info.setdefault("trail_enabled", False)
                    s_info.setdefault("trail_mode", "PERM")
                    s_info.setdefault("trail_expires_at", None)
                    s_info.setdefault("file_id", None)
                    s_info.setdefault("source_obfuscated", False)
                    s_info.setdefault("key_meta", {})
                    s_info.setdefault("user_access", {})
                    # Normalize access maps
                    if not isinstance(s_info.get("key_meta"), dict):
                        s_info["key_meta"] = {}
                    ua = {}
                    for k, v in (s_info.get("user_access") or {}).items():
                        try:
                            ua[int(k)] = v if isinstance(v, dict) else {}
                        except (TypeError, ValueError):
                            continue
                    s_info["user_access"] = ua
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
                "source_obfuscated": bool(s_info.get("source_obfuscated", False)),
                "panel_guild_id": s_info.get("panel_guild_id"),
                "panel_channel_id": s_info.get("panel_channel_id"),
                "panel_message_id": s_info.get("panel_message_id"),
                "panel_name": s_info.get("panel_name"),
                "panel_description": s_info.get("panel_description", ""),
                "valid_keys": list(s_info.get("valid_keys", set())),
                "whitelisted_users": list(s_info.get("whitelisted_users", set())),
                "user_keys": {str(k): v for k, v in s_info.get("user_keys", {}).items()},
                "trail_enabled": bool(s_info.get("trail_enabled", False)),
                "trail_mode": s_info.get("trail_mode", "PERM"),
                "trail_expires_at": s_info.get("trail_expires_at"),
                "file_id": s_info.get("file_id"),
                "key_meta": s_info.get("key_meta") if isinstance(s_info.get("key_meta"), dict) else {},
                "user_access": {str(k): v for k, v in (s_info.get("user_access") or {}).items()},
                "created_by": s_info.get("created_by"),
                "panel_created_by": s_info.get("panel_created_by"),
            }
        
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "scripts": serializable_scripts,
                "blacklisted_users": list(blacklisted_users)
            }, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving data: {e}")
        return False

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

DISCORD_PLAN_CODES_FILE = "discord_plan_codes.json"
DISCORD_PLAN_ENTITLEMENTS_FILE = "discord_plan_entitlements.json"
MONTHLY_USAGE_FILE = "discord_monthly_usage.json"
discord_plan_codes = _load_json_dict(DISCORD_PLAN_CODES_FILE)
discord_plan_entitlements = _load_json_dict(DISCORD_PLAN_ENTITLEMENTS_FILE)
monthly_usage = _load_json_dict(MONTHLY_USAGE_FILE)
FREE_LIMITS = {"scripts": 5, "panels": 5, "sources": 20, "keys": 100, "avatars": 0}
PREMIUM_LIMITS = {"scripts": 10, "panels": 10, "sources": 100, "keys": 1000, "avatars": 5}

def _month_key():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")

def _discord_plan(user_id):
    rec = discord_plan_entitlements.get(str(user_id), {})
    if not isinstance(rec, dict):
        return "Free"
    expires = rec.get("expires_at")
    if expires:
        try:
            if datetime.datetime.fromisoformat(expires) <= datetime.datetime.now(datetime.timezone.utc):
                discord_plan_entitlements.pop(str(user_id), None)
                _save_json_dict(DISCORD_PLAN_ENTITLEMENTS_FILE, discord_plan_entitlements)
                return "Free"
        except (ValueError, TypeError):
            return "Free"
    return str(rec.get("plan", "Free"))

def _quota_check(user_id, category):
    plan = _discord_plan(user_id)
    if plan in ("Ultra", "Admin"):
        return True, None
    limits = PREMIUM_LIMITS if plan == "Premium" else FREE_LIMITS
    limit = limits.get(category)
    if limit is None:
        return True, None

    # Scripts/panels are active inventory limits. Deleting an item frees a slot.
    # "keys" is one shared active pool: a generated key OR a whitelist grant
    # consumes exactly one slot. Deleting the key frees that slot.
    if category == "keys":
        owned_keys = 0
        for s_info in scripts_data.values():
            for key, meta in (s_info.get("key_meta") or {}).items():
                if not isinstance(meta, dict):
                    continue
                if str(meta.get("created_by", "")) != str(user_id):
                    continue
                if key not in s_info.get("valid_keys", set()):
                    continue
                if meta.get("disabled"):
                    continue
                expires_at = meta.get("expires_at")
                if expires_at:
                    try:
                        if datetime.datetime.fromisoformat(expires_at) <= datetime.datetime.now(datetime.timezone.utc):
                            continue
                    except (ValueError, TypeError):
                        pass
                owned_keys += 1
        if owned_keys >= limit:
            return False, f"{plan} key/whitelist limit reached: {limit} active slots. Delete a key to free a slot."
        return True, None

    if category == "scripts":
        owned = sum(1 for item in scripts_data.values()
                    if str(item.get("created_by", "")) == str(user_id))
        if owned >= limit:
            return False, f"{plan} limit reached: {limit} active scripts. Delete one to free a slot."
        return True, None
    if category == "panels":
        owned = sum(1 for item in scripts_data.values()
                    if str(item.get("panel_created_by", item.get("created_by", ""))) == str(user_id)
                    and item.get("panel_message_id"))
        if owned >= limit:
            return False, f"{plan} limit reached: {limit} active panels. Delete one to free a slot."
        return True, None

    month = _month_key()
    rec = monthly_usage.setdefault(str(user_id), {})
    if rec.get("month") != month:
        rec.clear()
        rec["month"] = month
    if int(rec.get(category, 0)) >= limit:
        return False, f"{plan} monthly limit reached: {limit} {category}. Your usage resets next month."
    return True, None

def _quota_increment(user_id, category):
    month = _month_key()
    rec = monthly_usage.setdefault(str(user_id), {})
    if rec.get("month") != month:
        rec.clear()
        rec["month"] = month
    rec[category] = int(rec.get(category, 0)) + 1
    _save_json_dict(MONTHLY_USAGE_FILE, monthly_usage)

def _quota_status(user_id):
    plan = _discord_plan(user_id)
    limits = PREMIUM_LIMITS if plan == "Premium" else FREE_LIMITS
    if plan in ("Ultra", "Admin"):
        return f"{plan} — unlimited"
    rec = monthly_usage.setdefault(str(user_id), {})
    if rec.get("month") != _month_key():
        rec = {"month": _month_key()}
    names = {"scripts": "active scripts", "panels": "active panels", "sources": "sources this month", "keys": "active keys/whitelists", "avatars": "avatar changes this month"}
    active_scripts = sum(1 for item in scripts_data.values() if str(item.get("created_by", "")) == str(user_id))
    active_panels = sum(1 for item in scripts_data.values()
                        if str(item.get("panel_created_by", item.get("created_by", ""))) == str(user_id)
                        and item.get("panel_message_id"))
    parts = []
    for category, limit in limits.items():
        if limit is not None:
            used = active_scripts if category == "scripts" else active_panels if category == "panels" else int(rec.get(category, 0))
            parts.append(f"{names[category]}: {used}/{limit}")
    return f"{plan} plan — " + " • ".join(parts)

def _make_discord_plan_code(plan, days, custom_code=None):
    raw = (str(custom_code).strip().upper() if custom_code else "CRYSTAL-" + plan.upper() + "-" + secrets.token_hex(5).upper())
    code_hash = _hash_redeem_code(raw)
    if code_hash in discord_plan_codes:
        raise ValueError("That code already exists.")
    discord_plan_codes[code_hash] = {
        "code": raw, "plan": plan, "duration_days": int(days),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "redeemed_by": None, "redeemed_at": None
    }
    _save_json_dict(DISCORD_PLAN_CODES_FILE, discord_plan_codes)
    return raw

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


def _script_api_key_valid(query_key: str) -> bool:
    expected = os.getenv("SCRIPT_API_KEY", "").strip()
    if not expected:
        return False
    return secrets.compare_digest(str(query_key or ""), expected)

def _script_record_payload(script_name: str, s_info: dict) -> dict:
    return {
        "ok": True,
        "script": {
            "name": script_name,
            "version": s_info.get("version", "1.0.0"),
            "source_code": s_info.get("source_code", ""),
            "buyer_role_id": s_info.get("buyer_role_id"),
            "admin_role_id": s_info.get("admin_role_id"),
        }
    }

def _load_script_from_api_url(api_url: str) -> dict:
    """Fetch a Crystal Hub script API payload for /create-script."""
    parsed = urllib.parse.urlparse(api_url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("Invalid API URL.")

    allowed_hosts = []
    for raw in (
        os.getenv("SCRIPT_API_HOST", ""),
        os.getenv("SITE_ORIGIN", ""),
        os.getenv("RENDER_EXTERNAL_URL", ""),
    ):
        raw = raw.strip()
        if raw:
            try:
                allowed_hosts.append(urllib.parse.urlparse(raw).netloc.lower())
            except Exception:
                pass

    host = parsed.netloc.lower()
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError("This API URL is not an approved Crystal Hub API host.")

    query = urllib.parse.parse_qs(parsed.query)
    api_key = query.get("key", [""])[0]
    expected = os.getenv("SCRIPT_API_KEY", "").strip()
    if expected and not secrets.compare_digest(api_key, expected):
        raise ValueError("Invalid script API key.")

    req = urllib.request.Request(
        api_url,
        headers={"Accept": "application/json", "User-Agent": "CrystalHubBot/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read(2_000_000)
    except Exception as exc:
        raise ValueError(f"Could not fetch script API: {type(exc).__name__}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Script API did not return valid JSON.") from exc

    if isinstance(data, dict) and isinstance(data.get("script"), dict):
        data = data["script"]
    if not isinstance(data, dict):
        raise ValueError("Script API response must be a JSON object.")

    name = str(data.get("name") or data.get("script_name") or "").strip()[:100]
    source = data.get("source_code", data.get("code", data.get("source", "")))
    if not name:
        raise ValueError("Script API response is missing the script name.")
    if not isinstance(source, str):
        raise ValueError("Script API response is missing source_code.")
    return {
        "name": name,
        "version": str(data.get("version") or "1.0.0")[:32],
        "source_code": source,
        "buyer_role_id": data.get("buyer_role_id"),
        "admin_role_id": data.get("admin_role_id"),
    }

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
        site_redeem_codes[key] = {"plan": plan, "duration_days": days, "label": label, "code": raw, "created_by": "website-admin", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "redeemed_by": None, "redeemed_at": None, "revoked": False}
        try:
            _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
        except OSError:
            site_redeem_codes.pop(key, None)
            self._send_text(500, json.dumps({"error": "Could not save the code. Check persistent storage on Render."})); return
        self._send_text(200, json.dumps({"ok": True, "code": raw, "plan": plan, "duration": "30 days" if days else "Lifetime", "label": label}))

    def _admin_manage_site_codes(self, action):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                self._send_text(400, json.dumps({"error": "Invalid request."})); return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict): raise ValueError("invalid body")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, json.dumps({"error": "Invalid request."})); return
        expected = os.getenv("ADMIN_ACCESS_CODE", "admi-on-top")
        supplied = str(payload.get("adminCode", ""))
        if not expected or not secrets.compare_digest(supplied, expected):
            self._send_text(403, json.dumps({"error": "Admin authorization failed."})); return
        if action == "list":
            rows = []
            for key, rec in site_redeem_codes.items():
                rows.append({"id": key, "code": rec.get("code", ""), "plan": rec.get("plan", "Unknown"), "label": rec.get("label", ""), "redeemed": bool(rec.get("redeemed_by")), "revoked": bool(rec.get("revoked")), "created_at": rec.get("created_at", "")})
            self._send_text(200, json.dumps({"ok": True, "codes": rows})); return
        key = str(payload.get("codeHash", ""))
        rec = site_redeem_codes.get(key)
        if not rec:
            self._send_text(404, json.dumps({"error": "Code not found."})); return
        if rec.get("redeemed_by"):
            self._send_text(409, json.dumps({"error": "Redeemed codes cannot be deleted."})); return
        rec["revoked"] = True
        try: _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
        except OSError:
            rec["revoked"] = False
            self._send_text(500, json.dumps({"error": "Could not save code changes."})); return
        self._send_text(200, json.dumps({"ok": True}))

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
        if admin_redeem_code and secrets.compare_digest(code.upper(), admin_redeem_code.strip().upper()):
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
        if parsed_path.path == "/api/admin/list-codes":
            self._admin_manage_site_codes("list")
            return
        if parsed_path.path == "/api/admin/delete-code":
            self._admin_manage_site_codes("delete")
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
            reported_device = str(payload.get("device", "PC")).strip()[:20] or "PC"
            if reported_device.lower() not in ("mobile", "pc"):
                reported_device = "PC"
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, "invalid request")
            return

        if not script_name or not user_key or not reported_hwid:
            self._send_text(400, "missing fields")
            return

        s_info = scripts_data.get(script_name)
        if not s_info:
            self._send_text(403, "denied: invalid key")
            return
        ok_key, reason = key_is_usable(s_info, user_key)
        if not ok_key:
            self._send_text(403, "denied: " + reason)
            return

        binding_key = f"{script_name}:{user_key}"
        existing_binding = hwid_bindings.get(binding_key)
        existing_hwid = existing_binding.get("hwid") if isinstance(existing_binding, dict) else existing_binding
        if existing_hwid is None:
            hwid_bindings[binding_key] = {"hwid": reported_hwid, "device": reported_device, "last_seen": _iso_now()}
            try:
                save_hwid_bindings()
            except OSError:
                hwid_bindings.pop(binding_key, None)
                self._send_text(500, "server storage error")
                return
        elif existing_hwid != reported_hwid:
            self._send_text(403, "you need to reset hwid")
            return
        else:
            hwid_bindings[binding_key] = {"hwid": existing_hwid, "device": reported_device, "last_seen": _iso_now()}
            try:
                save_hwid_bindings()
            except OSError:
                pass

        if parsed_path.path == "/script-source":
            # Deliver source only after key and saved device binding pass.
            self._send_text(200, s_info.get("source_code", "-- No source code loaded"))
        else:
            self._send_text(200, "ok")

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path_parts = parsed_path.path.strip("/").split("/")

        # Unique file IDs: /files/v4/lua/<file_id> (same protection model as /script/<name>)
        if len(path_parts) >= 4 and path_parts[0] == "files" and path_parts[1] == "v4" and path_parts[2] == "lua":
            file_id = path_parts[3].strip()
            script_name = None
            for name, info in scripts_data.items():
                if str(info.get("file_id") or "") == file_id:
                    script_name = name
                    break
            if not script_name:
                self._send_text(404, "File not found.")
                return
            # Rewrite to legacy /script/<name> handler path shape
            self.path = "/script/" + urllib.parse.quote(script_name) + (("?" + parsed_path.query) if parsed_path.query else "")
            parsed_path = urllib.parse.urlparse(self.path)
            path_parts = parsed_path.path.strip("/").split("/")

        if len(path_parts) >= 2 and path_parts[0] == "api" and path_parts[1] == "script-export":
            query = urllib.parse.parse_qs(parsed_path.query)
            if not _script_api_key_valid(query.get("key", [""])[0]):
                self._send_text(401, json.dumps({"error": "Invalid API key."}))
                return
            script_name = urllib.parse.unquote("/".join(path_parts[2:])).strip()
            if not script_name or script_name not in scripts_data:
                self._send_text(404, json.dumps({"error": "Script not found."}))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", os.getenv("SITE_ORIGIN", "*"))
            payload = json.dumps(_script_record_payload(script_name, scripts_data[script_name]), ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if len(path_parts) >= 2 and path_parts[0] == "script":
            # User-Agent is only a basic browser filter and can be spoofed.
            # The actual source is separately protected by key + server-side device binding.
            user_agent = (self.headers.get("User-Agent") or "").lower()
            allowed_agents = ("roblox", "delta", "real")
            if not any(agent in user_agent for agent in allowed_agents):
                # Friendly visual deny page for ordinary browsers. User-Agent checks are
                # only a basic filter and can be spoofed; actual access is enforced by
                # server-side script-key and device authorization in /script-source.
                deny_page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crystal Hub — Access denied</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:Arial,Helvetica,sans-serif}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:#777;color:#f10b16;text-align:center;padding:24px}
main{width:min(1100px,100%);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px}
.symbol{font-size:clamp(72px,14vw,150px);font-weight:900;line-height:1}
h1{margin:0;font-size:clamp(38px,8vw,100px);line-height:1.04;font-weight:1000;letter-spacing:-.035em;overflow-wrap:anywhere;text-shadow:0 2px 0 rgba(0,0,0,.06)}
p{margin:0;color:#2b1010;font-size:clamp(15px,2.5vw,22px);font-weight:700}
</style></head><body><main><div class="symbol" aria-hidden="true">⛔</div>
<h1>YOU ARE NOT ALLOWED<br>IN THIS PAGE</h1><p>Protected by Crystal Hub</p></main></body></html>"""
                payload = deny_page.encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
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
local UserInputService = game:GetService("UserInputService")
local device_type = UserInputService.TouchEnabled and "Mobile" or "PC"
local request_ok, response = pcall(function()
    return request_fn({{
        Url = "{app_url}/script-source",
        Method = "POST",
        Headers = {{ ["Content-Type"] = "application/json" }},
        Body = HttpService:JSONEncode({{
            script_name = {script_name_lua},
            key = user_key,
            hwid = current_hwid,
            device = device_type
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
bot = commands.Bot(command_prefix=commands.when_mentioned_or("!", "-"), intents=intents, allowed_mentions=allowed_mentions, case_insensitive=True)

def generate_random_key(prefix="MUk5", length=32):
    chars = string.ascii_letters + string.digits
    return prefix + "".join(random.choice(chars) for _ in range(length - len(prefix)))


# ---------- Duration / access helpers (PERM + TIME) ----------
_DURATION_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)

def parse_duration(text: str) -> int:
    """Parse duration strings like 1m, 30m, 1h, 12h, 1d, 7d into seconds.
    1m = 1 minute (not month). Raises ValueError on invalid input.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        raise ValueError("Duration is required when using TIME.")
    m = _DURATION_RE.fullmatch(raw)
    if not m:
        raise ValueError("Invalid duration. Use formats like 1m, 30m, 1h, 12h, 1d, 7d.")
    amount = int(m.group(1))
    unit = m.group(2).lower()
    if amount < 1:
        raise ValueError("Duration amount must be at least 1.")
    if unit == "m":
        seconds = amount * 60
    elif unit == "h":
        seconds = amount * 3600
    else:
        seconds = amount * 86400
    if seconds > 3650 * 86400:
        raise ValueError("Duration is too long (max ~3650 days).")
    return seconds


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _lua_long_string(value: str) -> str:
    """Return a Lua long-bracket string whose closing delimiter cannot occur in value."""
    level = 0
    while True:
        equals = "=" * level
        closing = "]" + equals + "]"
        if closing not in value:
            return "[" + equals + "[" + value + closing
        level += 1


def add_key_gate_before_obfuscation(source: str, script_name: str) -> str:
    """Wrap source with Crystal Hub key/HWID validation before the payload runs."""
    app_url = os.getenv("RENDER_EXTERNAL_URL", "https://discord-bot-3u9m.onrender.com").rstrip("/")
    script_name_lua = json.dumps(str(script_name))
    payload_literal = _lua_long_string(source)
    return f"""-- Crystal Hub key gate (inserted before obfuscation)
local _Players = game:GetService("Players")
local _player = _Players.LocalPlayer
local _key = rawget(_G, "script_key") or script_key
if type(_key) ~= "string" or _key == "" then
    if _player then _player:Kick("key not found") end
    return
end
local _okId, _hwid = pcall(function()
    return game:GetService("RbxAnalyticsService"):GetClientId()
end)
if not _okId or type(_hwid) ~= "string" or _hwid == "" then
    if _player then _player:Kick("HWID check failed. Please retry.") end
    return
end
local _request = (syn and syn.request) or http_request or request
if type(_request) ~= "function" then
    if _player then _player:Kick("Key check unavailable") end
    return
end
local _HttpService = game:GetService("HttpService")
local _Input = game:GetService("UserInputService")
local _device = _Input.TouchEnabled and "Mobile" or "PC"
local _reqOk, _response = pcall(function()
    return _request({{
        Url = "{app_url}/hwid/check",
        Method = "POST",
        Headers = {{["Content-Type"] = "application/json"}},
        Body = _HttpService:JSONEncode({{
            script_name = {script_name_lua},
            key = _key,
            hwid = _hwid,
            device = _device
        }})
    }})
end)
if not _reqOk or type(_response) ~= "table" then
    if _player then _player:Kick("Key authorization failed") end
    return
end
local _status = tonumber(_response.StatusCode or _response.Status or 0)
local _body = tostring(_response.Body or "")
if _status ~= 200 then
    if _player then
        if string.find(string.lower(_body), "you need to reset hwid", 1, true) then
            _player:Kick("you need to reset hwid")
        elseif string.find(string.lower(_body), "invalid key", 1, true) then
            _player:Kick("key not found")
        else
            _player:Kick("Key authorization failed")
        end
    end
    return
end
local _payload = {payload_literal}
local _loader = loadstring or load
if type(_loader) ~= "function" then error("loadstring unavailable") end
local _compiled, _compileError = _loader(_payload)
if type(_compiled) ~= "function" then error("source compile failed: " .. tostring(_compileError)) end
_compiled()
"""


async def obfuscate_lua_source(source: str, script_name: str = "") -> str:
    """Add Crystal Hub's key gate, then use the site's browser-only Lua obfuscator."""
    source = str(source or "")
    if not source.strip():
        raise ValueError("The uploaded Lua source is empty.")
    if not str(script_name).strip():
        raise ValueError("Script name is required to add the key gate before obfuscation.")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is missing. Add 'playwright' to requirements.txt and install Chromium "
            "in the Render build command before using this obfuscator."
        ) from exc

    # The selected site processes code in the browser and does not advertise a public API.
    # Add Crystal Hub's key/HWID gate BEFORE putting the source into the browser tool.
    prepared_source = add_key_gate_before_obfuscation(source, script_name)
    tool_url = "https://obfuscatortools.com/tools/lua-obfuscator.php"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.goto(tool_url, wait_until="domcontentloaded", timeout=60000)
            await page.locator("textarea").first.wait_for(state="visible", timeout=30000)

            # The site has separate source and output text areas. Fail clearly if its UI changes.
            textareas = page.locator("textarea")
            count = await textareas.count()
            if count < 2:
                raise RuntimeError(
                    "The obfuscator page layout changed (expected source and output text areas); "
                    "no result was saved."
                )

            # Ensure the site's own optional banner is disabled; Crystal Hub adds its own label.
            try:
                header_label = page.get_by_text("Add Protection Header", exact=False).first
                if await header_label.count():
                    checkbox = header_label.locator("input[type=checkbox]")
                    if await checkbox.count() and await checkbox.is_checked():
                        await checkbox.uncheck()
            except Exception:
                # The checkbox may be wrapped differently; the output is still branded below.
                pass

            await textareas.nth(0).fill(prepared_source, timeout=30000)
            button = page.get_by_role("button", name=re.compile(r"Obfuscate Lua Script", re.I))
            if await button.count() == 0:
                button = page.get_by_text("Obfuscate Lua Script", exact=False).last
            await button.click(timeout=30000)

            # Wait until the output field contains transformed source, not the input itself.
            await page.wait_for_function(
                """(original) => {
                    const fields = document.querySelectorAll('textarea');
                    return fields.length >= 2 && fields[1].value.trim().length > 0 &&
                           fields[1].value.trim() !== original.trim();
                }""",
                arg=prepared_source,
                timeout=90000,
            )
            output = (await textareas.nth(1).input_value()).strip()
            if not output or output == prepared_source.strip():
                raise RuntimeError("The website did not return transformed Lua source.")

            # Remove an optional third-party protection banner if the tool inserted one.
            output = re.sub(
                r"^\s*--[^\n]*(?:obfuscatortools|lua obfuscator)[^\n]*\n",
                "",
                output,
                count=1,
                flags=re.IGNORECASE,
            )
            return "--[[ Protected by Crystal Hub ]]\n" + output.lstrip()
        finally:
            await browser.close()


def _iso_now():
    return _utcnow().isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def is_expired(expires_at) -> bool:
    dt = _parse_iso(expires_at)
    if dt is None:
        return False
    return dt <= _utcnow()


def format_remaining(expires_at) -> str:
    dt = _parse_iso(expires_at)
    if dt is None:
        return "permanent"
    remaining = int((dt - _utcnow()).total_seconds())
    if remaining <= 0:
        return "expired"
    return format_duration(remaining)


def resolve_expires(mode: str, duration_text: Optional[str]):
    mode = (mode or "PERM").upper()
    if mode == "PERM":
        return "PERM", None
    if mode != "TIME":
        raise ValueError("Mode must be PERM or TIME.")
    seconds = parse_duration(duration_text)
    expires = (_utcnow() + datetime.timedelta(seconds=seconds)).isoformat()
    return "TIME", expires


def ensure_file_id(script_name: str, s_info: dict) -> str:
    fid = s_info.get("file_id")
    if isinstance(fid, str) and len(fid) >= 16:
        return fid
    used = {str(v.get("file_id") or "") for v in scripts_data.values()}
    for _ in range(40):
        candidate = secrets.token_hex(16)
        if candidate not in used:
            s_info["file_id"] = candidate
            return candidate
    # Extremely unlikely fallback
    s_info["file_id"] = secrets.token_hex(24)
    return s_info["file_id"]


def file_public_url(file_id: str) -> str:
    base = os.getenv("FILE_BASE_URL", os.getenv("RENDER_EXTERNAL_URL", "https://discord-bot-3u9m.onrender.com").rstrip("/") + "/files/v4/lua").rstrip("/")
    return f"{base}/{file_id}"


def trail_is_active(s_info: dict) -> bool:
    if not s_info.get("trail_enabled"):
        return False
    if (s_info.get("trail_mode") or "PERM").upper() == "TIME":
        if is_expired(s_info.get("trail_expires_at")):
            s_info["trail_enabled"] = False
            return False
    return True


def key_is_usable(s_info: dict, key: str) -> tuple:
    """Return (ok, reason). Rejects disabled or expired keys."""
    key = str(key or "")
    if not key:
        return False, "Key is empty."
    meta = (s_info.get("key_meta") or {}).get(key, {})
    if meta.get("disabled"):
        return False, "This key has been disabled."
    if key not in s_info.get("valid_keys", set()) and key not in set((s_info.get("user_keys") or {}).values()):
        return False, "Key is not valid for this script."
    if (meta.get("mode") or "PERM").upper() == "TIME" and is_expired(meta.get("expires_at")):
        return False, "This key has expired."
    return True, ""


def user_access_ok(s_info: dict, user_id: int) -> tuple:
    """Check whitelist / key access including TIME expiry. Returns (ok, reason, key)."""
    uid = int(user_id)
    access = (s_info.get("user_access") or {}).get(uid)
    key = (s_info.get("user_keys") or {}).get(uid)
    if access and isinstance(access, dict):
        if (access.get("mode") or "PERM").upper() == "TIME" and is_expired(access.get("expires_at")):
            # Drop expired access
            s_info.get("whitelisted_users", set()).discard(uid)
            s_info.get("user_access", {}).pop(uid, None)
            if key:
                s_info.get("user_keys", {}).pop(uid, None)
            return False, "Your access has expired. Redeem a key or ask an admin.", None
        key = access.get("key") or key
        if key:
            ok, reason = key_is_usable(s_info, key)
            if not ok:
                return False, reason, key
        return True, "", key
    if uid in s_info.get("whitelisted_users", set()) or uid in s_info.get("user_keys", {}):
        if key:
            ok, reason = key_is_usable(s_info, key)
            if not ok:
                return False, reason, key
        return True, "", key
    return False, "Access Denied! You must be whitelisted by an admin or redeem a valid key first.", None


def register_key(
    s_info: dict,
    key: str,
    mode: str = "PERM",
    expires_at: Optional[str] = None,
    created_by: Optional[int] = None,
):
    s_info.setdefault("valid_keys", set()).add(key)
    meta = s_info.setdefault("key_meta", {})
    meta[key] = {
        "mode": mode,
        "expires_at": expires_at,
        "disabled": False,
        "created_at": _iso_now(),
        "created_by": int(created_by) if created_by is not None else None,
        "kind": "key",
    }


def grant_user_access(
    s_info: dict,
    user_id: int,
    key: str,
    mode: str = "PERM",
    expires_at: Optional[str] = None,
    created_by: Optional[int] = None,
):
    uid = int(user_id)
    s_info.setdefault("whitelisted_users", set()).add(uid)
    s_info.setdefault("user_keys", {})[uid] = key
    s_info.setdefault("user_access", {})[uid] = {
        "key": key,
        "mode": mode,
        "expires_at": expires_at,
        "granted_at": _iso_now(),
    }
    register_key(s_info, key, mode, expires_at, created_by=created_by)


async def script_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    choices = []
    for script_name in scripts_data.keys():
        if current.lower() in script_name.lower():
            choices.append(app_commands.Choice(name=script_name, value=script_name))
    return choices[:25]

# Only this Discord account may generate Premium/Ultra/Admin plan codes.
CODE_CREATOR_USERNAMES = {"admi111"}


def _user_name_matches(user, allowed_names) -> bool:
    names = {
        str(getattr(user, "name", "") or "").lower(),
        str(getattr(user, "global_name", "") or "").lower(),
        str(getattr(user, "display_name", "") or "").lower(),
    }
    allowed = {n.lower() for n in allowed_names}
    return bool(names & allowed)


def is_code_creator(user) -> bool:
    """Plan code creation (/redeem create_plan, -createcode) — only admi111 (or BOT_OWNER_ID)."""
    if is_bot_owner(getattr(user, "id", 0)):
        return True
    return _user_name_matches(user, CODE_CREATOR_USERNAMES)


def has_admin_editor_role(interaction: discord.Interaction) -> bool:
    """Server rank named like 'Admin Editor' can manage whitelist/keys/trail/panels."""
    member = interaction.user
    if not interaction.guild or not isinstance(member, discord.Member):
        return False
    for role in member.roles:
        name = " ".join(str(role.name).lower().split())
        compact = name.replace(" ", "").replace("-", "").replace("_", "")
        if "admin editor" in name or compact in ("admineditor", "editoradmin"):
            return True
    return False


def is_guild_administrator(interaction: discord.Interaction) -> bool:
    """Discord Administrator permission (typical server owner / full admin)."""
    try:
        return bool(interaction.user.guild_permissions.administrator)
    except AttributeError:
        return False


def is_admin_or_has_role(interaction: discord.Interaction, script_name: str = None) -> bool:
    """All script management uses the server-wide admin role or Discord Administrator."""
    return _has_global_admin_access(interaction)

# Prevent blacklisted users from invoking any slash command, including if they own a server.
async def _global_blacklist_interaction_check(interaction: discord.Interaction) -> bool:
    if interaction.user and interaction.user.id in blacklisted_users:
        message = "🚫 You are globally blacklisted from using this bot."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    # Gate bot-management slash commands with the server-wide role. Keep user-facing
    # commands (such as /my-plan and /redeem) available; quota checks remain in place.
    command_name = getattr(getattr(interaction, "command", None), "name", "")
    management_commands = {
        "generate-key", "key", "whitelist", "whilst", "see-keys", "delete-key",
        "unwhitelist", "trail", "create-script", "delete-script", "source-setup",
        "set-buyer-role", "panel-setup", "update-script", "reset-hwid-cooldown",
        "ai-question-channel", "ai-add-qa", "ai-qa-list", "ai-bot",
        "blacklist", "unblacklist", "list-servers", "send-server-message",
        "create-redeem-code", "list-redeem-codes", "revoke-redeem-code",
    }
    if command_name in management_commands:
        allowed, message = _global_admin_status(interaction)
        if not allowed or not _has_global_admin_access(interaction):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
                else:
                    await interaction.response.send_message(message or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
    return True

bot.tree.interaction_check = _global_blacklist_interaction_check


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
        _quota_increment(interaction.user.id, "sources")
        await interaction.response.send_message(f"⚡ Source file saved & protected for `{self.script_name}`!", ephemeral=True)

    @discord.ui.button(label="No ❌", style=discord.ButtonStyle.danger)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        scripts_data[self.script_name]["source_code"] = self.file_content
        save_data()
        _quota_increment(interaction.user.id, "sources")
        await interaction.response.send_message(f"✅ Source file attached for `{self.script_name}`!", ephemeral=True)


class AddSourceModal(discord.ui.Modal, title="Crystal Hub — Add Source"):
    source = discord.ui.TextInput(
        label="Paste source code",
        placeholder="Paste the script source here",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000
    )

    def __init__(self, script_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.script_name = script_name
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the person who created this script can add its source from this button.", ephemeral=True)
            return
        if self.script_name not in scripts_data:
            await interaction.response.send_message("❌ Script not found.", ephemeral=True)
            return
        scripts_data[self.script_name]["source_code"] = str(self.source.value)
        save_data()
        await interaction.response.send_message(f"✅ Source added to `{self.script_name}`.", ephemeral=True)


class CreateScriptActionsView(discord.ui.View):
    def __init__(self, script_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.script_name = script_name
        self.owner_id = owner_id

    @discord.ui.button(label="Add Source (Optional)", style=discord.ButtonStyle.primary, emoji="📄")
    async def add_source(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the script creator can use this button.", ephemeral=True)
            return
        if self.script_name not in scripts_data:
            await interaction.response.send_message("❌ Script not found.", ephemeral=True)
            return
        await interaction.response.send_modal(AddSourceModal(self.script_name, self.owner_id))


class ScriptRedeemModal(discord.ui.Modal, title="Crystal Hub — Redeem a Key"):
    code = discord.ui.TextInput(
        label="Enter script key below",
        placeholder="Paste your Crystal Hub key",
        required=True,
        max_length=120
    )

    def __init__(self, script_name: str):
        super().__init__(timeout=180)
        self.script_name = script_name

    async def on_submit(self, interaction: discord.Interaction):
        supplied = str(self.code.value or "").strip()

        # Special redeem-only code: remove this user from the global blacklist.
        if supplied == "you-are-gay":
            uid = interaction.user.id
            if uid not in blacklisted_users:
                await interaction.response.send_message(
                    "ℹ️ You are not on the global blacklist.",
                    ephemeral=True
                )
                return
            blacklisted_users.discard(uid)
            save_data()
            await interaction.response.send_message(
                "✅ Global blacklist removed from your account.",
                ephemeral=True
            )
            return

        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("❌ Script not found.", ephemeral=True)
            return

        if supplied in s_info.get("valid_keys", set()) or supplied in set((s_info.get("user_keys") or {}).values()):
            uid = interaction.user.id
            ok, reason = key_is_usable(s_info, supplied)
            if not ok:
                await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
                return
            owner = next((int(k) for k, v in s_info.get("user_keys", {}).items()
                          if v == supplied and int(k) != uid), None)
            if owner is not None:
                await interaction.response.send_message("❌ This key is already linked to another Discord account.", ephemeral=True)
                return
            meta = (s_info.get("key_meta") or {}).get(supplied, {})
            mode = (meta.get("mode") or "PERM").upper()
            expires_at = meta.get("expires_at")
            grant_user_access(s_info, uid, supplied, mode, expires_at)
            save_data()
            extra = ""
            if mode == "TIME":
                extra = f"\nAccess: TIME — remaining {format_remaining(expires_at)}"
            else:
                extra = "\nAccess: PERM"
            await interaction.response.send_message(
                f"✅ Key redeemed successfully for **{self.script_name}** — Crystal Hub.{extra}",
                ephemeral=True
            )
            return

        # Premium/Ultra codes are also redeemable through this Crystal Hub modal.
        code_hash = _hash_redeem_code(supplied)
        rec = discord_plan_codes.get(code_hash)
        if not rec or rec.get("redeemed_by"):
            await interaction.response.send_message("❌ Invalid or already-used Crystal Hub code.", ephemeral=True)
            return
        plan = str(rec.get("plan", "Premium"))
        days = int(rec.get("duration_days", 30))
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = None if days <= 0 else (now + datetime.timedelta(days=days)).isoformat()
        old_rec = dict(rec)
        rec["redeemed_by"] = str(interaction.user.id)
        rec["redeemed_at"] = now.isoformat()
        discord_plan_entitlements[str(interaction.user.id)] = {
            "plan": plan, "redeemed_at": now.isoformat(), "expires_at": expires_at
        }
        try:
            _save_json_dict(DISCORD_PLAN_CODES_FILE, discord_plan_codes)
            _save_json_dict(DISCORD_PLAN_ENTITLEMENTS_FILE, discord_plan_entitlements)
        except OSError:
            discord_plan_codes[code_hash] = old_rec
            await interaction.response.send_message("❌ Could not save the redemption. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Crystal Hub **{plan}** activated ({'Lifetime' if days <= 0 else str(days) + ' days'}).",
            ephemeral=True
        )


class ViewMobileResponseView(discord.ui.View):
    """Shown under the normal script message; Mobile is available only from this message."""
    def __init__(self, script_name: str, user_key: str):
        super().__init__(timeout=300)
        self.script_name = script_name
        self.user_key = user_key

    @discord.ui.button(label="View Mobile 📱", style=discord.ButtonStyle.secondary)
    async def view_mobile_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("❌ Script details not found.", ephemeral=True)
            return

        ok, reason, current_key = user_access_ok(s_info, interaction.user.id)
        if not ok or not current_key:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return

        # Keep the same script URL/key format, but expose it as the mobile option.
        file_id = ensure_file_id(self.script_name, s_info)
        file_url = file_public_url(file_id)
        mobile = (
            f'Here is your mobile script:\n'
            f'``script_key="{current_key}"; loadstring(game:HttpGet("{file_url}"))()``'
        )
        await interaction.response.send_message(mobile, ephemeral=True)


class PanelControlView(discord.ui.View):
    def __init__(self, script_name: str):
        super().__init__(timeout=None)
        self.script_name = script_name

    def _script_message(self, s_info: dict, user_key: str):
        file_id = ensure_file_id(self.script_name, s_info)
        file_url = file_public_url(file_id)
        return (
            f'Here is your script:\n'
            f'```lua\n'
            f'script_key="{user_key}";\n'
            f'loadstring(game:HttpGet("{file_url}"))()\n'
            f'```'
        )

    def _get_user_key(self, s_info: dict, user_id: int):
        ok, reason, user_key = user_access_ok(s_info, user_id)
        if not ok:
            return None, reason
        if not user_key:
            user_key = generate_random_key()
            grant_user_access(s_info, user_id, user_key, "PERM", None)
        ensure_file_id(self.script_name, s_info)
        save_data()
        return user_key, ""

    @discord.ui.button(label="📜 Get Script", style=discord.ButtonStyle.primary, row=1)
    async def view_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("❌ Script details not found.", ephemeral=True)
            return

        user_key, reason = self._get_user_key(s_info, interaction.user.id)
        if not user_key:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return

        code = self._script_message(s_info, user_key)
        # The Mobile button is attached to THIS script message, not the main panel.
        await interaction.response.send_message(
            code,
            view=ViewMobileResponseView(self.script_name, user_key),
            ephemeral=True
        )

    @discord.ui.button(label="📊 Get Stats", style=discord.ButtonStyle.secondary, row=3)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Stats is the original button, now updated to show the user's current state.
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("❌ Script details not found.", ephemeral=True)
            return

        ok, reason, user_key = user_access_ok(s_info, interaction.user.id)
        if not ok or not user_key:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return

        meta = (s_info.get("key_meta") or {}).get(user_key, {})
        access = (s_info.get("user_access") or {}).get(interaction.user.id, {})
        mode = str(access.get("mode") or meta.get("mode") or "PERM").upper()
        expires_at = access.get("expires_at") or meta.get("expires_at")
        duration = format_remaining(expires_at) if mode == "TIME" else "Lifetime"

        binding = hwid_bindings.get(f"{self.script_name}:{user_key}")
        if isinstance(binding, dict):
            device = str(binding.get("device") or "Unknown")
            last_seen = binding.get("last_seen")
        else:
            device = "Unknown" if binding is None else "PC"
            last_seen = None

        last_seen_text = "Never"
        if last_seen:
            dt = _parse_iso(last_seen)
            if dt:
                last_seen_text = dt.strftime("%Y-%m-%d %H:%M UTC")

        embed = discord.Embed(
            title="📊 Script Stats",
            description=(
                f"**Script:** `{self.script_name}`\n"
                f"**Key:** `{user_key}`\n"
                f"**Access:** `{mode}`\n"
                f"**Duration:** `{duration}`\n"
                f"**Last Device:** `{device}`\n"
                f"**Last Login:** `{last_seen_text}`"
            ),
            color=0xd4af37
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="⚙️ Reset HWID", style=discord.ButtonStyle.secondary, row=2)
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        user_id = interaction.user.id
        if not s_info or user_id not in s_info.get("user_keys", {}):
            await interaction.response.send_message("❌ You don't have an active key for this script.", ephemeral=True)
            return
        cooldown = int(security_state.get("reset_hwid_cooldown", DEFAULT_RESET_HWID_COOLDOWN))
        cooldown_key = f"{interaction.guild_id}:{self.script_name}:{user_id}"
        now = int(time.time())
        last_used = int(security_state.get("reset_hwid_last_used", {}).get(cooldown_key, 0))
        remaining = cooldown - (now - last_used)
        if remaining > 0:
            await interaction.response.send_message(
                f"⏳ You must wait **{format_duration(remaining)}** before resetting HWID again.",
                ephemeral=True
            )
            return
        user_key = s_info["user_keys"][user_id]
        hwid_bindings.pop(f"{self.script_name}:{user_key}", None)
        save_hwid_bindings()
        security_state.setdefault("reset_hwid_last_used", {})[cooldown_key] = now
        save_security_state()
        await interaction.response.send_message("✅ HWID reset successfully.", ephemeral=True)

    @discord.ui.button(label="🔑 Redeem Key", style=discord.ButtonStyle.success, row=0)
    async def redeem_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScriptRedeemModal(self.script_name))

    @discord.ui.button(label="👤 Get Role", style=discord.ButtonStyle.primary, row=1)
    async def get_buyer_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info or not s_info.get("buyer_role_id"):
            await interaction.response.send_message("❌ Buyer role is not configured for this script.", ephemeral=True)
            return
        ok, reason, _key = user_access_ok(s_info, interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ Access Denied! {reason}", ephemeral=True)
            return
        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role:
            if role in interaction.user.roles:
                await interaction.response.send_message("✅ You already have the buyer role!", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"🎉 Granted you the {role.mention} buyer role!", ephemeral=True)


# ==================== Commands ==================== #

@bot.event
async def on_ready():
    # Remove old guild-specific copies first; registering the same commands
    # globally and per-guild made Discord show duplicate slash commands.
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Cleared old guild-specific slash commands for {guild.id} ({guild.name}).")
        except Exception as e:
            print(f"Could not clear old guild commands for {guild.id}: {type(e).__name__}: {e}")

    # Register commands globally only, so each command is listed once.
    try:
        global_synced = await bot.tree.sync()
        print(f"Synced {len(global_synced)} global slash commands successfully.")
    except Exception as e:
        print(f"Error syncing global slash commands: {type(e).__name__}: {e}")

    print(f"Bot connected successfully as: {bot.user}")


# 1. Generate key (/generate-key and /key)
@bot.tree.command(name="generate-key", description="Generate a single-use key for a script")
@app_commands.describe(script="Target script", duration_type="PERM = permanent, TIME = timed", duration="Required for TIME (e.g. 1m, 1h, 1d)")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.choices(duration_type=[
    app_commands.Choice(name="PERM (permanent)", value="PERM"),
    app_commands.Choice(name="TIME (timed)", value="TIME"),
])
async def generate_key_command(
    interaction: discord.Interaction,
    script: str,
    duration_type: app_commands.Choice[str] = None,
    duration: str = None,
):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You need the **Admin Editor** role (or script admin role) to generate keys.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    mode = (duration_type.value if duration_type else "PERM").upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return
    new_key = generate_random_key()
    register_key(scripts_data[script], new_key, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _quota_increment(interaction.user.id, "keys")
    when = "permanent" if mode == "PERM" else f"expires in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"🔑 **New Key Generated for `{script}`** ({mode} — {when}):\n```text\n{new_key}\n```",
        ephemeral=True
    )


@bot.tree.command(name="key", description="Generate a script key (PERM or TIME)")
@app_commands.describe(script="Target script", duration_type="PERM = permanent, TIME = timed", duration="Required for TIME (e.g. 1m, 30m, 1h, 1d)")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.choices(duration_type=[
    app_commands.Choice(name="PERM (permanent)", value="PERM"),
    app_commands.Choice(name="TIME (timed)", value="TIME"),
])
async def key_command(
    interaction: discord.Interaction,
    script: str,
    duration_type: app_commands.Choice[str],
    duration: str = None,
):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You need the **Admin Editor** role (or script admin role) to generate keys.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    mode = duration_type.value.upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return
    new_key = generate_random_key()
    register_key(scripts_data[script], new_key, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _quota_increment(interaction.user.id, "keys")
    when = "permanent" if mode == "PERM" else f"expires in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"🔑 **New Key Generated for `{script}`** ({mode} — {when}):\n```text\n{new_key}\n```",
        ephemeral=True
    )


# 2. Whitelist (/whitelist and /whilst)
@bot.tree.command(name="whitelist", description="Whitelist a user for a script and notify them via DM")
@app_commands.describe(user="The member to whitelist", script="Target script", duration_type="PERM = permanent, TIME = timed", duration="Required for TIME (e.g. 1m, 1h, 1d)")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.choices(duration_type=[
    app_commands.Choice(name="PERM (permanent)", value="PERM"),
    app_commands.Choice(name="TIME (timed)", value="TIME"),
])
async def whitelist_command(
    interaction: discord.Interaction,
    user: discord.Member,
    script: str,
    duration_type: app_commands.Choice[str] = None,
    duration: str = None,
):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You need the **Admin Editor** role (or script admin role) to whitelist users.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    mode = (duration_type.value if duration_type else "PERM").upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return
    s_info = scripts_data[script]
    new_key = generate_random_key()
    grant_user_access(s_info, user.id, new_key, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _quota_increment(interaction.user.id, "keys")
    if s_info.get("buyer_role_id"):
        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role and role not in user.roles:
            try:
                await user.add_roles(role)
            except Exception:
                pass
    when = "permanent" if mode == "PERM" else f"TIME — {format_remaining(expires_at)} remaining"
    try:
        dm_embed = discord.Embed(
            title="🎉 Whitelist Granted!",
            description=f"You have been whitelisted in **{interaction.guild.name}** for script: **{script}**!\n\nYour Key: `{new_key}`\nAccess: {when}",
            color=0x00ff00
        )
        await user.send(embed=dm_embed)
        dm_status = "DM sent successfully."
    except Exception:
        dm_status = "Could not send DM (user DMs closed)."
    await interaction.response.send_message(f"✅ {user.mention} has been whitelisted for `{script}` ({when})! ({dm_status})", ephemeral=True)


@bot.tree.command(name="whilst", description="Whitelist a user with PERM or TIME access")
@app_commands.describe(user="The member to whitelist", script="Target script", duration_type="PERM = permanent, TIME = timed", duration="Required for TIME (e.g. 1m, 30m, 1h, 7d)")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.choices(duration_type=[
    app_commands.Choice(name="PERM (permanent)", value="PERM"),
    app_commands.Choice(name="TIME (timed)", value="TIME"),
])
async def whilst_command(
    interaction: discord.Interaction,
    user: discord.Member,
    script: str,
    duration_type: app_commands.Choice[str],
    duration: str = None,
):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You need the **Admin Editor** role (or script admin role) to whitelist users.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    mode = duration_type.value.upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return
    s_info = scripts_data[script]
    new_key_val = generate_random_key()
    grant_user_access(s_info, user.id, new_key_val, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _quota_increment(interaction.user.id, "keys")
    if s_info.get("buyer_role_id"):
        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role and role not in user.roles:
            try:
                await user.add_roles(role)
            except Exception:
                pass
    when = "permanent" if mode == "PERM" else f"TIME — {format_remaining(expires_at)} remaining"
    try:
        dm_embed = discord.Embed(
            title="🎉 Whitelist Granted!",
            description=f"You have been whitelisted in **{interaction.guild.name}** for script: **{script}**!\n\nYour Key: `{new_key_val}`\nAccess: {when}",
            color=0x00ff00
        )
        await user.send(embed=dm_embed)
        dm_status = "DM sent successfully."
    except Exception:
        dm_status = "Could not send DM (user DMs closed)."
    await interaction.response.send_message(f"✅ {user.mention} has been whitelisted for `{script}` ({when})! ({dm_status})", ephemeral=True)



@bot.tree.command(name="see-keys", description="Show your active keys and whitelist grants")
@app_commands.describe(script="Optional script to filter by")
@app_commands.autocomplete(script=script_autocomplete)
async def see_keys_command(interaction: discord.Interaction, script: str = None):
    if script and script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return

    # Only show keys created by this user, grouped by the whitelisted person.
    # Unassigned/generated keys are shown separately.
    grouped = {}
    unassigned = []
    total = 0

    selected = {script: scripts_data[script]} if script else scripts_data
    for script_name, s_info in selected.items():
        meta_map = s_info.get("key_meta") or {}
        valid = s_info.get("valid_keys", set())
        user_keys = s_info.get("user_keys", {}) or {}

        for key in list(valid):
            meta = meta_map.get(key, {})
            if not isinstance(meta, dict):
                meta = {}
            if str(meta.get("created_by", "")) != str(interaction.user.id):
                continue
            if meta.get("disabled"):
                continue

            expires_at = meta.get("expires_at")
            if expires_at:
                try:
                    if datetime.datetime.fromisoformat(expires_at) <= datetime.datetime.now(datetime.timezone.utc):
                        continue
                except (ValueError, TypeError):
                    pass

            total += 1
            owner_id = None
            for uid, user_key in user_keys.items():
                if user_key == key:
                    owner_id = int(uid)
                    break

            mode = str(meta.get("mode", "PERM"))
            when = "PERM"
            if expires_at:
                when = f"expires {format_remaining(expires_at)}"

            entry = f"**{script_name}** — `{key}` ({mode}, {when})"
            if owner_id is not None:
                grouped.setdefault(owner_id, []).append(entry)
            else:
                unassigned.append(entry)

    if total == 0:
        msg = "🔑 You currently have no active keys or whitelist grants."
    else:
        chunks = [f"🔑 **Your Keys / Whitelist ({total})**"]
        for uid, entries in grouped.items():
            chunks.append(f"\n👤 <@{uid}>")
            chunks.extend(f"  {entry}" for entry in entries)
        if unassigned:
            chunks.append("\n📌 **Generated Keys**")
            chunks.extend(f"  {entry}" for entry in unassigned)
        chunks.append("\nUse `/delete-key` to delete any key.")
        chunks.append("Use `/unwhitelist` to remove a whitelist.")
        msg = "\n".join(chunks)

    # Discord message limit protection.
    if len(msg) > 1900:
        msg = msg[:1870] + "\n…\nUse `/see-keys script:<name>` to filter."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="delete-key", description="Delete one of your active keys")
@app_commands.describe(key="The exact key to delete")
async def delete_key_command(interaction: discord.Interaction, key: str):
    key = key.strip()
    found = False
    for script_name, s_info in scripts_data.items():
        meta_map = s_info.get("key_meta") or {}
        meta = meta_map.get(key)
        if not isinstance(meta, dict):
            continue
        if str(meta.get("created_by", "")) != str(interaction.user.id) and not is_admin_or_has_role(interaction, script_name):
            continue

        if key not in s_info.get("valid_keys", set()):
            continue

        s_info["valid_keys"].discard(key)
        s_info.setdefault("key_meta", {}).pop(key, None)

        # Remove any whitelist/access entry tied to this key.
        for uid, user_key in list(s_info.get("user_keys", {}).items()):
            if user_key == key:
                s_info["user_keys"].pop(uid, None)
                s_info.get("whitelisted_users", set()).discard(uid)
                s_info.get("user_access", {}).pop(uid, None)

        hwid_bindings.pop(key, None)
        found = True
        save_data()
        save_hwid_bindings()
        await interaction.response.send_message(
            f"🗑️ Key `{key}` was deleted from `{script_name}`. The key/whitelist slot is free again.",
            ephemeral=True
        )
        return

    if not found:
        await interaction.response.send_message(
            "❌ Key not found, or you do not have permission to delete it.",
            ephemeral=True
        )


@bot.tree.command(name="unwhitelist", description="Remove a user from a script whitelist")
@app_commands.describe(user="The whitelisted member", script="Target script")
@app_commands.autocomplete(script=script_autocomplete)
async def unwhitelist_command(
    interaction: discord.Interaction,
    user: discord.Member,
    script: str,
):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message(
            "❌ You need the **Admin Editor** role (or script admin role) to remove whitelist access.",
            ephemeral=True
        )
        return

    s_info = scripts_data[script]
    uid = int(user.id)
    old_key = s_info.get("user_keys", {}).get(uid)
    if uid not in s_info.get("whitelisted_users", set()) and not old_key:
        await interaction.response.send_message(
            f"❌ {user.mention} is not whitelisted for `{script}`.",
            ephemeral=True
        )
        return

    s_info.get("whitelisted_users", set()).discard(uid)
    s_info.get("user_keys", {}).pop(uid, None)
    s_info.get("user_access", {}).pop(uid, None)

    # A whitelist grant owns its generated key, so remove that key too and free the shared slot.
    if old_key:
        s_info.get("valid_keys", set()).discard(old_key)
        s_info.get("key_meta", {}).pop(old_key, None)
        hwid_bindings.pop(old_key, None)

    save_data()
    save_hwid_bindings()
    await interaction.response.send_message(
        f"✅ Removed {user.mention} from the whitelist for `{script}`."
        + (" Their whitelist key was deleted too." if old_key else ""),
        ephemeral=True
    )


@bot.tree.command(name="trail", description="Enable or disable free Trail access for a script")
@app_commands.describe(script="Target script", state="ON or OFF", duration_type="PERM or TIME (used when enabling)", duration="Required for TIME (e.g. 1m, 1h, 1d)")
@app_commands.autocomplete(script=script_autocomplete)
@app_commands.choices(
    state=[
        app_commands.Choice(name="ON", value="ON"),
        app_commands.Choice(name="OFF", value="OFF"),
    ],
    duration_type=[
        app_commands.Choice(name="PERM (permanent)", value="PERM"),
        app_commands.Choice(name="TIME (timed)", value="TIME"),
    ],
)
async def trail_command(
    interaction: discord.Interaction,
    script: str,
    state: app_commands.Choice[str],
    duration_type: app_commands.Choice[str] = None,
    duration: str = None,
):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You need the **Admin Editor** role (or script admin role) to manage Trail.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    s_info = scripts_data[script]
    if state.value == "OFF":
        s_info["trail_enabled"] = False
        s_info["trail_mode"] = "PERM"
        s_info["trail_expires_at"] = None
        save_data()
        await interaction.response.send_message(f"✅ Trail **OFF** for `{script}`.", ephemeral=True)
        return
    mode = (duration_type.value if duration_type else "PERM").upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    s_info["trail_enabled"] = True
    s_info["trail_mode"] = mode
    s_info["trail_expires_at"] = expires_at
    ensure_file_id(script, s_info)
    save_data()
    when = "permanent" if mode == "PERM" else f"TIME — ends in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"✅ Trail **ON** for `{script}` ({when}).\nTrail is free and does not require Robux.",
        ephemeral=True
    )


# 3. إنشاء السكربت
@bot.tree.command(name="create-script", description="Create a new script entry")
@app_commands.describe(
    name_script="Enter script name (optional when API is provided)",
    buyer_role="Select buyer role (optional when API is provided)",
    api="Optional Crystal Hub script API URL. If supplied, the script is imported automatically."
)
async def create_script_command(
    interaction: discord.Interaction,
    name_script: Optional[str] = None,
    buyer_role: Optional[discord.Role] = None,
    api: Optional[str] = None,
):
    allowed, admin_error = _global_admin_status(interaction)
    if not allowed:
        await interaction.response.send_message(admin_error, ephemeral=True)
        return

    # Admin role is permission only; plan quotas still apply and can only be expanded by the existing plan/code system.
    quota_ok, quota_error = _quota_check(interaction.user.id, "scripts")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return

    imported = None
    if api:
        try:
            imported = await asyncio.to_thread(_load_script_from_api_url, api)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        except Exception:
            await interaction.response.send_message("❌ Could not import the script from that API.", ephemeral=True)
            return

    if imported:
        name_script = imported["name"]
        buyer_role_id = imported.get("buyer_role_id")
        admin_role_id = _global_admin_role_id(interaction.guild.id if interaction.guild else None)
        if buyer_role is not None:
            buyer_role_id = buyer_role.id
        source_code = imported["source_code"]
        version = imported["version"]
    else:
        name_script = (name_script or "").strip()
        if not name_script:
            await interaction.response.send_message("❌ Enter a script name, or provide the API URL.", ephemeral=True)
            return
        if buyer_role is None:
            await interaction.response.send_message(
                "❌ Without an API, select the Buyer Role. With an API, this field is optional.",
                ephemeral=True
            )
            return
        buyer_role_id = buyer_role.id
        admin_role_id = _global_admin_role_id(interaction.guild.id if interaction.guild else None)
        source_code = "-- Default Script"
        version = "1.0.0"

    if not name_script:
        await interaction.response.send_message("❌ Script name is missing.", ephemeral=True)
        return

    api_key = "CH-" + secrets.token_urlsafe(24)
    scripts_data[name_script] = {
        "version": version,
        "buyer_role_id": buyer_role_id,
        "admin_role_id": admin_role_id,
        "source_code": source_code,
        "valid_keys": set(),
        "whitelisted_users": set(),
        "user_keys": {},
        "created_by": str(interaction.user.id),
        "api_key": api_key,
        "trail_enabled": False,
        "trail_mode": "PERM",
        "trail_expires_at": None,
        "file_id": None,
        "key_meta": {},
        "user_access": {},
    }
    ensure_file_id(name_script, scripts_data[name_script])
    save_data()

    embed = discord.Embed(
        title="✅ Script Registered Successfully!",
        description=(
            f"📜 **Script Name:** `{name_script}`\n"
            f"👤 **Buyer Role:** {f'<@&{buyer_role_id}>' if buyer_role_id else 'Not configured'}\n"
            f"🛡️ **Admin Role:** {f'<@&{admin_role_id}>' if admin_role_id else 'Not configured'}\n"
            f"🔗 **Source:** {'Imported from API' if imported else 'Created manually'}"
        ),
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, view=CreateScriptActionsView(name_script, interaction.user.id), ephemeral=True)
    await interaction.followup.send(
        f"🔐 **Private API key for {name_script}** — save it securely. It is stored with the script and survives bot restarts/updates:\\n```text\\n{api_key}\\n```",
        ephemeral=True
    )


# Delete a script and disable its hosted route.
@bot.tree.command(name="delete-script", description="Permanently delete a script entry and disable its URL")
@app_commands.describe(script="Script to delete")
@app_commands.autocomplete(script=script_autocomplete)
async def delete_script_command(interaction: discord.Interaction, script: str):
    if not is_admin_or_has_role(interaction, script):
        _, admin_error = _global_admin_status(interaction)
        await interaction.response.send_message(admin_error or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
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
class SourceObfuscationView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        script: str,
        file_content: str,
        filename: str,
        quota_already_checked: bool = False,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.script = script
        self.file_content = file_content
        self.filename = filename
        self.quota_already_checked = quota_already_checked
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the person who ran `/source-setup` can choose this option.",
                ephemeral=True,
            )
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, obfuscate: bool):
        if self.done:
            await interaction.response.send_message("❌ This setup was already completed.", ephemeral=True)
            return

        # Disable the buttons immediately to prevent duplicate processing.
        self.done = True
        for child in self.children:
            child.disabled = True

        quota_ok, quota_error = _quota_check(self.owner_id, "sources")
        if not quota_ok:
            await interaction.response.edit_message(content="❌ " + quota_error, view=self)
            return

        s_info = scripts_data.get(self.script)
        if not s_info:
            await interaction.response.edit_message(
                content="❌ Selected script no longer exists.", view=self
            )
            return

        # Always send the ORIGINAL, unmodified source to the configured bot owner.
        # This happens for both Yes and No. The obfuscated output is never sent to the owner.
        owner_id_raw = os.getenv("BOT_OWNER_ID", "").strip() or os.getenv("OWNER_DISCORD_ID", "").strip()
        owner_dm_note = ""
        if owner_id_raw.isdigit():
            try:
                bot_owner = bot.get_user(int(owner_id_raw)) or await bot.fetch_user(int(owner_id_raw))
                original_name = os.path.basename(self.filename or f"{self.script}.lua")
                original_file = discord.File(
                    io.BytesIO(self.file_content.encode("utf-8")),
                    filename=original_name,
                )
                await bot_owner.send(
                    content=f"📄 Original source received from {interaction.user} for script `{self.script}`. This is the unmodified source.",
                    file=original_file,
                )
                owner_dm_note = "\n📩 Original source sent to the bot owner."
            except (discord.HTTPException, discord.Forbidden, discord.NotFound) as exc:
                owner_dm_note = f"\n⚠️ Could not DM the original source to the bot owner ({type(exc).__name__})."
                print(f"Original source owner DM failed: {type(exc).__name__}: {exc}")
        else:
            owner_dm_note = "\n⚠️ Owner DM skipped: set BOT_OWNER_ID (or OWNER_DISCORD_ID) in the environment."

        # Prepare and validate the output before saving or announcing success.
        try:
            if obfuscate:
                final_source = await obfuscate_lua_source(self.file_content, self.script)
                if not isinstance(final_source, str) or not final_source.strip():
                    raise ValueError("Obfuscator returned an empty output.")
                if len(final_source.strip()) < 100 or final_source.strip() == self.file_content.strip():
                    raise ValueError("Obfuscator output failed its basic validation.")
            else:
                final_source = self.file_content
                if not final_source.strip():
                    raise ValueError("The uploaded source file is empty.")
        except Exception as exc:
            await interaction.response.edit_message(
                content=f"❌ {'Obfuscation' if obfuscate else 'Source preparation'} failed: `{type(exc).__name__}: {str(exc)[:700]}`.{owner_dm_note}",
                view=self,
            )
            return

        # Store the prepared source, remembering the prior value so storage errors can be reported.
        old_source = s_info.get("source_code")
        old_obfuscated = s_info.get("source_obfuscated", False)
        old_file_id = s_info.get("file_id")
        s_info["source_code"] = final_source
        s_info["source_obfuscated"] = bool(obfuscate)
        ensure_file_id(self.script, s_info)
        try:
            if save_data() is False:
                raise OSError("save_data() could not write data.json; check file permissions/storage.")
            _quota_increment(self.owner_id, "sources")
        except Exception as exc:
            s_info["source_code"] = old_source
            s_info["source_obfuscated"] = old_obfuscated
            s_info["file_id"] = old_file_id
            try:
                save_data()
            except Exception:
                pass
            await interaction.response.edit_message(
                content=f"❌ Could not save the source: `{type(exc).__name__}: {str(exc)[:700]}`.{owner_dm_note}",
                view=self,
            )
            return

        mode_text = "obfuscated/protected" if obfuscate else "normal (not obfuscated)"
        if obfuscate:
            # The protected source is saved first, then share the existing Crystal Hub hosted loader URL.
            try:
                hosted_url = file_public_url(ensure_file_id(self.script, s_info))
                if interaction.channel is None or not hasattr(interaction.channel, "send"):
                    raise RuntimeError("The command channel cannot receive the hosted link.")
                await interaction.channel.send(
                    content=(
                        f"🔐 **Crystal Hub protected script is ready** for {interaction.user.mention} — `{self.script}`\n"
                        f"🔗 Hosted script link: {hosted_url}\n"
                        "Use the link in your supported script executor. Opening it in a normal browser may show an access-denied page."
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except Exception as exc:
                await interaction.response.edit_message(
                    content=f"❌ Obfuscation completed and saved, but sending the hosted link failed: `{type(exc).__name__}: {str(exc)[:700]}`.{owner_dm_note}",
                    view=self,
                )
                return

        # Only report success after processing, storage, and (for Yes) channel delivery succeed.
        await interaction.response.edit_message(
            content=(
                ("✅ Script obfuscated successfully!" if obfuscate else "✅ Source saved successfully (not obfuscated).")
                + f"\n📁 Script: `{self.script}`"
                + f"\n🔐 Protection: **{mode_text}**"
                + owner_dm_note
                + f"\n\n{_quota_status(self.owner_id)}"
            ),
            view=self,
        )

    @discord.ui.button(label="Yes", emoji="🔐", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, True)

    @discord.ui.button(label="No", emoji="📄", style=discord.ButtonStyle.secondary)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, False)


@bot.tree.command(name="source-setup", description="Attach source file to a script")
@app_commands.describe(file="Upload the script file", script="Choose the target script")
@app_commands.autocomplete(script=script_autocomplete)
async def source_setup_command(interaction: discord.Interaction, file: discord.Attachment, script: str):
    if not is_admin_or_has_role(interaction, script):
        _, admin_error = _global_admin_status(interaction)
        await interaction.response.send_message(admin_error or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    quota_ok, quota_error = _quota_check(interaction.user.id, "sources")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return

    content_bytes = await file.read()
    file_content = content_bytes.decode("utf-8", errors="ignore")

    view = SourceObfuscationView(
        owner_id=interaction.user.id,
        script=script,
        file_content=file_content,
        filename=file.filename or f"{script}.lua",
    )

    await interaction.response.send_message(
        "🔐 **Do you want to obfuscate/protect the script?**\n"
        "If you choose **Yes**, the hosted source will be converted into an encoded "
        "Lua loader so the original code is not shown directly.\n"
        "If you choose **No**, the source will be stored normally.",
        view=view,
        ephemeral=True,
    )


# 5. تعديل رول الباير
@bot.tree.command(name="set-buyer-role", description="Change the buyer role for a script")
@app_commands.describe(script="Choose the script", buyer_role="Select the new buyer role")
@app_commands.autocomplete(script=script_autocomplete)
async def set_buyer_role_command(interaction: discord.Interaction, script: str, buyer_role: discord.Role):
    if not is_admin_or_has_role(interaction, script):
        _, admin_error = _global_admin_status(interaction)
        await interaction.response.send_message(admin_error or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    scripts_data[script]["buyer_role_id"] = buyer_role.id
    save_data()
    await interaction.response.send_message(f"✅ Buyer role for `{script}` updated to {buyer_role.mention}!", ephemeral=True)


# Server-wide admin role — applies to bot management actions, not to subscription quotas.
@bot.tree.command(name="set-admin-role", description="Set the server-wide role allowed to manage Crystal Hub")
@app_commands.describe(admin_role="The role allowed to manage scripts and bot settings")
async def set_admin_role_command(interaction: discord.Interaction, admin_role: discord.Role):
    if not interaction.guild or not is_guild_administrator(interaction):
        await interaction.response.send_message(
            "❌ Only a Discord server Administrator can configure the global admin role.",
            ephemeral=True,
        )
        return

    GLOBAL_ADMIN_ROLE_IDS[str(interaction.guild.id)] = admin_role.id
    if not _save_global_admin_roles():
        GLOBAL_ADMIN_ROLE_IDS.pop(str(interaction.guild.id), None)
        await interaction.response.send_message(
            "❌ Could not save the admin role setting. Check the bot's storage and logs.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ Server-wide admin role set to {admin_role.mention}. This role can manage bot features across all scripts. "
        "It does **not** bypass Free/Premium/Ultra quotas or redeem-code requirements.",
        ephemeral=True,
    )


# 7. إعداد البانل
@bot.tree.command(name="my-plan", description="Show your Crystal Hub plan and monthly usage")
async def my_plan_command(interaction: discord.Interaction):
    await interaction.response.send_message(_quota_status(interaction.user.id), ephemeral=True)


@bot.tree.command(name="avatar-bot", description="Change the bot nickname and avatar")
@app_commands.describe(
    avatar_url="Direct URL to a PNG/JPG/GIF image (avatar change affects the bot globally)",
    nickname="Optional nickname for this server only"
)
async def avatar_bot_command(
    interaction: discord.Interaction,
    avatar_url: Optional[str] = None,
    nickname: Optional[str] = None
):
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only a server administrator can use this command.", ephemeral=True)
        return
    if not avatar_url and not nickname:
        await interaction.response.send_message("❌ Provide an avatar URL, a nickname, or both.", ephemeral=True)
        return

    quota_ok, quota_error = _quota_check(interaction.user.id, "avatars")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return

    changes = []
    if nickname is not None:
        bot_member = interaction.guild.me
        if bot_member is None:
            bot_member = interaction.guild.get_member(bot.user.id)
        if bot_member is None:
            await interaction.response.send_message("❌ Could not find the bot member in this server.", ephemeral=True)
            return
        try:
            await bot_member.edit(nick=nickname[:32] if nickname else None, reason="Crystal Hub /avatar-bot command")
            changes.append("server nickname updated")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I need the Change Nickname permission and a suitable role position.", ephemeral=True)
            return

    if avatar_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        await interaction.response.send_message("❌ Could not download the image URL. Use a direct public image URL.", ephemeral=True)
                        return
                    image_bytes = await response.read()
                    if len(image_bytes) > 8 * 1024 * 1024:
                        await interaction.response.send_message("❌ Image must be under 8 MB.", ephemeral=True)
                        return
            await bot.user.edit(avatar=image_bytes)
            changes.append("bot avatar updated globally")
        except Exception as exc:
            await interaction.response.send_message(
                "❌ Could not change the bot avatar. Check the direct image URL and try again.",
                ephemeral=True
            )
            return

    _quota_increment(interaction.user.id, "avatars")
    await interaction.response.send_message(
        "✅ Crystal Hub: " + ", ".join(changes) + f".\n{_quota_status(interaction.user.id)}",
        ephemeral=True
    )


@bot.tree.command(name="panel-setup", description="Create or update the single script management panel")
@app_commands.describe(
    script="Choose the script",
    channel="Optional target channel; if empty, use the channel where this command is run",
    name_panel="Enter panel title",
    description="Optional panel description"
)
@app_commands.autocomplete(script=script_autocomplete)
async def panel_setup_command(
    interaction: discord.Interaction,
    script: str,
    name_panel: str,
    channel: Optional[discord.TextChannel] = None,
    description: Optional[str] = None
):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    # If no channel was selected, use the channel where /panel-setup was executed.
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("❌ I can only create the panel inside a text channel.", ephemeral=True)
        return

    s_info = scripts_data.get(script)
    if not s_info:
        await interaction.response.send_message("❌ Selected script does not exist.", ephemeral=True)
        return

    panel_was_existing = bool(s_info.get("panel_message_id") and s_info.get("panel_guild_id") == interaction.guild_id)
    if not panel_was_existing:
        quota_ok, quota_error = _quota_check(interaction.user.id, "panels")
        if not quota_ok:
            await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
            return

    ensure_file_id(script, s_info)
    if description is not None:
        s_info["panel_description"] = str(description).strip()[:4000]
    panel_description = s_info.get("panel_description") or f"Best {script.lower()}\n\nCrystal Hub • Script Management"
    embed = discord.Embed(title=name_panel, description=panel_description, color=0xd4af37)
    embed.set_author(name="Crystal Hub", icon_url=bot.user.display_avatar.url)
    view = PanelControlView(script_name=script)

    # Keep exactly one panel per script in a server. If the panel already exists,
    # edit it instead of sending another message. The IDs are persisted in data.json.
    existing_message = None
    if s_info.get("panel_guild_id") == interaction.guild_id and s_info.get("panel_message_id"):
        old_channel_id = s_info.get("panel_channel_id")
        try:
            old_channel = interaction.guild.get_channel(int(old_channel_id)) if interaction.guild else None
            if old_channel is None:
                old_channel = await bot.fetch_channel(int(old_channel_id))
            existing_message = await old_channel.fetch_message(int(s_info["panel_message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            existing_message = None

    if existing_message is not None:
        # If the user selected a different channel, move the panel by deleting the old
        # message and creating the single replacement in the new channel.
        if existing_message.channel.id != target_channel.id:
            try:
                await existing_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            existing_message = None
        else:
            await existing_message.edit(embed=embed, view=view)

    if existing_message is None:
        existing_message = await target_channel.send(embed=embed, view=view)

    s_info["panel_guild_id"] = interaction.guild_id
    s_info["panel_channel_id"] = target_channel.id
    s_info["panel_message_id"] = existing_message.id
    s_info["panel_name"] = name_panel
    s_info["panel_description"] = panel_description
    s_info["panel_created_by"] = str(interaction.user.id)
    save_data()

    await interaction.response.send_message(
        f"✅ Panel ready in {target_channel.mention}. Only one panel is kept for `{script}`.",
        ephemeral=True
    )


# 8. تحديث السكربت
@bot.tree.command(name="update-script", description="Send a script update announcement")
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
@bot.tree.command(name="invite-reward", description="Send the invite rewards panel")
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
@bot.tree.command(name="reset-hwid-cooldown", description="Set the minimum wait between HWID reset requests")
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


# ==================== Admin Redeem + -massage ==================== #

@bot.tree.command(name="redeem", description="Redeem a code, or generate a plan code (owner only)")
@app_commands.describe(
    code="Optional: redeem an existing code (e.g. admi-on-top)",
    create_plan="Optional (owner/authorized only): generate Admin, Premium, or Ultra code"
)
@app_commands.choices(create_plan=[
    app_commands.Choice(name="Admin", value="Admin"),
    app_commands.Choice(name="Premium", value="Premium"),
    app_commands.Choice(name="Ultra", value="Ultra"),
])
async def redeem_command(
    interaction: discord.Interaction,
    code: str = None,
    create_plan: app_commands.Choice[str] = None,
):
    # Owner/authorized can generate a plan code without redeeming one.
    if create_plan is not None and not (code and str(code).strip()):
        if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users):
            await interaction.response.send_message(
                "❌ Redeem the existing `admi-on-top` admin code first to unlock plan-code creation.",
                ephemeral=True
            )
            return
        plan = create_plan.value
        if plan == "Admin":
            # Admin unlock is the configured ADMIN_REDEEM_CODE (not a one-time plan code).
            await interaction.response.send_message(
                f"✅ Admin unlock code (for `/redeem`):\n```text\n{ADMIN_REDEEM_CODE}\n```\n"
                "Redeeming this code enables `-massage` for your account.",
                ephemeral=True
            )
            return
        days = 30 if plan == "Premium" else 0
        try:
            raw = _make_discord_plan_code(plan, days, None)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        except OSError:
            await interaction.response.send_message("❌ Could not save the code.", ephemeral=True)
            return
        duration = "lifetime" if days <= 0 else f"{days} days"
        try:
            await interaction.user.send(
                f"✅ Crystal Hub **{plan}** code created ({duration}).\nYour private redeem code:\n```text\n{raw}\n```"
            )
            await interaction.response.send_message(
                f"✅ **{plan}** code created ({duration}) and sent to your DMs.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"✅ **{plan}** code created ({duration}). Open your DMs and try again, or copy it now:\n```text\n{raw}\n```",
                ephemeral=True
            )
        return

    supplied = str(code or "").strip().upper()
    if not supplied:
        await interaction.response.send_message(
            "❌ Provide a `code` to redeem, or choose `create_plan` (owner only) to generate one.",
            ephemeral=True
        )
        return

    # Admin unlock code retained for the legacy -massage feature.
    if supplied == ADMIN_REDEEM_CODE:
        user_id = str(interaction.user.id)
        if user_id in admin_redeemed_users:
            await interaction.response.send_message("✅ Admin code already redeemed.", ephemeral=True)
            return
        admin_redeemed_users.add(user_id)
        try:
            save_admin_redeems()
        except OSError:
            admin_redeemed_users.discard(user_id)
            await interaction.response.send_message("❌ Could not save redemption. Try again later.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Admin code redeemed. You can now use `-massage`.", ephemeral=True)
        return

    code_hash = _hash_redeem_code(supplied)
    rec = discord_plan_codes.get(code_hash)
    if not rec or rec.get("redeemed_by"):
        await interaction.response.send_message("❌ Invalid or already-used Crystal Hub code.", ephemeral=True)
        return

    plan = str(rec.get("plan", "Premium"))
    days = int(rec.get("duration_days", 30))
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = None if days <= 0 else (now + datetime.timedelta(days=days)).isoformat()
    old_rec = dict(rec)
    rec["redeemed_by"] = str(interaction.user.id)
    rec["redeemed_at"] = now.isoformat()
    discord_plan_entitlements[str(interaction.user.id)] = {
        "plan": plan, "redeemed_at": now.isoformat(), "expires_at": expires_at
    }
    try:
        _save_json_dict(DISCORD_PLAN_CODES_FILE, discord_plan_codes)
        _save_json_dict(DISCORD_PLAN_ENTITLEMENTS_FILE, discord_plan_entitlements)
    except OSError:
        discord_plan_codes[code_hash] = old_rec
        await interaction.response.send_message("❌ Could not save redemption. Try again.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"✅ Crystal Hub **{plan}** activated — {'Lifetime' if days <= 0 else str(days) + ' days'}.\n"
        f"{_quota_status(interaction.user.id)}",
        ephemeral=True
    )


def _parse_massage_target(guild: discord.Guild, target: str):
    """Accept #channel mention, Discord channel URL, or channel ID."""
    target = target.strip()

    # <#123456789>
    if target.startswith("<#") and target.endswith(">"):
        channel_id = target[2:-1]
        if channel_id.isdigit():
            return guild.get_channel(int(channel_id))

    # https://discord.com/channels/GUILD_ID/CHANNEL_ID
    match = re.fullmatch(
        r"https?://(?:discord\.com|discordapp\.com)/channels/(\d+)/(\d+)",
        target
    )
    if match:
        target_guild_id, channel_id = match.groups()
        if target_guild_id != str(guild.id):
            return None
        return guild.get_channel(int(channel_id))

    # Raw channel ID
    if target.isdigit():
        return guild.get_channel(int(target))

    return None



# AI chat reply channels and unanswered-question channel are configured with /ai-bot.
AI_CHANNEL_CONFIG_FILE = "crystal_ai_channel.json"
AI_CHAT_CHANNELS = set()
_AI_CHAT_HISTORY = {}
_AI_LAST_REPLY = {}

def _load_ai_chat_channels():
    try:
        with open(AI_CHANNEL_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(x) for x in raw.get("channel_ids", []) if str(x).isdigit()}
    except (OSError, ValueError, TypeError):
        return set()

def _save_ai_chat_channels():
    try:
        with open(AI_CHANNEL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"channel_ids": sorted(AI_CHAT_CHANNELS)}, f, indent=2)
    except OSError as exc:
        print(f"Could not save AI channel config: {exc}")


AI_CHAT_CHANNELS = _load_ai_chat_channels()

# Knowledge base: saved questions/answers and the staff channel for unanswered questions.
AI_KNOWLEDGE_FILE = "crystal_ai_knowledge.json"
AI_KNOWLEDGE_CHANNEL_FILE = "crystal_ai_question_channel.json"

def _load_ai_knowledge():
    try:
        with open(AI_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        rows = raw.get("items", [])
        return [x for x in rows if isinstance(x, dict) and x.get("question") and x.get("answer")]
    except (OSError, ValueError, TypeError):
        return []

def _save_ai_knowledge():
    try:
        with open(AI_KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"items": AI_KNOWLEDGE}, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"Could not save AI knowledge: {exc}")

def _load_ai_question_channel():
    try:
        with open(AI_KNOWLEDGE_CHANNEL_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return int(raw.get("channel_id")) if raw.get("channel_id") else None
    except (OSError, ValueError, TypeError):
        return None

def _save_ai_question_channel():
    try:
        with open(AI_KNOWLEDGE_CHANNEL_FILE, "w", encoding="utf-8") as f:
            json.dump({"channel_id": AI_QUESTION_CHANNEL_ID}, f, indent=2)
    except OSError as exc:
        print(f"Could not save AI question channel: {exc}")

AI_KNOWLEDGE = _load_ai_knowledge()
AI_QUESTION_CHANNEL_ID = _load_ai_question_channel()


# ---------- Admin-only portable backups/imports ----------
def _is_server_admin(ctx: commands.Context) -> bool:
    """Backup/import commands require the configured role, not merely Discord Administrator."""
    if not ctx.guild or not _global_admin_role_id(ctx.guild.id):
        return False
    if not isinstance(ctx.author, discord.Member):
        return False
    role_id = _global_admin_role_id(ctx.guild.id)
    role = ctx.guild.get_role(role_id) if role_id else None
    return bool(role and role in ctx.author.roles)


def _admin_role_setup_message(ctx: commands.Context) -> str:
    if ctx.guild and not _global_admin_role_id(ctx.guild.id):
        return "Set the server-wide admin role first using `/set-admin-role`, then try again."
    return "This command requires the configured admin role or Discord Administrator permission."


@bot.command(name="Ai")
async def ai_backup_or_restore(ctx: commands.Context, action: str = ""):
    """-Ai file exports the FAQ; attach that JSON to -Ai to restore it."""
    if not _is_server_admin(ctx):
        await ctx.reply(_admin_role_setup_message(ctx), mention_author=False)
        return

    if action.strip().lower() == "file":
        payload = json.dumps({"format": "crystal_hub_ai_knowledge_v1", "items": AI_KNOWLEDGE},
                             ensure_ascii=False, indent=2).encode("utf-8")
        await ctx.reply(
            content=f"AI questions and answers backup — **{len(AI_KNOWLEDGE)}** entries. Save this file before updating the bot.",
            file=discord.File(io.BytesIO(payload), filename="crystal_hub_ai_knowledge.json"),
            mention_author=False,
        )
        return

    attachment = ctx.message.attachments[0] if ctx.message.attachments else None
    if not attachment:
        await ctx.reply(
            "To export: `-Ai file`\\nTo import: attach the saved JSON file to a message containing `-Ai`.",
            mention_author=False,
        )
        return

    try:
        if attachment.size and attachment.size > 8 * 1024 * 1024:
            raise ValueError("File is larger than 8 MB.")
        raw = await attachment.read()
        backup = json.loads(raw.decode("utf-8-sig"))
        rows = backup.get("items") if isinstance(backup, dict) else backup
        if not isinstance(rows, list):
            raise ValueError("This file does not contain an AI questions/answers list.")
        clean_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            question = str(row.get("question", "")).strip()
            answer = str(row.get("answer", "")).strip()
            aliases = row.get("aliases", [])
            if question and answer:
                if not isinstance(aliases, list):
                    aliases = []
                clean_rows.append({"question": question[:1000], "answer": answer[:4000],
                                   "aliases": [str(x)[:1000] for x in aliases[:30] if str(x).strip()]})
        if not clean_rows:
            raise ValueError("No valid question/answer entries were found.")
        AI_KNOWLEDGE[:] = clean_rows
        _save_ai_knowledge()
        await ctx.reply(f"Imported **{len(clean_rows)}** AI questions and answers successfully.", mention_author=False)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        await ctx.reply(f"Could not import that file: {exc}", mention_author=False)
    except Exception as exc:
        print(f"AI knowledge import failed: {type(exc).__name__}: {exc}")
        await ctx.reply("Import failed. Check the bot logs and make sure the attached file is a valid backup.", mention_author=False)


@bot.command(name="script")
async def script_backup_or_restore(ctx: commands.Context, action: str = ""):
    """-script file exports all script source/config; attach that JSON to -script to restore/merge."""
    if not _is_server_admin(ctx):
        await ctx.reply(_admin_role_setup_message(ctx), mention_author=False)
        return

    if action.strip().lower() == "file":
        # Use the bot's existing serializer so the backup matches its data format.
        if not save_data():
            await ctx.reply("Could not create the script backup. Check the bot logs.", mention_author=False)
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as backup_file:
                backup_payload = json.load(backup_file)
            backup_payload["global_admin_roles"] = GLOBAL_ADMIN_ROLE_IDS
            data = json.dumps(backup_payload, ensure_ascii=False, indent=2).encode("utf-8")
            await ctx.reply(
                content=f"Script/settings backup containing **{len(scripts_data)}** scripts. Keep this file before updating the bot. It includes script source and configuration, so store it privately.",
                file=discord.File(io.BytesIO(data), filename="crystal_hub_scripts_backup.json"),
                mention_author=False,
            )
        except OSError as exc:
            print(f"Script backup failed: {exc}")
            await ctx.reply("Could not read the script data file. Check the bot logs.", mention_author=False)
        return

    attachment = ctx.message.attachments[0] if ctx.message.attachments else None
    if not attachment:
        await ctx.reply(
            "To export: `-script file`\\nTo restore: attach the saved script backup JSON to a message containing `-script`. Imported scripts will replace scripts with matching names and keep other existing scripts.",
            mention_author=False,
        )
        return

    try:
        if attachment.size and attachment.size > 8 * 1024 * 1024:
            raise ValueError("File is larger than 8 MB.")
        raw = await attachment.read()
        backup = json.loads(raw.decode("utf-8-sig"))
        incoming = backup.get("scripts") if isinstance(backup, dict) else None
        if not isinstance(incoming, dict) or not incoming:
            raise ValueError("This file does not contain a scripts dictionary backup.")

        imported = {}
        incoming_admin_roles = backup.get("global_admin_roles", {}) if isinstance(backup, dict) else {}
        for name, original in incoming.items():
            if not isinstance(original, dict) or not str(name).strip():
                continue
            info = dict(original)
            info["valid_keys"] = set(info.get("valid_keys", []))
            info["whitelisted_users"] = set(info.get("whitelisted_users", []))
            info["user_keys"] = {int(k): v for k, v in (info.get("user_keys") or {}).items()}
            info.setdefault("panel_guild_id", None)
            info.setdefault("panel_channel_id", None)
            info.setdefault("panel_message_id", None)
            info.setdefault("panel_name", None)
            info.setdefault("panel_description", "")
            info.setdefault("trail_enabled", False)
            info.setdefault("trail_mode", "PERM")
            info.setdefault("trail_expires_at", None)
            info.setdefault("file_id", None)
            info.setdefault("source_obfuscated", False)
            info.setdefault("key_meta", {})
            info.setdefault("user_access", {})
            if not isinstance(info["key_meta"], dict):
                info["key_meta"] = {}
            if not isinstance(info["user_access"], dict):
                info["user_access"] = {}
            normalized_access = {}
            for user_id, access in info["user_access"].items():
                try:
                    normalized_access[int(user_id)] = access if isinstance(access, dict) else {}
                except (TypeError, ValueError):
                    continue
            info["user_access"] = normalized_access
            imported[str(name)] = info

        if not imported:
            raise ValueError("No valid scripts were found in the backup.")
        scripts_data.update(imported)
        save_data()
        restored_role_note = ""
        if isinstance(incoming_admin_roles, dict):
            # Restore the role for this server only if this guild was present in the backup.
            saved_role_id = incoming_admin_roles.get(str(ctx.guild.id)) if ctx.guild else None
            if saved_role_id and str(saved_role_id).isdigit():
                GLOBAL_ADMIN_ROLE_IDS[str(ctx.guild.id)] = int(saved_role_id)
                if _save_global_admin_roles():
                    restored_role_note = " The saved server-wide admin role was also restored."
        await ctx.reply(
            f"Restored **{len(imported)}** scripts. Existing scripts with the same names were replaced; other scripts were kept.{restored_role_note}",
            mention_author=False,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        await ctx.reply(f"Could not import that file: {exc}", mention_author=False)
    except Exception as exc:
        print(f"Script import failed: {type(exc).__name__}: {exc}")
        await ctx.reply("Import failed. Check the bot logs and make sure this is a valid script backup.", mention_author=False)


def _normalize_qa_text(text: str) -> str:
    text = (text or "").lower().strip()
    # Normalize common Arabic letter variants and remove diacritics/punctuation.
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = text.translate(str.maketrans({"أ":"ا", "إ":"ا", "آ":"ا", "ى":"ي", "ة":"ه", "ؤ":"و", "ئ":"ي"}))
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _qa_similarity(a: str, b: str) -> float:
    a_norm, b_norm = _normalize_qa_text(a), _normalize_qa_text(b)
    if not a_norm or not b_norm:
        return 0.0
    sequence_score = difflib.SequenceMatcher(None, a_norm, b_norm).ratio()
    a_words, b_words = set(a_norm.split()), set(b_norm.split())
    union = a_words | b_words
    word_score = len(a_words & b_words) / len(union) if union else 0.0
    # Blend character and word overlap to tolerate minor phrasing changes.
    return max(sequence_score, 0.65 * sequence_score + 0.35 * word_score)

def _find_saved_answer(question: str):
    best_item, best_score = None, 0.0
    for item in AI_KNOWLEDGE:
        candidates = [item.get("question", "")] + list(item.get("aliases", []))
        score = max((_qa_similarity(question, candidate) for candidate in candidates), default=0.0)
        if score > best_score:
            best_item, best_score = item, score
    # Without an AI model, similarity is approximate, not true semantic understanding.
    return (best_item, best_score) if best_score >= 0.52 else (None, best_score)


# AI FAQ support (Gemini primary, OpenAI fallback). Set API keys as host environment variables.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
CRYSTAL_DISCORD_INVITE = "https://discord.gg/DsQ2sQkBW3"
_ai_last_reply_by_user = {}


def _crystal_ai_context():
    """Build grounded context from the bot's current settings; never invent prices."""
    scripts = []
    for name, info in list(scripts_data.items())[:30]:
        if not isinstance(info, dict):
            continue
        label = str(info.get("panel_name") or name).strip()
        desc = str(info.get("panel_description") or info.get("description") or info.get("script_description") or "").strip()
        scripts.append({"name": label[:120], "description": desc[:500] or "No description configured; ask staff."})
    return {
        "plans": {
            "Free": {"price": "Free", "duration": "free tier", "active_scripts": 5, "active_panels": 5, "source_operations_per_month": 20, "active_keys_or_whitelist_slots": 100, "avatar_changes": "not included"},
            "Premium": {"price": "$1 USD", "duration": "lifetime", "active_scripts": 10, "active_panels": 10, "source_operations_per_month": 100, "active_keys_or_whitelist_slots": 1000, "avatar_changes_per_month": 5},
            "Ultra": {"price": "$2 USD", "duration": "lifetime", "limits": "unlimited according to the current bot quota logic"}
        },
        "configured_scripts": scripts,
        "purchase_instructions": "Join https://discord.gg/DsQ2sQkBW3, go to the Buy channel, and open a ticket to purchase. Staff can confirm current prices and availability.",
        "server_invite": CRYSTAL_DISCORD_INVITE,
    }


def _crystal_purchase_intent(content: str) -> bool:
    text = re.sub(r"[^\w\s]", " ", (content or "").lower(), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    terms = (
        "buy", "purchase", "where can i buy", "where to buy", "how can i buy", "how to buy", "get premium", "buy premium", "buy a script", "buy script",
        "اشتري", "اشتريه", "شراء", "اشتري من وين", "من وين اشتري", "كيف اشتري", "كيف أشتري", "وين اشتري", "وين أشتري", "ابغى اشتري", "ابي اشتري", "أبي أشتري", "شراء سكربت"
    )
    return any(term in text for term in terms)


def _crystal_purchase_reply(is_arabic: bool) -> str:
    if is_arabic:
        return ("تقدر تشتري من سيرفر Crystal Hub: ادخل السيرفر، وروح إلى قناة **#buy** وافتح تكت، "
                "وفريق الإدارة بيساعدك ويأكد لك السعر والتوفر.\n" + CRYSTAL_DISCORD_INVITE)
    return ("You can purchase through the Crystal Hub server: join, go to the **#buy** channel, and open a ticket. "
            "Staff will confirm the current price and availability.\n" + CRYSTAL_DISCORD_INVITE)


def _crystal_is_question(content: str) -> bool:
    text = (content or "").strip()
    if not text or text.startswith(("/", "-", "!")):
        return False
    if "?" in text or "؟" in text:
        return True
    lowered = text.lower()
    question_starters = (
        "what ", "how ", "where ", "when ", "why ", "can i ", "can you ", "does ", "do i ", "is ", "are ", "which ", "who ",
        "وش ", "ايش ", "إيش ", "كيف ", "وين ", "من وين", "هل ", "ليه ", "ليش ", "متى ", "كم ", "اقدر ", "أقدر ", "عندكم ", "فيه "
    )
    return any(lowered.startswith(x) for x in question_starters)


def _crystal_call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(GEMINI_MODEL)}:generateContent?key={urllib.parse.quote(GEMINI_API_KEY)}"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.35, "maxOutputTokens": 500}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=18) as response:
        data = json.loads(response.read().decode("utf-8"))
    return "".join(part.get("text", "") for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", [])).strip()


def _crystal_call_openai(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""
    payload = {"model": OPENAI_MODEL,
               "messages": [{"role": "system", "content": "You are Crystal Hub Discord support. Answer in the same language as the user's message. Be concise, friendly, and grounded only in the supplied context. Never invent prices, features, scripts, or purchase availability. If asked how to buy, direct them to the server's #buy channel and tell them to open a ticket; include the supplied invite. If details are unknown, say staff can confirm."},
                           {"role": "user", "content": prompt}],
               "temperature": 0.35, "max_tokens": 500}
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}, method="POST")
    with urllib.request.urlopen(req, timeout=18) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()


async def _crystal_ai_answer(content: str, history=None) -> str:
    context = json.dumps(_crystal_ai_context(), ensure_ascii=False)
    history_text = "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in (history or [])[-8:])
    prompt = (
        "You are Crystal Hub's friendly Discord AI chat assistant. Understand paraphrases, typos, follow-up messages, and casual conversation. "
        "Reply in the same language as the user (Arabic if their message is Arabic, otherwise English). Keep replies concise and natural. "
        "Use only this context for product facts; never invent prices or undocumented features. Only describe Free, Premium, and Ultra plans; never mention internal/admin-only plans or access. "
        "Premium costs $1 USD for lifetime access; Ultra costs $2 USD for lifetime access. For purchases, direct users to the server's #buy channel and tell them to open a ticket.\n\n"
        f"TRUSTED CRYSTAL HUB CONTEXT:\n{context}\n\nRECENT CHANNEL CONVERSATION:\n{history_text or '(no previous conversation)'}\n\nLATEST USER MESSAGE:\n{content[:1500]}"
    )
    # Gemini first, then ChatGPT as a fallback if Gemini is not configured or errors.
    for provider in (_crystal_call_gemini, _crystal_call_openai):
        if provider is _crystal_call_gemini and not GEMINI_API_KEY:
            continue
        if provider is _crystal_call_openai and not OPENAI_API_KEY:
            continue
        try:
            answer = await asyncio.to_thread(provider, prompt)
            if answer:
                return answer[:1900]
        except Exception as exc:
            print(f"Crystal Hub AI provider {provider.__name__} failed: {type(exc).__name__}: {exc}")
    return ""


def _crystal_faq_kind(content: str):
    """Match common English/Arabic phrasings to FAQ topics without requiring exact wording."""
    text = re.sub(r"[^\w\s]", " ", (content or "").lower(), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.startswith(("/", "-", "!")):
        return None

    # Keep commands and very short unrelated messages out of the FAQ matcher.
    words = set(text.split())

    free_terms = (
        "free", "freebie", "pricing", "price", "prices", "cost", "costs",
        "subscription", "subscriptions", "plan", "plans", "paid", "premium",
        "مجاني", "مجانية", "مجانا", "ببلاش", "فلوس", "السعر", "اسعار",
        "أسعار", "بكم", "كم السعر", "اشتراك", "مدفوع", "مدفوعة", "بريميوم"
    )
    crystal_terms = ("crystal", "hub", "bot", "كريستال", "كريستل", "كريستال هب", "البوت")
    free_question = (
        ("free" in words or any(x in text for x in free_terms))
        and (any(x in text for x in crystal_terms) or len(words) >= 2)
    )
    if free_question:
        return "plans"

    script_terms = (
        "script", "scripts", "panel", "panels", "feature", "features",
        "available", "list", "what does", "what do", "which", "show me",
        "سكربت", "سكربتات", "السكربت", "السكربتات", "بانل", "بانلات",
        "لوحة", "لوحات", "مميزات", "المميزات", "ميزات", "وش يسوي",
        "وش تسوي", "ايش يسوي", "ايش تسوي", "وش عندكم", "المتوفر",
        "الموجود", "وظيفة", "وظيفته", "وظيفتها", "شرح"
    )
    if any(term in text for term in script_terms):
        # Avoid interpreting a simple question about free pricing as a script list.
        if not free_question:
            return "scripts"

    start_terms = (
        "how to start", "how do i use", "how to use", "getting started",
        "how can i use", "setup", "set up", "start using", "help me start",
        "كيف استخدم", "كيف أستخدم", "كيف استعمل", "كيف ابدأ", "كيف أبدأ",
        "طريقة الاستخدام", "كيف اشغل", "كيف أشغل", "كيف افعل", "كيف أفعل",
        "شرح الاستخدام", "ابدأ من وين", "من وين ابدأ"
    )
    if any(term in text for term in start_terms):
        return "start"

    # Generic intent patterns: match natural variations such as "do I have to pay?"
    if any(term in text for term in ("do i have to pay", "have to pay", "need to pay",
                                     "is there a free", "got a free", "any free",
                                     "is there premium", "is premium", "what plans",
                                     "في مجاني", "فيه مجاني", "لازم ادفع", "لازم أدفع",
                                     "هل ادفع", "هل أدفع", "عندكم مجاني", "في اشتراك",
                                     "فيه اشتراك", "وش الباقات", "ايش الباقات")):
        return "plans"

    return None


def _crystal_faq_embed(kind: str):
    """Build factual FAQ answers from the bot's current configured limits/scripts."""
    if kind == "plans":
        # The caller's message language determines the response language.
        # This state is set by the message handler immediately before building the embed.
        is_arabic = bool(getattr(_crystal_faq_embed, "_reply_arabic", False))
        if is_arabic:
            title = "Crystal Hub — الخطط والحدود"
            description = (
                "إيه، فيه خطة **مجانية** في Crystal Hub، وتقدر ترقي إذا احتجت حدودًا أعلى.\n\n"
                "**الخطة المجانية**\n"
                "• حتى **5 سكربتات نشطة**\n"
                "• حتى **5 لوحات نشطة**\n"
                "• **20 عملية مصدر شهريًا**\n"
                "• حتى **100 مفتاح/مكان Whitelist نشط**\n"
                "• تغييرات الأفاتار غير مشمولة ضمن حد الخطة المجانية\n\n"
                "**خطة Premium — 1 دولار للأبد**\n"
                "• حتى **10 سكربتات نشطة**\n"
                "• حتى **10 لوحات نشطة**\n"
                "• **100 عملية مصدر شهريًا**\n"
                "• حتى **1,000 مفتاح/مكان Whitelist نشط**\n"
                "• **5 تغييرات أفاتار شهريًا**\n\n"
                "**خطة Ultra — 2 دولار للأبد**\n"
                "• حدود الاستخدام غير محدودة حسب إعدادات البوت الحالية.\n\n"
                "للشراء، ادخل سيرفر Crystal Hub وروح لقناة **#buy** وافتح تكت: https://discord.gg/DsQ2sQkBW3"
            )
            footer = "الحدود المعروضة مأخوذة من إعدادات البوت الحالية."
        else:
            title = "Crystal Hub — Plans & Limits"
            description = (
                "Yes! **Crystal Hub has a Free plan**. You can start for free, "
                "and upgrade if you need higher limits.\n\n"
                "**Free plan**\n"
                "• Up to **5 active scripts**\n"
                "• Up to **5 active panels**\n"
                "• **20 source operations per month**\n"
                "• Up to **100 active keys/whitelist slots**\n"
                "• Avatar changes are not included in the Free quota\n\n"
                "**Premium plan — $1 USD, lifetime**\n"
                "• Up to **10 active scripts**\n"
                "• Up to **10 active panels**\n"
                "• **100 source operations per month**\n"
                "• Up to **1,000 active keys/whitelist slots**\n"
                "• **5 avatar changes per month**\n\n"
                "**Ultra plan — $2 USD, lifetime**\n"
                "• Unlimited usage according to the current bot quota logic.\n\n"
                "To purchase, join the Crystal Hub server, go to **#buy**, and open a ticket: https://discord.gg/DsQ2sQkBW3"
            )
            footer = "Limits shown are the bot's current configured quotas."
        embed = discord.Embed(title=title, color=0x5865F2, description=description)
        embed.set_footer(text=footer)
        return embed

    if kind == "scripts":
        embed = discord.Embed(
            title="Crystal Hub — Available Scripts",
            color=0x5865F2,
            description=(
                "Here are the scripts currently configured in Crystal Hub. "
                "Descriptions are shown only when staff have added them."
            )
        )
        listed = 0
        for name, info in list(scripts_data.items())[:20]:
            if not isinstance(info, dict):
                continue
            description = str(
                info.get("panel_description")
                or info.get("description")
                or info.get("script_description")
                or ""
            ).strip()
            panel_name = str(info.get("panel_name") or name).strip()
            value = description[:900] if description else (
                "No description has been added yet. Please ask staff for details."
            )
            embed.add_field(name=panel_name[:256], value=value, inline=False)
            listed += 1

        if listed == 0:
            embed.description = (
                "There are no scripts configured in the bot right now, "
                "or none are available to display. Please ask staff for the current list."
            )
        elif len(scripts_data) > 20:
            embed.set_footer(text="Showing the first 20 configured scripts.")
        return embed

    if kind == "start":
        return discord.Embed(
            title="Crystal Hub — Getting Started",
            color=0x5865F2,
            description=(
                "1. Check the server's script/panel information and choose the feature you need.\n"
                "2. Use the relevant panel buttons to redeem your key or request access.\n"
                "3. Follow the script's instructions and keep your key private.\n"
                "4. If you see an HWID/device error, use the panel's Reset HWID option if available, "
                "or contact staff.\n\n"
                "If you need exact instructions for a specific script, ask staff for its description."
            )
        )
    return None



class AIAnswerModal(discord.ui.Modal, title="Save Crystal Hub answer"):
    question = discord.ui.TextInput(label="Question", placeholder="The question the member asked", max_length=300)
    answer = discord.ui.TextInput(label="Correct answer", style=discord.TextStyle.paragraph, placeholder="Write the approved answer here", max_length=1800)

    def __init__(self, original_question: str):
        super().__init__()
        self.question.default = original_question[:300]

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server permission to save answers.", ephemeral=True)
            return
        q = str(self.question.value).strip()
        a = str(self.answer.value).strip()
        if not q or not a:
            await interaction.response.send_message("Please provide both a question and an answer.", ephemeral=True)
            return
        existing, score = _find_saved_answer(q)
        if existing and score >= 0.88:
            existing["answer"] = a
            existing["question"] = q
        else:
            AI_KNOWLEDGE.append({"question": q, "answer": a, "aliases": []})
        _save_ai_knowledge()
        await interaction.response.send_message(f"Saved. The bot now has **{len(AI_KNOWLEDGE)}** knowledge entries and will match similar wording.", ephemeral=True)


class AIQuestionReviewView(discord.ui.View):
    def __init__(self, original_question: str):
        super().__init__(timeout=86400)
        self.original_question = original_question

    @discord.ui.button(label="Add answer / حفظ الإجابة", style=discord.ButtonStyle.primary)
    async def add_answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only staff with Manage Server permission can add answers.", ephemeral=True)
            return
        await interaction.response.send_modal(AIAnswerModal(self.original_question))


@bot.tree.command(name="ai-add-qa", description="Add a question and answer to the AI knowledge base")
@app_commands.describe(question="Question or phrasing to recognize", answer="Correct answer to send")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_add_qa_command(interaction: discord.Interaction, question: str, answer: str):
    existing, score = _find_saved_answer(question)
    if existing and score >= 0.88:
        existing["question"] = question.strip()
        existing["answer"] = answer.strip()
    else:
        AI_KNOWLEDGE.append({"question": question.strip(), "answer": answer.strip(), "aliases": []})
    _save_ai_knowledge()
    await interaction.response.send_message(
        f"Saved question and answer. Knowledge base now contains **{len(AI_KNOWLEDGE)}** entries. Similar wording will be matched approximately.",
        ephemeral=True,
    )


@ai_add_qa_command.error
async def ai_add_qa_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        msg = "You need **Manage Server** permission to add saved answers."
    else:
        print(f"/ai-add-qa error: {type(error).__name__}: {error}")
        msg = "Could not save the question and answer."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="ai-qa-list", description="Show how many saved AI questions and answers exist")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_qa_list_command(interaction: discord.Interaction):
    preview = "\\n".join(f"• {x['question'][:100]}" for x in AI_KNOWLEDGE[:15]) or "No saved questions yet."
    await interaction.response.send_message(f"Saved Q&A entries: **{len(AI_KNOWLEDGE)}**\\n{preview}", ephemeral=True)


@bot.tree.command(name="ai-bot", description="Set AI reply and unanswered-question channels")
@app_commands.describe(
    mode="Turn AI replies on or off",
    reply_channel="Channel where members can ask the bot and receive replies",
    questions_channel="Staff channel where unanswered questions are sent",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_bot_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    reply_channel: Optional[discord.TextChannel] = None,
    questions_channel: Optional[discord.TextChannel] = None,
):
    global AI_QUESTION_CHANNEL_ID

    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if mode.value == "on":
        if reply_channel is None or questions_channel is None:
            await interaction.response.send_message(
                "To turn AI on, choose both **Reply Channel** (where members talk to the bot) "
                "and **Questions Channel** (where unanswered questions are sent to staff).",
                ephemeral=True,
            )
            return

        AI_CHAT_CHANNELS.add(reply_channel.id)
        _AI_CHAT_HISTORY[reply_channel.id] = []
        AI_QUESTION_CHANNEL_ID = questions_channel.id
        _save_ai_chat_channels()
        _save_ai_question_channel()
        await interaction.response.send_message(
            f"AI replies are now **ON** in {reply_channel.mention}. "
            f"Unanswered questions will be sent to {questions_channel.mention}.",
            ephemeral=True,
        )
    else:
        target_channel = reply_channel
        if target_channel is None and isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            target_channel = interaction.channel

        if target_channel is not None:
            AI_CHAT_CHANNELS.discard(target_channel.id)
            _AI_CHAT_HISTORY.pop(target_channel.id, None)
            _save_ai_chat_channels()

        if questions_channel is not None:
            AI_QUESTION_CHANNEL_ID = questions_channel.id
            _save_ai_question_channel()

        if target_channel is not None:
            await interaction.response.send_message(
                f"AI replies are now **OFF** in {target_channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Choose a Reply Channel to turn AI off in that channel.",
                ephemeral=True,
            )


@ai_bot_command.error
async def ai_bot_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You need **Manage Server** permission to change AI chat settings."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    else:
        print(f"/ai-bot command error: {type(error).__name__}: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Could not change AI chat settings. Check the bot logs.", ephemeral=True
            )


@bot.tree.command(name="admin", description="Show the Crystal Hub administration guide")
async def admin_command(interaction: discord.Interaction):
    # Admin access requires BOTH the redeemed admi-on-top code and Discord Administrator permission.
    if not is_guild_administrator(interaction):
        await interaction.response.send_message(
            "❌ You need Discord Administrator permission to use `/admin`.",
            ephemeral=True
        )
        return

    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users):
        await interaction.response.send_message(
            "❌ Redeem the existing `admi-on-top` admin code through Redeem Key before using `/admin`.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Crystal Hub — Admin Guide",
        description=(
            "**Administration Commands**\\n"
            "• `-massage #channel Your message` — Send a message to a channel.\\n"
            "• `-massage CHANNEL_LINK Your message` — Use a channel link or ID instead of a mention.\\n"
            "• `/blacklist @user` — Globally block a user from bot features.\\n"
            "• `/unblacklist user_id` — Remove a user's global blacklist.\\n\\n"
            "**Note:** Only use messaging commands in channels where the bot has permission to send messages."
        ),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(
        embed=embed,
        view=AdminGuideLanguageView(),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )


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


@bot.tree.command(name="list-servers", description="List servers the bot has joined")
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


@bot.tree.command(name="send-server-message", description="Send a message to a channel in a selected server")
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

@bot.tree.command(name="create-redeem-code", description="Create a one-time Crystal Hub website subscription code")
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

@bot.tree.command(name="list-redeem-codes", description="List Crystal Hub website redeem code status")
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

@bot.tree.command(name="revoke-redeem-code", description="Revoke an unused Crystal Hub website redeem code")
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



@bot.event
async def on_message(message: discord.Message):
    """Answer messages in configured AI channels and forward unknown questions to staff."""
    if message.author.bot:
        await bot.process_commands(message)
        return

    try:
        if message.guild is not None and message.channel.id in AI_CHAT_CHANNELS:
            content = (message.content or "").strip()
            if content and not content.startswith(("/", "-", "!")):
                now = asyncio.get_running_loop().time()
                last = _ai_last_reply_by_user.get(message.author.id, 0)
                if now - last >= 2:
                    _ai_last_reply_by_user[message.author.id] = now
                    saved_item, score = _find_saved_answer(content)
                    if saved_item:
                        await message.reply(str(saved_item["answer"])[:1900], mention_author=False)
                    else:
                        faq_kind = _crystal_faq_kind(content)
                        if faq_kind:
                            _crystal_faq_embed._reply_arabic = bool(
                                re.search(r"[\u0600-\u06FF]", content)
                            )
                            embed = _crystal_faq_embed(faq_kind)
                            if embed is not None:
                                await message.reply(embed=embed, mention_author=False)
                        else:
                            history = _AI_CHAT_HISTORY.setdefault(message.channel.id, [])
                            answer = await _crystal_ai_answer(content, history=history)
                            history.append({"role": "user", "content": content[:1000]})
                            if answer:
                                history.append({"role": "assistant", "content": answer[:1500]})
                                del history[:-12]
                                await message.reply(answer[:1900], mention_author=False)
                            elif AI_QUESTION_CHANNEL_ID:
                                staff_channel = bot.get_channel(AI_QUESTION_CHANNEL_ID)
                                if staff_channel is None:
                                    try:
                                        staff_channel = await bot.fetch_channel(AI_QUESTION_CHANNEL_ID)
                                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                                        print(f"Could not access AI question channel: {type(exc).__name__}: {exc}")
                                        staff_channel = None

                                if staff_channel is not None:
                                    question_embed = discord.Embed(
                                        title="New unanswered AI question",
                                        description=content[:3500],
                                        color=discord.Color.orange(),
                                    )
                                    question_embed.add_field(
                                        name="Asked by",
                                        value=f"{message.author.mention} (`{message.author.id}`)",
                                        inline=False,
                                    )
                                    question_embed.add_field(
                                        name="Source channel",
                                        value=f"{message.channel.mention}",
                                        inline=False,
                                    )
                                    await staff_channel.send(
                                        embed=question_embed,
                                        view=AIQuestionReviewView(content[:1000]),
                                        allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                                    )
                                    await message.reply(
                                        "ما عندي إجابة محفوظة لهذا السؤال، أرسلته لفريق الدعم عشان يضيفون الإجابة.",
                                        mention_author=False,
                                    )
                                else:
                                    await message.reply(
                                        "ما قدرت أجاوب، وقناة الأسئلة غير متاحة حاليًا. تأكد من إعداد `/ai-bot` وصلاحيات البوت.",
                                        mention_author=False,
                                    )
                            else:
                                await message.reply(
                                    "ما عندي إجابة لهذا السؤال حتى الآن. تأكد من اختيار Questions Channel في `/ai-bot`.",
                                    mention_author=False,
                                )
    except Exception as exc:
        print(f"AI message handler failed: {type(exc).__name__}: {exc}")

    await bot.process_commands(message)


TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN environment variable is missing!")  
