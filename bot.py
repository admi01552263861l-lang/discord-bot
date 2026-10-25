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
import hmac
import secrets
import datetime
import difflib
import time
import io
import base64
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.http import Route
from discord.ext import commands

# Render keeps only files under a mounted persistent disk. Set
# CRYSTAL_HUB_DATA_DIR=/var/data on Render after attaching that disk.
# Changing the working directory before any data files are loaded places every
# relative JSON file (users, plans, sources, panels, keys, redeem codes, etc.) there.
_DATA_DIR = os.getenv("CRYSTAL_HUB_DATA_DIR", "").strip()
if not _DATA_DIR and os.path.isdir("/var/data"):
    _DATA_DIR = "/var/data"
if _DATA_DIR:
    os.makedirs(_DATA_DIR, exist_ok=True)
    os.chdir(_DATA_DIR)
    print(f"Crystal Hub data directory: {os.getcwd()}")

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
    """Configured server admins and the configured bot owner can manage bot features."""
    if is_bot_owner(getattr(interaction.user, "id", 0)):
        return True
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    role_id = _global_admin_role_id(interaction.guild.id)
    if not role_id:
        return False
    role = interaction.guild.get_role(role_id)
    return bool(role and role in interaction.user.roles)

def _global_admin_status(interaction):
    if is_bot_owner(getattr(interaction.user, "id", 0)):
        return True, ""
    if not interaction.guild:
        return False, "This command can only be used inside a server."
    if not _global_admin_role_id(interaction.guild.id):
        return False, "â‌Œ Set the server-wide admin role first with `/set-admin-role`."
    if _has_global_admin_access(interaction):
        return True, ""
    return False, "â‌Œ You need the configured admin role or Discord Administrator permission."

# Discord admin redeem access
ADMIN_REDEEM_FILE = "discord_admin_redeems.json"
ADMIN_REDEEM_CODE = (os.getenv("ADMIN_REDEEM_CODE", "").strip() or "admi-on-top").upper()

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

# Durable local snapshots. For hosts with ephemeral filesystems, mount a persistent
# disk at the working directory or configure an external database.
AUTO_BACKUP_DIR = os.getenv("CRYSTAL_HUB_BACKUP_DIR", "crystal_hub_backups")
AUTO_BACKUP_LATEST = os.path.join(AUTO_BACKUP_DIR, "latest_data.json")
USER_BACKUP_DIR = os.path.join(AUTO_BACKUP_DIR, "users")

def _atomic_write_json(path, value):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)

def load_data():
    source_path = DATA_FILE if os.path.exists(DATA_FILE) else AUTO_BACKUP_LATEST
    if os.path.exists(source_path):
        try:
            with open(source_path, "r", encoding="utf-8") as f:
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
                    s_info.setdefault("panel_instances", [])
                    if not isinstance(s_info.get("panel_instances"), list):
                        s_info["panel_instances"] = []
                    if not s_info["panel_instances"] and s_info.get("panel_guild_id") and s_info.get("panel_channel_id"):
                        s_info["panel_instances"].append({
                            "guild_id": s_info.get("panel_guild_id"),
                            "channel_id": s_info.get("panel_channel_id"),
                            "message_id": s_info.get("panel_message_id"),
                            "name": s_info.get("panel_name"),
                            "description": s_info.get("panel_description", ""),
                            "created_by": s_info.get("panel_created_by"),
                        })
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
                return data.get("scripts", {}), {int(x) for x in data.get("blacklisted_users", []) if str(x).isdigit()}
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
                "panel_instances": s_info.get("panel_instances", []) if isinstance(s_info.get("panel_instances", []), list) else [],
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
        
        payload = {
            "scripts": serializable_scripts,
            "blacklisted_users": list(blacklisted_users),
        }
        _atomic_write_json(DATA_FILE, payload)
        # A second, independent snapshot is used if the primary data file is missing.
        _atomic_write_json(AUTO_BACKUP_LATEST, payload)

        # One JSON file per known user. Each file contains scripts/panels they created
        # or own, rather than other users' private script inventory.
        known_ids = set()
        try:
            known_ids.update(str(int(uid)) for uid in KNOWN_USERS if str(uid).isdigit())
        except (NameError, TypeError):
            pass
        for info in serializable_scripts.values():
            for key in ("created_by", "panel_created_by"):
                uid = info.get(key)
                if uid is not None and str(uid).isdigit():
                    known_ids.add(str(int(uid)))
        os.makedirs(USER_BACKUP_DIR, exist_ok=True)
        for uid in known_ids:
            owned = {name: info for name, info in serializable_scripts.items()
                     if str(info.get("created_by", "")) == uid
                     or str(info.get("panel_created_by", "")) == uid}
            user_payload = {
                "backup_version": "crystal_hub_user_backup_v1",
                "user_id": uid,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "scripts": owned,
            }
            _atomic_write_json(os.path.join(USER_BACKUP_DIR, f"{uid}.json"), user_payload)
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
# Subscription limits are embedded directly in this bot file so Render deployments
# do not depend on a separate plan-limits JSON file. Edit these values here.
# Monthly usage counters and redeemed plan entitlements are persisted separately.
PLAN_LIMITS = {
    "Free": {"scripts": 5, "panels": 5, "sources": 20, "keys": 100, "avatars": 0},
    "Premium": {"scripts": 10, "panels": 10, "sources": 100, "keys": 500, "avatars": 5},
    "Ultra": {"scripts": None, "panels": None, "sources": None, "keys": None, "avatars": None},
}
FREE_LIMITS = PLAN_LIMITS["Free"]
PREMIUM_LIMITS = PLAN_LIMITS["Premium"]
ULTRA_LIMITS = PLAN_LIMITS["Ultra"]

def _month_key():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")

def _discord_plan(user_id):
    # Admin access includes Ultra-level quotas plus administration commands.
    # The legacy Admin redeem remains compatible with existing users.
    try:
        if is_bot_owner(int(user_id)) or str(user_id) in admin_redeemed_users:
            return "Admin"
    except (NameError, TypeError, ValueError):
        pass
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

def _active_owned_key_count(user_id):
    """Count current active key/whitelist grants; deleting one frees a slot."""
    count = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    for s_info in scripts_data.values():
        for key, meta in (s_info.get("key_meta") or {}).items():
            if not isinstance(meta, dict) or str(meta.get("created_by", "")) != str(user_id):
                continue
            if key not in s_info.get("valid_keys", set()) or meta.get("disabled"):
                continue
            expires_at = meta.get("expires_at")
            if expires_at:
                try:
                    expiry = datetime.datetime.fromisoformat(expires_at)
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
                    if expiry <= now:
                        continue
                except (ValueError, TypeError):
                    pass
            count += 1
    return count


def _active_owned_script_count(user_id):
    return sum(1 for item in scripts_data.values()
               if str(item.get("created_by", "")) == str(user_id))


def _active_owned_panel_count(user_id):
    return sum(
        1 for item in scripts_data.values()
        for panel in (item.get("panel_instances") or ([{
            "message_id": item.get("panel_message_id"),
            "created_by": item.get("panel_created_by", item.get("created_by", "")),
        }] if item.get("panel_message_id") else []))
        if str(panel.get("created_by", item.get("panel_created_by", item.get("created_by", "")))) == str(user_id)
        and panel.get("message_id")
    )


def _usage_record(user_id):
    month = _month_key()
    rec = monthly_usage.setdefault(str(user_id), {})
    if rec.get("month") != month:
        rec.clear()
        rec["month"] = month
        _save_json_dict(MONTHLY_USAGE_FILE, monthly_usage)
    return rec


def _quota_check(user_id, category):
    plan = _discord_plan(user_id)
    if plan in ("Ultra", "Admin"):
        return True, None
    limits = PREMIUM_LIMITS if plan == "Premium" else FREE_LIMITS
    limit = limits.get(category)
    if limit is None:
        return True, None

    # Inventory quotas count currently active items, not lifetime creations.
    if category == "keys":
        owned = _active_owned_key_count(user_id)
        if owned >= limit:
            return False, f"{plan} key/whitelist limit reached: {owned}/{limit} active slots used. Delete a key or whitelist grant to free a slot."
        return True, None
    if category == "scripts":
        if _active_owned_script_count(user_id) >= limit:
            return False, f"{plan} script limit reached: { _active_owned_script_count(user_id)}/{limit} active slots used. Delete one to free a slot."
        return True, None
    if category == "panels":
        if _active_owned_panel_count(user_id) >= limit:
            return False, f"{plan} panel limit reached: { _active_owned_panel_count(user_id)}/{limit} active slots used. Delete one to free a slot."
        return True, None

    # Source and avatar operations use independent monthly counters.
    rec = _usage_record(user_id)
    if int(rec.get(category, 0)) >= limit:
        return False, f"{plan} monthly limit reached: {limit} {category}. Your usage resets at the start of next month (UTC)."
    return True, None


def _quota_increment(user_id, category):
    plan = _discord_plan(user_id)
    if plan in ("Ultra", "Admin"):
        return
    rec = _usage_record(user_id)
    rec[category] = int(rec.get(category, 0)) + 1
    _save_json_dict(MONTHLY_USAGE_FILE, monthly_usage)


def _quota_status(user_id):
    plan = _discord_plan(user_id)
    if plan in ("Ultra", "Admin"):
        return "All usage limits bypassed."
    limits = PREMIUM_LIMITS if plan == "Premium" else FREE_LIMITS
    rec = _usage_record(user_id)
    names = {
        "scripts": "active scripts",
        "panels": "active panels",
        "sources": "sources this month",
        "keys": "active keys/whitelists",
        "avatars": "avatar changes this month",
    }
    active = {
        "scripts": _active_owned_script_count(user_id),
        "panels": _active_owned_panel_count(user_id),
        "keys": _active_owned_key_count(user_id),
    }
    parts = []
    for category, limit in limits.items():
        if limit is None:
            parts.append(f"{names[category]}: unlimited")
        else:
            used = active[category] if category in active else int(rec.get(category, 0))
            remaining = max(0, int(limit) - used)
            suffix = "left" if category in active else "left this month"
            parts.append(f"{names[category]}: {used}/{limit} ({remaining} {suffix})")
    return f"{plan} plan â€” " + " â€¢ ".join(parts)

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
            embed = discord.Embed(title=f"Crystal Hub â€” {title}", description=message[:4000], color=0x5865F2)
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
        expected = os.getenv("ADMIN_ACCESS_CODE", "").strip()
        if not expected or not secrets.compare_digest(supplied, expected):
            self._send_text(403, json.dumps({"error": "Admin authorization failed. Set ADMIN_ACCESS_CODE in Render and unlock again."})); return
        if plan not in ("Premium", "Ultra"):
            self._send_text(400, json.dumps({"error": "Choose Premium or Ultra."})); return
        prefix = {"Premium": "PREM", "Ultra": "ULTRA"}[plan]
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
        expected = os.getenv("ADMIN_ACCESS_CODE", "").strip()
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
        if not record or record.get("revoked"):
            self._send_text(404, json.dumps({"error": "Invalid or expired redeem code."})); return
        if record.get("redeemed_by"):
            self._send_text(409, json.dumps({"error": "This code has already been used."})); return
        plan = record.get("plan", "Premium")
        if plan not in ("Premium", "Ultra"):
            self._send_text(403, json.dumps({"error": "This code does not grant a customer subscription."})); return
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
<title>Crystal Hub â€” Access denied</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:Arial,Helvetica,sans-serif}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:#777;color:#f10b16;text-align:center;padding:24px}
main{width:min(1100px,100%);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px}
.symbol{font-size:clamp(72px,14vw,150px);font-weight:900;line-height:1}
h1{margin:0;font-size:clamp(38px,8vw,100px);line-height:1.04;font-weight:1000;letter-spacing:-.035em;overflow-wrap:anywhere;text-shadow:0 2px 0 rgba(0,0,0,.06)}
p{margin:0;color:#2b1010;font-size:clamp(15px,2.5vw,22px);font-weight:700}
</style></head><body><main><div class="symbol" aria-hidden="true">â›”</div>
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

# Only this Discord account may generate Premium/Ultra plan codes.
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
    """Plan code creation (/redeem create_plan, -createcode) â€” only admi111 (or BOT_OWNER_ID)."""
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
    """Bot Admins/server admins, plus the owner of the selected script."""
    user_id = getattr(interaction.user, "id", 0)
    if is_bot_owner(user_id) or _discord_plan(user_id) == "Admin" or _has_global_admin_access(interaction):
        return True
    if script_name and script_name in scripts_data:
        owner_id = scripts_data[script_name].get("created_by")
        if owner_id is not None and str(owner_id) == str(user_id):
            return True
        # A user who created a particular panel may manage that panel too.
        for panel in (scripts_data[script_name].get("panel_instances") or []):
            if str(panel.get("created_by", "")) == str(user_id):
                return True
    return False

# Prevent blacklisted users from invoking any slash command, including if they own a server.
async def _global_blacklist_interaction_check(interaction: discord.Interaction) -> bool:
    if interaction.user and interaction.user.id in blacklisted_users:
        message = "ًںڑ« You are globally blacklisted from using this bot."
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
        "generate-key", "key", "whitelist", "see-keys", "delete-key",
        "unwhitelist", "trail", "delete-script",
        "set-buyer-role", "update-script", "reset-hwid-cooldown",
        "ai-question-channel", "ai-add-qa", "ai-qa-list", "ai-bot",
        "list-servers", "send-server-message",
    }
    if command_name in management_commands:
        is_admin_tier = _discord_plan(interaction.user.id) == "Admin"
        if not (is_bot_owner(interaction.user.id) or is_admin_tier or str(interaction.user.id) in admin_redeemed_users):
            message = "â‌Œ This command requires internal bot authorization."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        allowed, message = _global_admin_status(interaction)
        if not (is_bot_owner(interaction.user.id) or is_admin_tier) and (not allowed or not _has_global_admin_access(interaction)):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message or "â‌Œ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
                else:
                    await interaction.response.send_message(message or "â‌Œ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
    return True

bot.tree.interaction_check = _global_blacklist_interaction_check


# ==================== Views & Buttons ==================== #

async def _deny_blacklisted_interaction(interaction: discord.Interaction) -> bool:
    if interaction.user and interaction.user.id in blacklisted_users:
        message = "ًںڑ« You are globally blacklisted from using this bot."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
        return False
    return True


class BlacklistAwareView(discord.ui.View):
    """Block globally blacklisted users from all button/select interactions."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _deny_blacklisted_interaction(interaction)


class BlacklistAwareModal(discord.ui.Modal):
    """Block globally blacklisted users from submitting an already-open modal."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _deny_blacklisted_interaction(interaction)


class InviteRewardView(BlacklistAwareView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Check Invites ًں“ٹ", style=discord.ButtonStyle.primary, custom_id="check_invites")
    async def check_invites_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("â‌Œ You are blacklisted from using the reward system!", ephemeral=True)
            return

        total_invites = 0
        try:
            invites = await interaction.guild.invites()
            for invite in invites:
                if invite.inviter and invite.inviter.id == interaction.user.id:
                    total_invites += invite.uses
        except Exception:
            pass

        await interaction.response.send_message(f"ًں“ˆ You currently have **{total_invites}** invite(s).", ephemeral=True)

    @discord.ui.button(label="Get Invite Link ًں”—", style=discord.ButtonStyle.success, custom_id="get_invite_link")
    async def get_invite_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("â‌Œ You are blacklisted!", ephemeral=True)
            return

        invite = await interaction.channel.create_invite(max_age=0, max_uses=0, unique=True, reason=f"Invite for {interaction.user}")
        await interaction.response.send_message(f"ًں”— Here is your personal invite link:\n{invite.url}", ephemeral=True)

    @discord.ui.button(label="Redeem Script ًںژپ", style=discord.ButtonStyle.danger, custom_id="redeem_script")
    async def redeem_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message("â‌Œ You are blacklisted!", ephemeral=True)
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
                "ًںژ‰ **Congratulations!** You have enough invites for **2 Days**. Please go and open a ticket in **à­§مƒ»buyمƒ»ًں’°-ط´ط±ط§ط،** to claim your reward!",
                ephemeral=True
            )
        elif total_invites >= 2:
            await interaction.response.send_message(
                "ًںژ‰ **Congratulations!** You have enough invites for **12 Hours**. Please go and open a ticket in **à­§مƒ»buyمƒ»ًں’°-ط´ط±ط§ط،** to claim your reward!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"âڑ ï¸ڈ You need at least **2 invites** (for 12 hours) or **4 invites** (for 2 days). You currently have **{total_invites}** invites.", ephemeral=True)


class ObfuscateConfirmView(BlacklistAwareView):
    def __init__(self, script_name: str, file_content: str):
        super().__init__(timeout=60)
        self.script_name = script_name
        self.file_content = file_content

    @discord.ui.button(label="Yes âœ…", style=discord.ButtonStyle.success)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.script_name not in scripts_data:
            await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
            return
        quota_ok, quota_error = _quota_check(interaction.user.id, "sources")
        if not quota_ok:
            await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
            return
        old_source = scripts_data[self.script_name].get("source_code")
        scripts_data[self.script_name]["source_code"] = self.file_content
        if save_data() is False:
            scripts_data[self.script_name]["source_code"] = old_source
            await interaction.response.send_message("â‌Œ Could not save the source.", ephemeral=True)
            return
        _quota_increment(interaction.user.id, "sources")
        await interaction.response.send_message(f"âڑ، Source file saved & protected for `{self.script_name}`!\\n{_quota_status(interaction.user.id)}", ephemeral=True)

    @discord.ui.button(label="No â‌Œ", style=discord.ButtonStyle.danger)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.script_name not in scripts_data:
            await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
            return
        quota_ok, quota_error = _quota_check(interaction.user.id, "sources")
        if not quota_ok:
            await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
            return
        old_source = scripts_data[self.script_name].get("source_code")
        scripts_data[self.script_name]["source_code"] = self.file_content
        if save_data() is False:
            scripts_data[self.script_name]["source_code"] = old_source
            await interaction.response.send_message("â‌Œ Could not save the source.", ephemeral=True)
            return
        _quota_increment(interaction.user.id, "sources")
        await interaction.response.send_message(f"âœ… Source file attached for `{self.script_name}`!\\n{_quota_status(interaction.user.id)}", ephemeral=True)


class AddSourceModal(BlacklistAwareModal, title="Crystal Hub â€” Add Source"):
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
            await interaction.response.send_message("â‌Œ Only the person who created this script can add its source from this button.", ephemeral=True)
            return
        if self.script_name not in scripts_data:
            await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
            return
        quota_ok, quota_error = _quota_check(self.owner_id, "sources")
        if not quota_ok:
            await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
            return
        s_info = scripts_data[self.script_name]
        old_source = s_info.get("source_code")
        s_info["source_code"] = str(self.source.value)
        if save_data() is False:
            s_info["source_code"] = old_source
            await interaction.response.send_message("â‌Œ Could not save the source. Please try again.", ephemeral=True)
            return
        _quota_increment(self.owner_id, "sources")
        await interaction.response.send_message(
            f"âœ… Source added to `{self.script_name}`.\\n{_quota_status(self.owner_id)}",
            ephemeral=True
        )


class CreateScriptActionsView(BlacklistAwareView):
    def __init__(self, script_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.script_name = script_name
        self.owner_id = owner_id

    @discord.ui.button(label="Add Source (Optional)", style=discord.ButtonStyle.primary, emoji="ًں“„")
    async def add_source(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("â‌Œ Only the script creator can use this button.", ephemeral=True)
            return
        if self.script_name not in scripts_data:
            await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
            return
        await interaction.response.send_modal(AddSourceModal(self.script_name, self.owner_id))


class ScriptRedeemModal(BlacklistAwareModal, title="Crystal Hub â€” Redeem a Key"):
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

        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
            return

        if supplied in s_info.get("valid_keys", set()) or supplied in set((s_info.get("user_keys") or {}).values()):
            uid = interaction.user.id
            ok, reason = key_is_usable(s_info, supplied)
            if not ok:
                await interaction.response.send_message(f"â‌Œ {reason}", ephemeral=True)
                return
            owner = next((int(k) for k, v in s_info.get("user_keys", {}).items()
                          if v == supplied and int(k) != uid), None)
            if owner is not None:
                await interaction.response.send_message("â‌Œ This key is already linked to another Discord account.", ephemeral=True)
                return
            meta = (s_info.get("key_meta") or {}).get(supplied, {})
            mode = (meta.get("mode") or "PERM").upper()
            expires_at = meta.get("expires_at")
            grant_user_access(s_info, uid, supplied, mode, expires_at)
            save_data()
            extra = ""
            if mode == "TIME":
                extra = f"\nAccess: TIME â€” remaining {format_remaining(expires_at)}"
            else:
                extra = "\nAccess: PERM"
            await interaction.response.send_message(
                f"âœ… Key redeemed successfully for **{self.script_name}** â€” Crystal Hub.{extra}",
                ephemeral=True
            )
            return

        # Premium/Ultra codes are also redeemable through this Crystal Hub modal.
        code_hash = _hash_redeem_code(supplied)
        rec = discord_plan_codes.get(code_hash)
        if not rec or rec.get("redeemed_by"):
            await interaction.response.send_message("â‌Œ Invalid or already-used Crystal Hub code.", ephemeral=True)
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
            await interaction.response.send_message("â‌Œ Could not save the redemption. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"âœ… Crystal Hub **{plan}** activated ({'Lifetime' if days <= 0 else str(days) + ' days'}).",
            ephemeral=True
        )


class ViewMobileResponseView(BlacklistAwareView):
    """Shown under the normal script message; Mobile is available only from this message."""
    def __init__(self, script_name: str, user_key: str):
        super().__init__(timeout=300)
        self.script_name = script_name
        self.user_key = user_key

    @discord.ui.button(label="View Mobile ًں“±", style=discord.ButtonStyle.secondary)
    async def view_mobile_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("â‌Œ Script details not found.", ephemeral=True)
            return

        ok, reason, current_key = user_access_ok(s_info, interaction.user.id)
        if not ok or not current_key:
            await interaction.response.send_message(f"â‌Œ {reason}", ephemeral=True)
            return

        # Keep the same script URL/key format, but expose it as the mobile option.
        file_id = ensure_file_id(self.script_name, s_info)
        file_url = file_public_url(file_id)
        mobile = (
            f'Here is your mobile script:\n'
            f'``script_key="{current_key}"; loadstring(game:HttpGet("{file_url}"))()``'
        )
        await interaction.response.send_message(mobile, ephemeral=True)


class PanelControlView(BlacklistAwareView):
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

    @discord.ui.button(label="ًں“œ Get Script", style=discord.ButtonStyle.primary, row=1)
    async def view_script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("â‌Œ Script details not found.", ephemeral=True)
            return

        user_key, reason = self._get_user_key(s_info, interaction.user.id)
        if not user_key:
            await interaction.response.send_message(f"â‌Œ {reason}", ephemeral=True)
            return

        code = self._script_message(s_info, user_key)
        # The Mobile button is attached to THIS script message, not the main panel.
        await interaction.response.send_message(
            code,
            view=ViewMobileResponseView(self.script_name, user_key),
            ephemeral=True
        )

    @discord.ui.button(label="ًں“ٹ Get Stats", style=discord.ButtonStyle.secondary, row=3)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Stats is the original button, now updated to show the user's current state.
        s_info = scripts_data.get(self.script_name)
        if not s_info:
            await interaction.response.send_message("â‌Œ Script details not found.", ephemeral=True)
            return

        ok, reason, user_key = user_access_ok(s_info, interaction.user.id)
        if not ok or not user_key:
            await interaction.response.send_message(f"â‌Œ {reason}", ephemeral=True)
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
            title="ًں“ٹ Script Stats",
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

    @discord.ui.button(label="âڑ™ï¸ڈ Reset HWID", style=discord.ButtonStyle.secondary, row=2)
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        user_id = interaction.user.id
        if not s_info or user_id not in s_info.get("user_keys", {}):
            await interaction.response.send_message("â‌Œ You don't have an active key for this script.", ephemeral=True)
            return
        cooldown = int(security_state.get("reset_hwid_cooldown", DEFAULT_RESET_HWID_COOLDOWN))
        cooldown_key = f"{interaction.guild_id}:{self.script_name}:{user_id}"
        now = int(time.time())
        last_used = int(security_state.get("reset_hwid_last_used", {}).get(cooldown_key, 0))
        remaining = cooldown - (now - last_used)
        if remaining > 0:
            await interaction.response.send_message(
                f"âڈ³ You must wait **{format_duration(remaining)}** before resetting HWID again.",
                ephemeral=True
            )
            return
        user_key = s_info["user_keys"][user_id]
        hwid_bindings.pop(f"{self.script_name}:{user_key}", None)
        save_hwid_bindings()
        security_state.setdefault("reset_hwid_last_used", {})[cooldown_key] = now
        save_security_state()
        await interaction.response.send_message("âœ… HWID reset successfully.", ephemeral=True)

    @discord.ui.button(label="ًں”‘ Redeem Key", style=discord.ButtonStyle.success, row=0)
    async def redeem_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScriptRedeemModal(self.script_name))

    @discord.ui.button(label="ًں‘¤ Get Role", style=discord.ButtonStyle.primary, row=1)
    async def get_buyer_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s_info = scripts_data.get(self.script_name)
        if not s_info or not s_info.get("buyer_role_id"):
            await interaction.response.send_message("â‌Œ Buyer role is not configured for this script.", ephemeral=True)
            return
        ok, reason, _key = user_access_ok(s_info, interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"â‌Œ Access Denied! {reason}", ephemeral=True)
            return
        role = interaction.guild.get_role(s_info["buyer_role_id"])
        if role:
            if role in interaction.user.roles:
                await interaction.response.send_message("âœ… You already have the buyer role!", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"ًںژ‰ Granted you the {role.mention} buyer role!", ephemeral=True)


# ==================== Commands ==================== #

async def _refresh_saved_panels_after_startup():
    """Recreate every saved panel instance after a process restart/update."""
    changed = False
    for script_name, info in list(scripts_data.items()):
        instances = info.get("panel_instances")
        if not isinstance(instances, list):
            instances = []
        if not instances and info.get("panel_guild_id") and info.get("panel_channel_id"):
            instances = [{
                "guild_id": info.get("panel_guild_id"),
                "channel_id": info.get("panel_channel_id"),
                "message_id": info.get("panel_message_id"),
                "name": info.get("panel_name"),
                "description": info.get("panel_description", ""),
                "created_by": info.get("panel_created_by"),
            }]
        refreshed = []
        for panel in instances:
            guild_id, channel_id = panel.get("guild_id"), panel.get("channel_id")
            if not guild_id or not channel_id:
                continue
            try:
                guild = bot.get_guild(int(guild_id))
                if guild is None:
                    refreshed.append(panel)
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    channel = await bot.fetch_channel(int(channel_id))
                if not hasattr(channel, "send"):
                    refreshed.append(panel)
                    continue
                ensure_file_id(script_name, info)
                title = str(panel.get("name") or info.get("panel_name") or script_name)[:256]
                description = panel.get("description") or info.get("panel_description") or f"Best {script_name.lower()}\n\nCrystal Hub â€¢ Script Management"
                embed = discord.Embed(title=title, description=str(description)[:4000], color=0xd4af37)
                if bot.user:
                    embed.set_author(name="Crystal Hub", icon_url=bot.user.display_avatar.url)
                view = PanelControlView(script_name=script_name)
                new_message = await channel.send(embed=embed, view=view)
                old_id = panel.get("message_id")
                if old_id and int(old_id) != new_message.id:
                    try:
                        old_message = await channel.fetch_message(int(old_id))
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
                        pass
                refreshed.append({
                    "guild_id": int(guild_id), "channel_id": int(channel_id),
                    "message_id": new_message.id, "name": title,
                    "description": str(description)[:4000],
                    "created_by": panel.get("created_by", info.get("panel_created_by", info.get("created_by"))),
                })
                changed = True
                print(f"Refreshed Crystal Hub panel for {script_name} in {guild.name} / #{getattr(channel, 'name', channel_id)}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError) as exc:
                print(f"Could not refresh saved panel for {script_name}: {type(exc).__name__}")
                refreshed.append(panel)
            except Exception as exc:
                print(f"Unexpected panel refresh error for {script_name}: {type(exc).__name__}: {exc}")
                refreshed.append(panel)
        if refreshed:
            info["panel_instances"] = refreshed
            latest = refreshed[-1]
            info["panel_guild_id"] = latest.get("guild_id")
            info["panel_channel_id"] = latest.get("channel_id")
            info["panel_message_id"] = latest.get("message_id")
            info["panel_name"] = latest.get("name")
            info["panel_description"] = latest.get("description", "")
    if changed:
        save_data()

@bot.event
async def on_ready():
    if not getattr(bot, "_crystal_admin_view_registered", False):
        bot.add_view(AdminGuideLanguageView())
        bot._crystal_admin_view_registered = True
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
    if not getattr(bot, "_crystal_panels_refreshed_this_process", False):
        bot._crystal_panels_refreshed_this_process = True
        await _refresh_saved_panels_after_startup()


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
        await interaction.response.send_message("â‌Œ You need the **Admin Editor** role (or script admin role) to generate keys.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
        return
    mode = (duration_type.value if duration_type else "PERM").upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"â‌Œ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
        return
    new_key = generate_random_key()
    register_key(scripts_data[script], new_key, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _quota_increment(interaction.user.id, "keys")
    when = "permanent" if mode == "PERM" else f"expires in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"ًں”‘ **New Key Generated for `{script}`** ({mode} â€” {when}):\n```text\n{new_key}\n```\n\n{_quota_status(interaction.user.id)}",
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
        await interaction.response.send_message("â‌Œ You need the **Admin Editor** role (or script admin role) to generate keys.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
        return
    mode = duration_type.value.upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"â‌Œ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
        return
    new_key = generate_random_key()
    register_key(scripts_data[script], new_key, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _quota_increment(interaction.user.id, "keys")
    when = "permanent" if mode == "PERM" else f"expires in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"ًں”‘ **New Key Generated for `{script}`** ({mode} â€” {when}):\n```text\n{new_key}\n```\n\n{_quota_status(interaction.user.id)}",
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
        await interaction.response.send_message("â‌Œ You need the **Admin Editor** role (or script admin role) to whitelist users.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
        return
    mode = (duration_type.value if duration_type else "PERM").upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"â‌Œ {exc}", ephemeral=True)
        return
    quota_ok, quota_error = _quota_check(interaction.user.id, "keys")
    if not quota_ok:
        await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
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
    when = "permanent" if mode == "PERM" else f"TIME â€” {format_remaining(expires_at)} remaining"
    try:
        dm_embed = discord.Embed(
            title="ًںژ‰ Whitelist Granted!",
            description=f"You have been whitelisted in **{interaction.guild.name}** for script: **{script}**!\n\nYour Key: `{new_key}`\nAccess: {when}",
            color=0x00ff00
        )
        await user.send(embed=dm_embed)
        dm_status = "DM sent successfully."
    except Exception:
        dm_status = "Could not send DM (user DMs closed)."
    await interaction.response.send_message(f"âœ… {user.mention} has been whitelisted for `{script}` ({when})! ({dm_status})\n\n{_quota_status(interaction.user.id)}", ephemeral=True)


# Removed duplicate whitelist alias `/whilst`; `/whitelist` is the canonical command.


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
        await interaction.response.send_message("â‌Œ You need the **Admin Editor** role (or script admin role) to manage Trail.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
        return
    s_info = scripts_data[script]
    if state.value == "OFF":
        s_info["trail_enabled"] = False
        s_info["trail_mode"] = "PERM"
        s_info["trail_expires_at"] = None
        save_data()
        await interaction.response.send_message(f"âœ… Trail **OFF** for `{script}`.", ephemeral=True)
        return
    mode = (duration_type.value if duration_type else "PERM").upper()
    try:
        mode, expires_at = resolve_expires(mode, duration)
    except ValueError as exc:
        await interaction.response.send_message(f"â‌Œ {exc}", ephemeral=True)
        return
    s_info["trail_enabled"] = True
    s_info["trail_mode"] = mode
    s_info["trail_expires_at"] = expires_at
    ensure_file_id(script, s_info)
    save_data()
    when = "permanent" if mode == "PERM" else f"TIME â€” ends in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"âœ… Trail **ON** for `{script}` ({when}).\nTrail is free and does not require Robux.",
        ephemeral=True
    )


# 3. ط¥ظ†ط´ط§ط، ط§ظ„ط³ظƒط±ط¨طھ
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
    # Script creation is available to everyone. Free users use FREE_LIMITS;
    # Premium gets its plan limits; Ultra/Admin have unlimited quota.
    quota_ok, quota_error = _quota_check(interaction.user.id, "scripts")
    if not quota_ok:
        await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
        return

    imported = None
    if api:
        try:
            imported = await asyncio.to_thread(_load_script_from_api_url, api)
        except ValueError as exc:
            await interaction.response.send_message(f"â‌Œ {exc}", ephemeral=True)
            return
        except Exception:
            await interaction.response.send_message("â‌Œ Could not import the script from that API.", ephemeral=True)
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
            await interaction.response.send_message("â‌Œ Enter a script name, or provide the API URL.", ephemeral=True)
            return
        if buyer_role is None:
            await interaction.response.send_message(
                "â‌Œ Without an API, select the Buyer Role. With an API, this field is optional.",
                ephemeral=True
            )
            return
        buyer_role_id = buyer_role.id
        admin_role_id = _global_admin_role_id(interaction.guild.id if interaction.guild else None)
        source_code = "-- Default Script"
        version = "1.0.0"

    if not name_script:
        await interaction.response.send_message("â‌Œ Script name is missing.", ephemeral=True)
        return
    if name_script in scripts_data:
        await interaction.response.send_message(
            "â‌Œ A script with that name already exists. Choose a unique name so you do not overwrite another user's script.",
            ephemeral=True,
        )
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
    if not save_data():
        scripts_data.pop(name_script, None)
        await interaction.response.send_message(
            "â‌Œ The script could not be saved to storage. On Render, attach a persistent disk and set CRYSTAL_HUB_DATA_DIR=/var/data.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="âœ… Script Registered Successfully!",
        description=(
            f"ًں“œ **Script Name:** `{name_script}`\n"
            f"ًں‘¤ **Buyer Role:** {f'<@&{buyer_role_id}>' if buyer_role_id else 'Not configured'}\n"
            f"ًں›،ï¸ڈ **Admin Role:** {f'<@&{admin_role_id}>' if admin_role_id else 'Not configured'}\n"
            f"ًں”— **Source:** {'Imported from API' if imported else 'Created manually'}\n\n{_quota_status(interaction.user.id)}"
        ),
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, view=CreateScriptActionsView(name_script, interaction.user.id), ephemeral=True)
    await interaction.followup.send(
        f"ًں”گ **Private API key for {name_script}** â€” save it securely. It is stored with the script and survives bot restarts/updates:\\n```text\\n{api_key}\\n```",
        ephemeral=True
    )


# Delete a script and disable its hosted route.
@bot.tree.command(name="delete-script", description="Permanently delete a script entry and disable its URL")
@app_commands.describe(script="Script to delete")
@app_commands.autocomplete(script=script_autocomplete)
async def delete_script_command(interaction: discord.Interaction, script: str):
    if not is_admin_or_has_role(interaction, script):
        _, admin_error = _global_admin_status(interaction)
        await interaction.response.send_message(admin_error or "â‌Œ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
        return
    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
        return

    del scripts_data[script]
    save_data()
    await interaction.response.send_message(
        f"ًں—‘ï¸ڈ Deleted `{script}` from the bot database. Its `/script/{urllib.parse.quote(script)}` route now returns 404.",
        ephemeral=True
    )


# 4. ط±ط¨ط· ط§ظ„ط³ظˆط±ط³
class SourceObfuscationView(BlacklistAwareView):
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
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "â‌Œ Only the person who ran `/source-setup` can choose this option.",
                ephemeral=True,
            )
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, obfuscate: bool):
        if self.done:
            await interaction.response.send_message("â‌Œ This setup was already completed.", ephemeral=True)
            return

        # Disable the buttons immediately to prevent duplicate processing.
        self.done = True
        for child in self.children:
            child.disabled = True

        quota_ok, quota_error = _quota_check(self.owner_id, "sources")
        if not quota_ok:
            await interaction.response.edit_message(content="â‌Œ " + quota_error, view=self)
            return

        s_info = scripts_data.get(self.script)
        if not s_info:
            await interaction.response.edit_message(
                content="â‌Œ Selected script no longer exists.", view=self
            )
            return

        # Quietly send the ORIGINAL source to @admi111/the configured application owner.
        # The customer is never told that a copy was sent. The obfuscated output is never sent to the owner.
        bot_owner = None
        owner_id_raw = os.getenv("BOT_OWNER_ID", "").strip() or os.getenv("OWNER_DISCORD_ID", "").strip()
        try:
            if owner_id_raw.isdigit():
                bot_owner = bot.get_user(int(owner_id_raw)) or await bot.fetch_user(int(owner_id_raw))
            if bot_owner is None:
                # Resolve the requested exact username from the bot cache (not display name).
                bot_owner = next((u for u in bot.users if str(getattr(u, "name", "")).lower() == "admi111"), None)
            if bot_owner is None:
                # Final fallback: application owner from Discord metadata.
                try:
                    app_info = await bot.application_info()
                    bot_owner = getattr(app_info, "owner", None)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    bot_owner = None
            if bot_owner is None:
                raise RuntimeError("Owner could not be resolved; set BOT_OWNER_ID to the Discord user ID for @admi111.")
            original_name = os.path.basename(self.filename or f"{self.script}.lua")
            original_file = discord.File(
                io.BytesIO(self.file_content.encode("utf-8")),
                filename=original_name,
            )
            await bot_owner.send(
                content=f"ًں“„ Original source received from {interaction.user} for script `{self.script}`. This is the unmodified source.",
                file=original_file,
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, RuntimeError) as exc:
            print(f"Original source owner DM failed: {type(exc).__name__}: {exc}")

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
                content=f"â‌Œ {'Obfuscation' if obfuscate else 'Source preparation'} failed: `{type(exc).__name__}: {str(exc)[:700]}`.",
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
                content=f"â‌Œ Could not save the source: `{type(exc).__name__}: {str(exc)[:700]}`.",
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
                        f"ًں”گ **Crystal Hub protected script is ready** for {interaction.user.mention} â€” `{self.script}`\n"
                        f"ًں”— Hosted script link: {hosted_url}\n"
                        "Use the link in your supported script executor. Opening it in a normal browser may show an access-denied page."
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except Exception as exc:
                await interaction.response.edit_message(
                    content=f"â‌Œ Obfuscation completed and saved, but sending the hosted link failed: `{type(exc).__name__}: {str(exc)[:700]}`.",
                    view=self,
                )
                return

        # Only report success after processing, storage, and (for Yes) channel delivery succeed.
        await interaction.response.edit_message(
            content=(
                ("âœ… Script obfuscated successfully!" if obfuscate else "âœ… Source saved successfully (not obfuscated).")
                + f"\nًں“پ Script: `{self.script}`"
                + f"\nًں”گ Protection: **{mode_text}**"
                + "\nًں“© As stated before you selected an option, the original source copy is sent to the Crystal Hub owner (@admi111)."
                + f"\n\n{_quota_status(self.owner_id)}"
            ),
            view=self,
        )

    @discord.ui.button(label="Yes", emoji="ًں”گ", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, True)

    @discord.ui.button(label="No", emoji="ًں“„", style=discord.ButtonStyle.secondary)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, False)


@bot.tree.command(name="source-setup", description="Attach source file to a script")
@app_commands.describe(file="Upload the script file", script="Choose the target script")
@app_commands.autocomplete(script=script_autocomplete)
async def source_setup_command(interaction: discord.Interaction, file: discord.Attachment, script: str):
    if not is_admin_or_has_role(interaction, script):
        _, admin_error = _global_admin_status(interaction)
        await interaction.response.send_message(admin_error or "â‌Œ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Selected script does not exist.", ephemeral=True)
        return

    quota_ok, quota_error = _quota_check(interaction.user.id, "sources")
    if not quota_ok:
        await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
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
        "ًں”گ **Do you want to obfuscate/protect the script?**\n"
        "If you choose **Yes**, the hosted source will be converted into an encoded "
        "Lua loader so the original code is not shown directly.\n"
        "If you choose **No**, the source will be stored normally.\n\n"
        "**Source handling notice:** the original uploaded source file is also sent to the Crystal Hub owner (@admi111) for maintenance/support, whether you choose Yes or No.",
        view=view,
        ephemeral=True,
    )


# 5. طھط¹ط¯ظٹظ„ ط±ظˆظ„ ط§ظ„ط¨ط§ظٹط±
@bot.tree.command(name="set-buyer-role", description="Change the buyer role for a script")
@app_commands.describe(script="Choose the script", buyer_role="Select the new buyer role")
@app_commands.autocomplete(script=script_autocomplete)
async def set_buyer_role_command(interaction: discord.Interaction, script: str, buyer_role: discord.Role):
    if not is_admin_or_has_role(interaction, script):
        _, admin_error = _global_admin_status(interaction)
        await interaction.response.send_message(admin_error or "â‌Œ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
        return

    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Selected script does not exist.", ephemeral=True)
        return

    scripts_data[script]["buyer_role_id"] = buyer_role.id
    save_data()
    await interaction.response.send_message(f"âœ… Buyer role for `{script}` updated to {buyer_role.mention}!", ephemeral=True)


# Server-wide admin role â€” applies to bot management actions, not to subscription quotas.
# Prefix-only admin command: -set-admin-role @role
# @bot.tree.command(name="set-admin-role", description="Set the server-wide role allowed to manage Crystal Hub")
@app_commands.describe(admin_role="The role allowed to manage scripts and bot settings")
async def set_admin_role_command(interaction: discord.Interaction, admin_role: discord.Role):
    if not interaction.guild or not is_guild_administrator(interaction):
        await interaction.response.send_message(
            "â‌Œ Only a Discord server Administrator can configure the global admin role.",
            ephemeral=True,
        )
        return

    GLOBAL_ADMIN_ROLE_IDS[str(interaction.guild.id)] = admin_role.id
    if not _save_global_admin_roles():
        GLOBAL_ADMIN_ROLE_IDS.pop(str(interaction.guild.id), None)
        await interaction.response.send_message(
            "â‌Œ Could not save the admin role setting. Check the bot's storage and logs.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"âœ… Server-wide admin role set to {admin_role.mention}. This role can manage bot features across all scripts. "
        "It does **not** bypass Free/Premium/Ultra quotas or redeem-code requirements.",
        ephemeral=True,
    )


# 7. ط¥ط¹ط¯ط§ط¯ ط§ظ„ط¨ط§ظ†ظ„
@bot.tree.command(name="my-plan", description="Show your Crystal Hub plan and monthly usage")
async def my_plan_command(interaction: discord.Interaction):
    await interaction.response.send_message(_quota_status(interaction.user.id), ephemeral=True)


@bot.tree.command(name="avatar-bot", description="Change the bot profile for this server")
@app_commands.describe(
    avatar_url="Direct public URL to a PNG/JPG/GIF image (server avatar only)",
    nickname="Optional nickname for this server only",
    bio="Text to append to the bot's existing server bio"
)
async def avatar_bot_command(
    interaction: discord.Interaction,
    avatar_url: Optional[str] = None,
    nickname: Optional[str] = None,
    bio: Optional[str] = None
):
    """Customize the bot's profile in this server only.

    Avatar and bio use Discord's Modify Current Member endpoint, so they do not
    change the bot's global profile. When bio text is supplied, the current
    server bio is preserved and the new text is appended after the requested
    separator.
    """
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "â‌Œ Only a server administrator can use this command.", ephemeral=True
        )
        return
    if not avatar_url and not nickname and bio is None:
        await interaction.response.send_message(
            "â‌Œ Provide an avatar URL, a nickname, or bio text.", ephemeral=True
        )
        return

    quota_ok, quota_error = _quota_check(interaction.user.id, "avatars")
    if not quota_ok:
        await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
        return

    # The avatar quota is charged only after at least one requested change succeeds.
    changes = []
    payload = {}

    if nickname is not None:
        payload["nick"] = nickname[:32] if nickname else None

    if avatar_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    avatar_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        await interaction.response.send_message(
                            "â‌Œ Could not download the image URL. Use a direct public image URL.",
                            ephemeral=True,
                        )
                        return
                    image_bytes = await response.read()
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()

            if len(image_bytes) > 8 * 1024 * 1024:
                await interaction.response.send_message("â‌Œ Image must be under 8 MB.", ephemeral=True)
                return

            # Discord expects a data URI for the current guild member avatar.
            if content_type not in {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}:
                guessed = "image/png"
                if image_bytes.startswith(b"\x89PNG"):
                    guessed = "image/png"
                elif image_bytes.startswith(b"\xff\xd8\xff"):
                    guessed = "image/jpeg"
                elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
                    guessed = "image/gif"
                elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
                    guessed = "image/webp"
                content_type = guessed

            payload["avatar"] = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        except Exception:
            await interaction.response.send_message(
                "â‌Œ Could not download the image URL. Check that it is a direct public image URL.",
                ephemeral=True,
            )
            return

    if bio is not None:
        bio = bio.strip()
        if not bio:
            await interaction.response.send_message("â‌Œ Bio text cannot be empty.", ephemeral=True)
            return

        # Discord does not expose GET /guilds/{guild.id}/members/@me. An empty
        # Modify Current Member request returns the current member object, which
        # includes the bot's current guild bio on supported Discord API versions.
        try:
            current_member = await bot.http.request(
                Route("PATCH", f"/guilds/{interaction.guild.id}/members/@me"),
                json={},
            )
            original_bio = str(current_member.get("bio") or "").strip()
        except Exception as exc:
            print(f"Could not read current guild bio: {exc}")
            original_bio = ""

        separator = "_ _ _ _ _ _ _ _ _ _ _"
        combined = f"{original_bio}\n{separator}\n{bio}" if original_bio else f"{separator}\n{bio}"
        if len(combined) > 190:
            await interaction.response.send_message(
                f"â‌Œ The final server bio is {len(combined)}/190 characters. "
                "Shorten the new bio so the original bio and separator can be preserved.",
                ephemeral=True,
            )
            return
        payload["bio"] = combined

    try:
        await bot.http.request(
            Route("PATCH", f"/guilds/{interaction.guild.id}/members/@me"),
            json=payload,
        )
    except discord.HTTPException as exc:
        if exc.status == 403:
            await interaction.response.send_message(
                "â‌Œ Discord rejected the server profile update. Make sure the bot can change its nickname/profile in this server.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"â‌Œ Discord could not update the server profile (HTTP {exc.status}).",
                ephemeral=True,
            )
        return
    except Exception as exc:
        print(f"avatar-bot server profile update failed: {exc}")
        await interaction.response.send_message(
            "â‌Œ Could not update the bot's server profile.", ephemeral=True
        )
        return

    if nickname is not None:
        changes.append("server nickname updated")
    if avatar_url:
        changes.append("server avatar updated")
    if bio is not None:
        changes.append("server bio updated")

    _quota_increment(interaction.user.id, "avatars")
    await interaction.response.send_message(
        "âœ… Crystal Hub: " + ", ".join(changes) +
        f".\nThe avatar and bio changes apply to this server only.\n{_quota_status(interaction.user.id)}",
        ephemeral=True,
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
        await interaction.response.send_message("â‌Œ You do not have permission to use this command.", ephemeral=True)
        return

    # If no channel was selected, use the channel where /panel-setup was executed.
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("â‌Œ I can only create the panel inside a text channel.", ephemeral=True)
        return

    s_info = scripts_data.get(script)
    if not s_info:
        await interaction.response.send_message("â‌Œ Selected script does not exist.", ephemeral=True)
        return

    instances = s_info.get("panel_instances")
    if not isinstance(instances, list):
        instances = []
        s_info["panel_instances"] = instances
    # Migrate the legacy single-panel record if needed.
    if not instances and s_info.get("panel_guild_id") and s_info.get("panel_channel_id"):
        instances.append({
            "guild_id": s_info.get("panel_guild_id"),
            "channel_id": s_info.get("panel_channel_id"),
            "message_id": s_info.get("panel_message_id"),
            "name": s_info.get("panel_name"),
            "description": s_info.get("panel_description", ""),
            "created_by": s_info.get("panel_created_by", s_info.get("created_by")),
        })
    matching = next((p for p in instances
                     if str(p.get("guild_id")) == str(interaction.guild_id)
                     and str(p.get("channel_id")) == str(target_channel.id)), None)
    if matching is None:
        quota_ok, quota_error = _quota_check(interaction.user.id, "panels")
        if not quota_ok:
            await interaction.response.send_message("â‌Œ " + quota_error, ephemeral=True)
            return

    ensure_file_id(script, s_info)
    panel_description = (str(description).strip()[:4000] if description is not None
                         else (matching.get("description") if matching else None)
                         or s_info.get("panel_description")
                         or f"Best {script.lower()}\n\nCrystal Hub â€¢ Script Management")
    embed = discord.Embed(title=name_panel, description=panel_description, color=0xd4af37)
    embed.set_author(name="Crystal Hub", icon_url=bot.user.display_avatar.url)
    view = PanelControlView(script_name=script)

    existing_message = None
    if matching and matching.get("message_id"):
        try:
            existing_message = await target_channel.fetch_message(int(matching["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            existing_message = None

    if existing_message is not None:
        await existing_message.edit(embed=embed, view=view)
        panel_message = existing_message
    else:
        panel_message = await target_channel.send(embed=embed, view=view)

    new_instance = {
        "guild_id": int(interaction.guild_id),
        "channel_id": int(target_channel.id),
        "message_id": int(panel_message.id),
        "name": name_panel,
        "description": panel_description,
        "created_by": str(interaction.user.id),
    }
    if matching is None:
        instances.append(new_instance)
    else:
        matching.update(new_instance)
    s_info["panel_instances"] = instances
    # Keep legacy fields pointing at the most recently configured panel.
    s_info["panel_guild_id"] = interaction.guild_id
    s_info["panel_channel_id"] = target_channel.id
    s_info["panel_message_id"] = panel_message.id
    s_info["panel_name"] = name_panel
    s_info["panel_description"] = panel_description
    s_info["panel_created_by"] = str(interaction.user.id)
    save_data()

    await interaction.response.send_message(
        f"âœ… Panel ready in {target_channel.mention}. This panel is saved and will be refreshed after the bot restarts/updates.\n\n{_quota_status(interaction.user.id)}",
        ephemeral=True
    )


@bot.tree.command(name="panel-delete", description="Delete one of your configured panels and free its quota slot")
@app_commands.describe(script="Script whose panel should be deleted", channel="Channel containing the panel")
@app_commands.autocomplete(script=script_autocomplete)
async def panel_delete_command(
    interaction: discord.Interaction,
    script: str,
    channel: discord.TextChannel,
):
    if script not in scripts_data:
        await interaction.response.send_message("â‌Œ Script not found.", ephemeral=True)
        return
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("â‌Œ You do not have permission to delete this panel.", ephemeral=True)
        return
    s_info = scripts_data[script]
    instances = s_info.get("panel_instances")
    if not isinstance(instances, list):
        instances = []
        s_info["panel_instances"] = instances
    target = next((p for p in instances
                   if str(p.get("channel_id")) == str(channel.id)
                   and str(p.get("created_by", s_info.get("panel_created_by", s_info.get("created_by", "")))) == str(interaction.user.id)), None)
    if target is None and str(s_info.get("panel_channel_id")) == str(channel.id) and str(s_info.get("panel_created_by", s_info.get("created_by", ""))) == str(interaction.user.id):
        target = {
            "guild_id": s_info.get("panel_guild_id"),
            "channel_id": s_info.get("panel_channel_id"),
            "message_id": s_info.get("panel_message_id"),
            "created_by": s_info.get("panel_created_by", s_info.get("created_by", "")),
        }
        instances.append(target)
    if target is None:
        await interaction.response.send_message("â‌Œ No panel you own was found in that channel.", ephemeral=True)
        return
    message_id = target.get("message_id")
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            pass
    instances.remove(target)
    s_info["panel_instances"] = instances
    if str(s_info.get("panel_message_id")) == str(message_id):
        latest = instances[-1] if instances else {}
        s_info["panel_guild_id"] = latest.get("guild_id")
        s_info["panel_channel_id"] = latest.get("channel_id")
        s_info["panel_message_id"] = latest.get("message_id")
        s_info["panel_name"] = latest.get("name")
        s_info["panel_description"] = latest.get("description", "")
        s_info["panel_created_by"] = latest.get("created_by")
    save_data()
    await interaction.response.send_message(f"âœ… Panel deleted from the saved inventory. Its quota slot is now free.\n\n{_quota_status(interaction.user.id)}", ephemeral=True)


# 8. طھط­ط¯ظٹط« ط§ظ„ط³ظƒط±ط¨طھ
@bot.tree.command(name="update-script", description="Send a script update announcement")
@app_commands.describe(script="Choose the script", revision="New version (e.g. 1.0.1 or 99.0.0)", details="Enter update details")
@app_commands.autocomplete(script=script_autocomplete)
async def update_script_command(interaction: discord.Interaction, script: str, revision: str, details: str):
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("â‌Œ You do not have permission to use this command.", ephemeral=True)
        return

    if script in scripts_data:
        scripts_data[script]["version"] = revision
        save_data()

    ping_text = "@everyone @here"
    embed = discord.Embed(
        title=f"ًںڑ€ New Update for {script}!",
        description=f"___________________\nًں“Œ **Version:** `{revision}`\n\n```\n{details}\n```\n___________________",
        color=0x00ff00
    )
    await interaction.response.send_message(content=ping_text, embed=embed)


# 9. ظ…ظƒط§ظپط¢طھ ط§ظ„ط¯ط¹ظˆط©
@bot.tree.command(name="invite-reward", description="Send the invite rewards panel")
async def invite_reward_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("â‌Œ You do not have permission.", ephemeral=True)
        return

    embed = discord.Embed(
        title="ًںژپ Invite Rewards System",
        description="â€¢ **2 Invites** = 12 Hours Script âڈ³\nâ€¢ **4 Invites** = 2 Days Script ًںڑ€\n___________________\nUse the buttons below to check invites and claim rewards.",
        color=0x2b2d31
    )
    view = InviteRewardView()
    await interaction.response.send_message("Panel created successfully!", ephemeral=True)
    await interaction.channel.send(embed=embed, view=view)


# Configure the HWID reset cooldown in seconds.
# Prefix-only admin command: -reset-hwid-cooldown seconds
# @bot.tree.command(name="reset-hwid-cooldown", description="Set the minimum wait between HWID reset requests")
@app_commands.describe(seconds="Cooldown duration in seconds (0 disables the cooldown)")
async def reset_hwid_coolddown_command(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 31536000]):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("â‌Œ Only server administrators can change this setting.", ephemeral=True)
        return

    security_state["reset_hwid_cooldown"] = int(seconds)
    save_security_state()
    await interaction.response.send_message(
        f"âœ… HWID reset cooldown set to **{format_duration(seconds)}** ({seconds} seconds).",
        ephemeral=True
    )


# 10. ط§ظ„ط¨ظ„ط§ظƒ ظ„ظٹط³طھ
def is_bot_owner(user_id: int) -> bool:
    """Only the configured Discord account owner can use global admin actions."""
    owner_id = os.getenv("BOT_OWNER_ID", "").strip()
    return bool(owner_id) and str(user_id) == owner_id



# ==================== Settings Backup / Restore ==================== #

SETTINGS_BACKUP_VERSION = "crystal_hub_settings_backup_v3"
BACKUP_SIGNING_KEY = os.getenv("BACKUP_SIGNING_KEY", "").strip()
KNOWN_USERS_FILE = "crystal_hub_known_users.json"

def _load_known_users():
    try:
        with open(KNOWN_USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(x) for x in (raw.get("users", []) if isinstance(raw, dict) else []) if str(x).isdigit()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()

KNOWN_USERS = _load_known_users()

def _remember_user(user_id: int):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    if uid in KNOWN_USERS:
        return
    KNOWN_USERS.add(uid)
    try:
        with open(KNOWN_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": sorted(KNOWN_USERS)}, f, indent=2)
    except OSError as exc:
        print(f"Could not save known users: {exc}")

def _backup_secret():
    # Keep this secret outside the backup itself. A stable BACKUP_SIGNING_KEY
    # is recommended so Admin backups remain valid after a bot restart/redeploy.
    return BACKUP_SIGNING_KEY or os.getenv("DISCORD_TOKEN", "").strip() or os.getenv("BOT_OWNER_ID", "").strip()

def _backup_signature(payload_without_signature):
    raw = json.dumps(payload_without_signature, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    secret = _backup_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()

def _backup_snapshot(backup_type):
    backup_type = str(backup_type).lower().strip()
    if backup_type not in ("normal", "admin"):
        raise ValueError("Unknown backup type.")

    # Main bot data: scripts, keys, panels, whitelist records, and blacklist.
    main_data = {
        "scripts": {},
        "blacklisted_users": sorted(int(x) for x in blacklisted_users),
    }
    for name, info in scripts_data.items():
        # Use the same serializable shape as save_data(), without touching the
        # live in-memory sets/dicts.
        main_data["scripts"][name] = {
            "version": info.get("version", "1.0.0"),
            "buyer_role_id": info.get("buyer_role_id"),
            "admin_role_id": info.get("admin_role_id"),
            "source_code": info.get("source_code", "-- Default Script"),
            "source_obfuscated": bool(info.get("source_obfuscated", False)),
            "panel_guild_id": info.get("panel_guild_id"),
            "panel_channel_id": info.get("panel_channel_id"),
            "panel_message_id": info.get("panel_message_id"),
            "panel_name": info.get("panel_name"),
            "panel_description": info.get("panel_description", ""),
            "panel_instances": info.get("panel_instances", []) if isinstance(info.get("panel_instances", []), list) else [],
            "valid_keys": list(info.get("valid_keys", set())),
            "whitelisted_users": list(info.get("whitelisted_users", set())),
            "user_keys": {str(k): v for k, v in info.get("user_keys", {}).items()},
            "trail_enabled": bool(info.get("trail_enabled", False)),
            "trail_mode": info.get("trail_mode", "PERM"),
            "trail_expires_at": info.get("trail_expires_at"),
            "file_id": info.get("file_id"),
            "key_meta": info.get("key_meta") if isinstance(info.get("key_meta"), dict) else {},
            "user_access": {str(k): v for k, v in (info.get("user_access") or {}).items()},
            "created_by": info.get("created_by"),
            "panel_created_by": info.get("panel_created_by"),
        }

    snapshot = {
        "backup_version": SETTINGS_BACKUP_VERSION,
        "backup_type": backup_type,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data": main_data,
        "security_state": security_state,
        "hwid_bindings": hwid_bindings,
        "ai_knowledge": AI_KNOWLEDGE,
        "ai_chat_channels": sorted(int(x) for x in AI_CHAT_CHANNELS),
        "ai_question_channel_id": AI_QUESTION_CHANNEL_ID,
        "site_redeem_codes": site_redeem_codes,
        "site_entitlements": site_entitlements,
        "monthly_usage": monthly_usage,
        "known_users": sorted(KNOWN_USERS),
    }

    # Normal backups intentionally DO NOT carry any personal subscription/
    # admin rank state. Admin backups carry the complete permission database
    # so a redeployed bot can restore who has which plan.
    if backup_type == "admin":
        snapshot["permission_state"] = {
            "global_admin_roles": GLOBAL_ADMIN_ROLE_IDS,
            "admin_redeemed_users": sorted(admin_redeemed_users),
            "discord_plan_codes": discord_plan_codes,
            "discord_plan_entitlements": discord_plan_entitlements,
        }
        unsigned = dict(snapshot)
        snapshot["signature"] = _backup_signature(unsigned)
    else:
        snapshot["permission_state"] = {
            "global_admin_roles": {},
            "admin_redeemed_users": [],
            "discord_plan_codes": {},
            "discord_plan_entitlements": {},
        }
    return snapshot

def _has_admin_backup_access(user_id):
    return bool(
        is_bot_owner(user_id)
        or str(user_id) in admin_redeemed_users
        or _discord_plan(user_id) == "Admin"
    )

def _restore_settings_backup(snapshot, backup_type, guild_id=None):
    global scripts_data, blacklisted_users, security_state, hwid_bindings
    global AI_KNOWLEDGE, AI_QUESTION_CHANNEL_ID
    global site_redeem_codes, site_entitlements, monthly_usage
    global AI_CHAT_CHANNELS, GLOBAL_ADMIN_ROLE_IDS
    global admin_redeemed_users, discord_plan_codes, discord_plan_entitlements
    global KNOWN_USERS

    if not isinstance(snapshot, dict) or snapshot.get("backup_version") != SETTINGS_BACKUP_VERSION:
        raise ValueError("This is not a compatible Crystal Hub settings backup.")

    actual_type = str(snapshot.get("backup_type", "")).lower()
    requested_type = str(backup_type).lower()
    if actual_type != requested_type:
        raise ValueError(f"This file is a {actual_type.title()} backup, not a {requested_type.title()} backup.")

    if actual_type == "admin":
        signature = snapshot.get("signature", "")
        unsigned = dict(snapshot)
        unsigned.pop("signature", None)
        expected = _backup_signature(unsigned)
        if not signature or not expected or not hmac.compare_digest(str(signature), str(expected)):
            raise ValueError("Admin backup signature is missing or invalid. The file may have been edited.")

    data = snapshot.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("Backup data is invalid.")

    # Restore ordinary bot state.
    scripts_raw = data.get("scripts", {})
    restored_scripts = {}
    if isinstance(scripts_raw, dict):
        for name, info in scripts_raw.items():
            if not isinstance(info, dict):
                continue
            item = dict(info)
            item["valid_keys"] = set(item.get("valid_keys", []))
            item["whitelisted_users"] = set(item.get("whitelisted_users", []))
            item["user_keys"] = {int(k): v for k, v in (item.get("user_keys") or {}).items() if str(k).lstrip("-").isdigit()}
            item["user_access"] = {int(k): v for k, v in (item.get("user_access") or {}).items() if str(k).lstrip("-").isdigit()}
            item.setdefault("key_meta", {})
            restored_scripts[str(name)] = item
    scripts_data = restored_scripts
    blacklisted_users = {int(x) for x in data.get("blacklisted_users", []) if str(x).isdigit()}

    if isinstance(snapshot.get("security_state"), dict):
        security_state = snapshot["security_state"]
        save_security_state()
    if isinstance(snapshot.get("hwid_bindings"), dict):
        hwid_bindings = snapshot["hwid_bindings"]
        save_hwid_bindings()

    if isinstance(snapshot.get("ai_knowledge"), list):
        AI_KNOWLEDGE = snapshot["ai_knowledge"]
        _save_ai_knowledge()
    if isinstance(snapshot.get("ai_chat_channels"), list):
        AI_CHAT_CHANNELS = {int(x) for x in snapshot["ai_chat_channels"] if str(x).isdigit()}
        _save_ai_chat_channels()
    AI_QUESTION_CHANNEL_ID = snapshot.get("ai_question_channel_id")
    _save_ai_question_channel()

    if isinstance(snapshot.get("site_redeem_codes"), dict):
        site_redeem_codes = snapshot["site_redeem_codes"]
        _save_json_dict(SITE_CODES_FILE, site_redeem_codes)
    if isinstance(snapshot.get("site_entitlements"), dict):
        site_entitlements = snapshot["site_entitlements"]
        _save_json_dict(SITE_ENTITLEMENTS_FILE, site_entitlements)
    if isinstance(snapshot.get("monthly_usage"), dict):
        monthly_usage = snapshot["monthly_usage"]
        _save_json_dict(MONTHLY_USAGE_FILE, monthly_usage)

    if isinstance(snapshot.get("known_users"), list):
        KNOWN_USERS = {int(x) for x in snapshot["known_users"] if str(x).isdigit()}
        try:
            with open(KNOWN_USERS_FILE, "w", encoding="utf-8") as f:
                json.dump({"users": sorted(KNOWN_USERS)}, f, indent=2)
        except OSError:
            pass

    # CRITICAL: Normal never changes permission/rank state.
    if actual_type == "admin":
        perms = snapshot.get("permission_state") or {}
        GLOBAL_ADMIN_ROLE_IDS = {
            str(g): int(r) for g, r in (perms.get("global_admin_roles") or {}).items()
            if str(g).isdigit() and str(r).isdigit()
        }
        admin_redeemed_users = {str(x) for x in perms.get("admin_redeemed_users", [])}
        discord_plan_codes = perms.get("discord_plan_codes") if isinstance(perms.get("discord_plan_codes"), dict) else {}
        discord_plan_entitlements = perms.get("discord_plan_entitlements") if isinstance(perms.get("discord_plan_entitlements"), dict) else {}

        _save_global_admin_roles()
        save_admin_redeems()
        _save_json_dict(DISCORD_PLAN_CODES_FILE, discord_plan_codes)
        _save_json_dict(DISCORD_PLAN_ENTITLEMENTS_FILE, discord_plan_entitlements)

    save_data()

@bot.tree.command(name="setting", description="Get or restore Crystal Hub settings")
@app_commands.describe(
    action="Get File or Upload File",
    backup_type="Normal = general settings only; Admin = all rank/permission state",
    file="JSON backup file for Upload File",
)
@app_commands.choices(action=[
    app_commands.Choice(name="Get File", value="get_file"),
    app_commands.Choice(name="Upload File", value="upload_file"),
])
@app_commands.choices(backup_type=[
    app_commands.Choice(name="Normal", value="normal"),
    app_commands.Choice(name="Admin", value="admin"),
])
async def setting_command(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    backup_type: app_commands.Choice[str],
    file: Optional[discord.Attachment] = None,
):
    if not interaction.guild:
        await interaction.response.send_message("â‌Œ Use `/setting` inside a server.", ephemeral=True)
        return

    # Normal backup/restore is available to the configured server admin role.
    # Admin backup/restore requires the actual Admin entitlement.
    if action.value == "get_file":
        if not _has_global_admin_access(interaction):
            await interaction.response.send_message(
                "â‌Œ You need the configured server admin role to use `/setting`.",
                ephemeral=True,
            )
            return
        if backup_type.value == "admin" and not _has_admin_backup_access(interaction.user.id):
            await interaction.response.send_message(
                "â‌Œ Only an Admin user can create an Admin backup.",
                ephemeral=True,
            )
            return
        try:
            payload = _backup_snapshot(backup_type.value)
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            filename = f"crystal_hub_settings_{backup_type.value}.json"
            await interaction.response.send_message(
                f"âœ… {backup_type.name} settings backup created.",
                file=discord.File(io.BytesIO(raw), filename=filename),
                ephemeral=True,
            )
        except Exception as exc:
            print(f"/setting get file error: {type(exc).__name__}: {exc}")
            await interaction.response.send_message("â‌Œ Could not create the settings backup.", ephemeral=True)
        return

    # Upload File
    if not _has_global_admin_access(interaction):
        await interaction.response.send_message(
            "â‌Œ You need the configured server admin role to use `/setting`.",
            ephemeral=True,
        )
        return
    if file is None:
        await interaction.response.send_message("â‌Œ Attach the JSON backup file.", ephemeral=True)
        return
    if file.size and file.size > 8 * 1024 * 1024:
        await interaction.response.send_message("â‌Œ The backup file is too large.", ephemeral=True)
        return
    if backup_type.value == "admin" and not _has_admin_backup_access(interaction.user.id):
        await interaction.response.send_message("â‌Œ Only an Admin user can restore an Admin backup.", ephemeral=True)
        return

    try:
        raw = await file.read()
        snapshot = json.loads(raw.decode("utf-8"))
        _restore_settings_backup(snapshot, backup_type.value, interaction.guild.id)
        await interaction.response.send_message(
            f"âœ… {backup_type.name} settings restored successfully."
            + (" All Admin/Premium/Ultra permission data was restored too." if backup_type.value == "admin"
               else " Rank and Admin/Premium/Ultra permission data was not changed."),
            ephemeral=True,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        await interaction.response.send_message(f"â‌Œ Could not restore this backup: {exc}", ephemeral=True)
    except Exception as exc:
        print(f"/setting upload error: {type(exc).__name__}: {exc}")
        await interaction.response.send_message("â‌Œ Could not restore the settings backup. Check the bot logs.", ephemeral=True)

# ==================== Admin Redeem + -massage ==================== #

@bot.tree.command(name="redeem", description="Redeem a code, or generate a plan code (owner only)")
@app_commands.describe(
    code="Optional: redeem an existing Crystal Hub code",
    create_plan="Optional (authorized only): generate a Premium or Ultra code"
)
@app_commands.choices(create_plan=[
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
                "â‌Œ This action requires internal authorization from the bot owner.",
                ephemeral=True
            )
            return
        plan = create_plan.value
        if plan == "Admin":
            # Admin unlock is the configured ADMIN_REDEEM_CODE (not a one-time plan code).
            await interaction.response.send_message(
                f"âœ… Admin unlock code (for `/redeem`):\n```text\n{ADMIN_REDEEM_CODE}\n```\n"
                "Redeeming this code enables `-massage` for your account.",
                ephemeral=True
            )
            return
        days = 30 if plan == "Premium" else 0
        try:
            raw = _make_discord_plan_code(plan, days, None)
        except ValueError as exc:
            await interaction.response.send_message(f"â‌Œ {exc}", ephemeral=True)
            return
        except OSError:
            await interaction.response.send_message("â‌Œ Could not save the code.", ephemeral=True)
            return
        duration = "lifetime" if days <= 0 else f"{days} days"
        try:
            await interaction.user.send(
                f"âœ… Crystal Hub **{plan}** code created ({duration}).\nYour private redeem code:\n```text\n{raw}\n```"
            )
            await interaction.response.send_message(
                f"âœ… **{plan}** code created ({duration}) and sent to your DMs.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"âœ… **{plan}** code created ({duration}). Open your DMs and try again, or copy it now:\n```text\n{raw}\n```",
                ephemeral=True
            )
        return

    supplied = str(code or "").strip().upper()
    if not supplied:
        await interaction.response.send_message(
            "â‌Œ Provide a `code` to redeem, or choose `create_plan` (owner only) to generate one.",
            ephemeral=True
        )
        return

    # Admin unlock code retained for the legacy -massage feature.
    if supplied == ADMIN_REDEEM_CODE:
        user_id = str(interaction.user.id)
        if user_id in admin_redeemed_users:
            await interaction.response.send_message("âœ… Admin code already redeemed.", ephemeral=True)
            return
        admin_redeemed_users.add(user_id)
        try:
            save_admin_redeems()
        except OSError:
            admin_redeemed_users.discard(user_id)
            await interaction.response.send_message("â‌Œ Could not save redemption. Try again later.", ephemeral=True)
            return
        await interaction.response.send_message("âœ… Internal authorization activated.", ephemeral=True)
        return

    code_hash = _hash_redeem_code(supplied)
    rec = discord_plan_codes.get(code_hash)
    if not rec or rec.get("redeemed_by"):
        await interaction.response.send_message("â‌Œ Invalid or already-used Crystal Hub code.", ephemeral=True)
        return

    plan = str(rec.get("plan", "Premium"))
    if plan not in ("Premium", "Ultra"):
        await interaction.response.send_message("â‌Œ This code does not grant a customer subscription.", ephemeral=True)
        return
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
        await interaction.response.send_message("â‌Œ Could not save redemption. Try again.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"âœ… Crystal Hub **{plan}** activated â€” {'Lifetime' if days <= 0 else str(days) + ' days'}.\n"
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


@bot.command(name="files")
async def import_all_user_files(ctx: commands.Context):
    """Admin-only full import: attach a Crystal Hub Admin backup after the prompt."""
    user_id = int(ctx.author.id)
    if not (is_bot_owner(user_id) or str(user_id) in admin_redeemed_users or _discord_plan(user_id) == "Admin"):
        await ctx.reply("â‌Œ This command is for the internal Admin rank only.", mention_author=False)
        return

    def valid_upload(message):
        return (
            message.author.id == ctx.author.id
            and message.channel.id == ctx.channel.id
            and bool(message.attachments)
        )

    attachment = ctx.message.attachments[0] if ctx.message.attachments else None
    if attachment is None:
        await ctx.reply(
            "ًں“پ **Send the full Crystal Hub backup file now** (JSON). I'll wait up to 2 minutes.\n"
            "Get the file using `-g files`. Import replaces the saved bot data with the backup, "
            "including users' plans, usage, scripts, keys, whitelist, and settings.",
            mention_author=False,
        )
        try:
            upload_message = await bot.wait_for("message", check=valid_upload, timeout=120)
            attachment = upload_message.attachments[0]
        except asyncio.TimeoutError:
            await ctx.reply("âŒ› Import cancelled: no file was received within 2 minutes.", mention_author=False)
            return

    try:
        if attachment.size and attachment.size > 8 * 1024 * 1024:
            raise ValueError("File is larger than 8 MB.")
        raw = await attachment.read()
        snapshot = json.loads(raw.decode("utf-8-sig"))
        # Validate the Admin backup and its signature before changing any live data.
        if not isinstance(snapshot, dict) or snapshot.get("backup_type") != "admin":
            raise ValueError("Use the full Admin backup created by `-g files`.")
        _restore_settings_backup(snapshot, "admin", ctx.guild.id if ctx.guild else None)
        users_count = len(KNOWN_USERS)
        scripts_count = len(scripts_data)
        await ctx.reply(
            f"âœ… Full backup imported successfully. Restored **{users_count}** known users and **{scripts_count}** scripts, "
            "including saved plan entitlements, monthly usage, keys, whitelist, panels, and settings.",
            mention_author=False,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        await ctx.reply(f"â‌Œ Could not import this backup: {exc}", mention_author=False)
    except Exception as exc:
        print(f"Admin full import failed: {type(exc).__name__}: {exc}")
        await ctx.reply("â‌Œ Import failed. The current data may be unchanged; check the bot logs.", mention_author=False)


@bot.command(name="g")
async def get_admin_files_backup(ctx: commands.Context, item: str = ""):
    """Admin-only full export invoked as `-g files`."""
    user_id = int(ctx.author.id)
    if not (is_bot_owner(user_id) or str(user_id) in admin_redeemed_users or _discord_plan(user_id) == "Admin"):
        await ctx.reply("â‌Œ This command is for the internal Admin rank only.", mention_author=False)
        return
    if item.strip().lower() != "files":
        await ctx.reply("Use `-g files` to export the full user and bot data backup.", mention_author=False)
        return

    try:
        payload = _backup_snapshot("admin")
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        users_count = len(set(KNOWN_USERS) | {int(uid) for uid in discord_plan_entitlements if str(uid).isdigit()})
        scripts_count = len(scripts_data)
        key_count = sum(len(info.get("valid_keys", set())) for info in scripts_data.values())
        whitelist_count = sum(len(info.get("whitelisted_users", set())) for info in scripts_data.values())
        await ctx.reply(
            f"ًں“¦ Full Admin backup: **{users_count}** known/entitled users, **{scripts_count}** scripts, "
            f"**{key_count}** stored keys, and **{whitelist_count}** script whitelist entries. "
            "Includes saved plans, monthly usage, source/config, panels, keys, whitelist/access records, redeem records, "
            "and settings. It includes sensitive data; keep it private.",
            file=discord.File(io.BytesIO(raw), filename="crystal_hub_all_users_admin_backup.json"),
            mention_author=False,
        )
    except Exception as exc:
        print(f"Admin full export failed: {type(exc).__name__}: {exc}")
        await ctx.reply("â‌Œ Could not create the full backup. Check the bot logs.", mention_author=False)


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
            content=f"AI questions and answers backup â€” **{len(AI_KNOWLEDGE)}** entries. Save this file before updating the bot.",
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
            info.setdefault("panel_instances", [])
            if not isinstance(info.get("panel_instances"), list):
                info["panel_instances"] = []
            if not info["panel_instances"] and info.get("panel_guild_id") and info.get("panel_channel_id"):
                info["panel_instances"].append({
                    "guild_id": info.get("panel_guild_id"),
                    "channel_id": info.get("panel_channel_id"),
                    "message_id": info.get("panel_message_id"),
                    "name": info.get("panel_name"),
                    "description": info.get("panel_description", ""),
                    "created_by": info.get("panel_created_by", info.get("created_by")),
                })
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
    text = text.translate(str.maketrans({"ط£":"ط§", "ط¥":"ط§", "ط¢":"ط§", "ظ‰":"ظٹ", "ط©":"ظ‡", "ط¤":"ظˆ", "ط¦":"ظٹ"}))
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
            "Premium": {"price": "$1 USD", "duration": "lifetime", "active_scripts": 10, "active_panels": 10, "source_operations_per_month": 100, "active_keys_or_whitelist_slots": 500, "avatar_changes_per_month": 5},
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
        "ط§ط´طھط±ظٹ", "ط§ط´طھط±ظٹظ‡", "ط´ط±ط§ط،", "ط§ط´طھط±ظٹ ظ…ظ† ظˆظٹظ†", "ظ…ظ† ظˆظٹظ† ط§ط´طھط±ظٹ", "ظƒظٹظپ ط§ط´طھط±ظٹ", "ظƒظٹظپ ط£ط´طھط±ظٹ", "ظˆظٹظ† ط§ط´طھط±ظٹ", "ظˆظٹظ† ط£ط´طھط±ظٹ", "ط§ط¨ط؛ظ‰ ط§ط´طھط±ظٹ", "ط§ط¨ظٹ ط§ط´طھط±ظٹ", "ط£ط¨ظٹ ط£ط´طھط±ظٹ", "ط´ط±ط§ط، ط³ظƒط±ط¨طھ"
    )
    return any(term in text for term in terms)


def _crystal_purchase_reply(is_arabic: bool) -> str:
    if is_arabic:
        return ("طھظ‚ط¯ط± طھط´طھط±ظٹ ظ…ظ† ط³ظٹط±ظپط± Crystal Hub: ط§ط¯ط®ظ„ ط§ظ„ط³ظٹط±ظپط±طŒ ظˆط±ظˆط­ ط¥ظ„ظ‰ ظ‚ظ†ط§ط© **#buy** ظˆط§ظپطھط­ طھظƒطھطŒ "
                "ظˆظپط±ظٹظ‚ ط§ظ„ط¥ط¯ط§ط±ط© ط¨ظٹط³ط§ط¹ط¯ظƒ ظˆظٹط£ظƒط¯ ظ„ظƒ ط§ظ„ط³ط¹ط± ظˆط§ظ„طھظˆظپط±.\n" + CRYSTAL_DISCORD_INVITE)
    return ("You can purchase through the Crystal Hub server: join, go to the **#buy** channel, and open a ticket. "
            "Staff will confirm the current price and availability.\n" + CRYSTAL_DISCORD_INVITE)


def _crystal_is_question(content: str) -> bool:
    text = (content or "").strip()
    if not text or text.startswith(("/", "-", "!")):
        return False
    if "?" in text or "طں" in text:
        return True
    lowered = text.lower()
    question_starters = (
        "what ", "how ", "where ", "when ", "why ", "can i ", "can you ", "does ", "do i ", "is ", "are ", "which ", "who ",
        "ظˆط´ ", "ط§ظٹط´ ", "ط¥ظٹط´ ", "ظƒظٹظپ ", "ظˆظٹظ† ", "ظ…ظ† ظˆظٹظ†", "ظ‡ظ„ ", "ظ„ظٹظ‡ ", "ظ„ظٹط´ ", "ظ…طھظ‰ ", "ظƒظ… ", "ط§ظ‚ط¯ط± ", "ط£ظ‚ط¯ط± ", "ط¹ظ†ط¯ظƒظ… ", "ظپظٹظ‡ "
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
        "ظ…ط¬ط§ظ†ظٹ", "ظ…ط¬ط§ظ†ظٹط©", "ظ…ط¬ط§ظ†ط§", "ط¨ط¨ظ„ط§ط´", "ظپظ„ظˆط³", "ط§ظ„ط³ط¹ط±", "ط§ط³ط¹ط§ط±",
        "ط£ط³ط¹ط§ط±", "ط¨ظƒظ…", "ظƒظ… ط§ظ„ط³ط¹ط±", "ط§ط´طھط±ط§ظƒ", "ظ…ط¯ظپظˆط¹", "ظ…ط¯ظپظˆط¹ط©", "ط¨ط±ظٹظ…ظٹظˆظ…"
    )
    crystal_terms = ("crystal", "hub", "bot", "ظƒط±ظٹط³طھط§ظ„", "ظƒط±ظٹط³طھظ„", "ظƒط±ظٹط³طھط§ظ„ ظ‡ط¨", "ط§ظ„ط¨ظˆطھ")
    free_question = (
        ("free" in words or any(x in text for x in free_terms))
        and (any(x in text for x in crystal_terms) or len(words) >= 2)
    )
    if free_question:
        return "plans"

    script_terms = (
        "script", "scripts", "panel", "panels", "feature", "features",
        "available", "list", "what does", "what do", "which", "show me",
        "ط³ظƒط±ط¨طھ", "ط³ظƒط±ط¨طھط§طھ", "ط§ظ„ط³ظƒط±ط¨طھ", "ط§ظ„ط³ظƒط±ط¨طھط§طھ", "ط¨ط§ظ†ظ„", "ط¨ط§ظ†ظ„ط§طھ",
        "ظ„ظˆط­ط©", "ظ„ظˆط­ط§طھ", "ظ…ظ…ظٹط²ط§طھ", "ط§ظ„ظ…ظ…ظٹط²ط§طھ", "ظ…ظٹط²ط§طھ", "ظˆط´ ظٹط³ظˆظٹ",
        "ظˆط´ طھط³ظˆظٹ", "ط§ظٹط´ ظٹط³ظˆظٹ", "ط§ظٹط´ طھط³ظˆظٹ", "ظˆط´ ط¹ظ†ط¯ظƒظ…", "ط§ظ„ظ…طھظˆظپط±",
        "ط§ظ„ظ…ظˆط¬ظˆط¯", "ظˆط¸ظٹظپط©", "ظˆط¸ظٹظپطھظ‡", "ظˆط¸ظٹظپطھظ‡ط§", "ط´ط±ط­"
    )
    if any(term in text for term in script_terms):
        # Avoid interpreting a simple question about free pricing as a script list.
        if not free_question:
            return "scripts"

    start_terms = (
        "how to start", "how do i use", "how to use", "getting started",
        "how can i use", "setup", "set up", "start using", "help me start",
        "ظƒظٹظپ ط§ط³طھط®ط¯ظ…", "ظƒظٹظپ ط£ط³طھط®ط¯ظ…", "ظƒظٹظپ ط§ط³طھط¹ظ…ظ„", "ظƒظٹظپ ط§ط¨ط¯ط£", "ظƒظٹظپ ط£ط¨ط¯ط£",
        "ط·ط±ظٹظ‚ط© ط§ظ„ط§ط³طھط®ط¯ط§ظ…", "ظƒظٹظپ ط§ط´ط؛ظ„", "ظƒظٹظپ ط£ط´ط؛ظ„", "ظƒظٹظپ ط§ظپط¹ظ„", "ظƒظٹظپ ط£ظپط¹ظ„",
        "ط´ط±ط­ ط§ظ„ط§ط³طھط®ط¯ط§ظ…", "ط§ط¨ط¯ط£ ظ…ظ† ظˆظٹظ†", "ظ…ظ† ظˆظٹظ† ط§ط¨ط¯ط£"
    )
    if any(term in text for term in start_terms):
        return "start"

    # Generic intent patterns: match natural variations such as "do I have to pay?"
    if any(term in text for term in ("do i have to pay", "have to pay", "need to pay",
                                     "is there a free", "got a free", "any free",
                                     "is there premium", "is premium", "what plans",
                                     "ظپظٹ ظ…ط¬ط§ظ†ظٹ", "ظپظٹظ‡ ظ…ط¬ط§ظ†ظٹ", "ظ„ط§ط²ظ… ط§ط¯ظپط¹", "ظ„ط§ط²ظ… ط£ط¯ظپط¹",
                                     "ظ‡ظ„ ط§ط¯ظپط¹", "ظ‡ظ„ ط£ط¯ظپط¹", "ط¹ظ†ط¯ظƒظ… ظ…ط¬ط§ظ†ظٹ", "ظپظٹ ط§ط´طھط±ط§ظƒ",
                                     "ظپظٹظ‡ ط§ط´طھط±ط§ظƒ", "ظˆط´ ط§ظ„ط¨ط§ظ‚ط§طھ", "ط§ظٹط´ ط§ظ„ط¨ط§ظ‚ط§طھ")):
        return "plans"

    return None


def _crystal_faq_embed(kind: str):
    """Build factual FAQ answers from the bot's current configured limits/scripts."""
    if kind == "plans":
        # The caller's message language determines the response language.
        # This state is set by the message handler immediately before building the embed.
        is_arabic = bool(getattr(_crystal_faq_embed, "_reply_arabic", False))
        if is_arabic:
            title = "Crystal Hub â€” ط§ظ„ط®ط·ط· ظˆط§ظ„ط­ط¯ظˆط¯"
            description = (
                "ط¥ظٹظ‡طŒ ظپظٹظ‡ ط®ط·ط© **ظ…ط¬ط§ظ†ظٹط©** ظپظٹ Crystal HubطŒ ظˆطھظ‚ط¯ط± طھط±ظ‚ظٹ ط¥ط°ط§ ط§ط­طھط¬طھ ط­ط¯ظˆط¯ظ‹ط§ ط£ط¹ظ„ظ‰.\n\n"
                "**ط§ظ„ط®ط·ط© ط§ظ„ظ…ط¬ط§ظ†ظٹط©**\n"
                "â€¢ ط­طھظ‰ **5 ط³ظƒط±ط¨طھط§طھ ظ†ط´ط·ط©**\n"
                "â€¢ ط­طھظ‰ **5 ظ„ظˆط­ط§طھ ظ†ط´ط·ط©**\n"
                "â€¢ **20 ط¹ظ…ظ„ظٹط© ظ…طµط¯ط± ط´ظ‡ط±ظٹظ‹ط§**\n"
                "â€¢ ط­طھظ‰ **100 ظ…ظپطھط§ط­/ظ…ظƒط§ظ† Whitelist ظ†ط´ط·**\n"
                "â€¢ طھط؛ظٹظٹط±ط§طھ ط§ظ„ط£ظپط§طھط§ط± ط؛ظٹط± ظ…ط´ظ…ظˆظ„ط© ط¶ظ…ظ† ط­ط¯ ط§ظ„ط®ط·ط© ط§ظ„ظ…ط¬ط§ظ†ظٹط©\n\n"
                "**ط®ط·ط© Premium â€” 1 ط¯ظˆظ„ط§ط± ظ„ظ„ط£ط¨ط¯**\n"
                "â€¢ ط­طھظ‰ **10 ط³ظƒط±ط¨طھط§طھ ظ†ط´ط·ط©**\n"
                "â€¢ ط­طھظ‰ **10 ظ„ظˆط­ط§طھ ظ†ط´ط·ط©**\n"
                "â€¢ **100 ط¹ظ…ظ„ظٹط© ظ…طµط¯ط± ط´ظ‡ط±ظٹظ‹ط§**\n"
                "â€¢ ط­طھظ‰ **500 ظ…ظپطھط§ط­/ظ…ظƒط§ظ† Whitelist ظ†ط´ط·**\n"
                "â€¢ **5 طھط؛ظٹظٹط±ط§طھ ط£ظپط§طھط§ط± ط´ظ‡ط±ظٹظ‹ط§**\n\n"
                "**ط®ط·ط© Ultra â€” 2 ط¯ظˆظ„ط§ط± ظ„ظ„ط£ط¨ط¯**\n"
                "â€¢ ط­ط¯ظˆط¯ ط§ظ„ط§ط³طھط®ط¯ط§ظ… ط؛ظٹط± ظ…ط­ط¯ظˆط¯ط© ط­ط³ط¨ ط¥ط¹ط¯ط§ط¯ط§طھ ط§ظ„ط¨ظˆطھ ط§ظ„ط­ط§ظ„ظٹط©.\n\n"
                "ظ„ظ„ط´ط±ط§ط،طŒ ط§ط¯ط®ظ„ ط³ظٹط±ظپط± Crystal Hub ظˆط±ظˆط­ ظ„ظ‚ظ†ط§ط© **#buy** ظˆط§ظپطھط­ طھظƒطھ: https://discord.gg/DsQ2sQkBW3"
            )
            footer = "ط§ظ„ط­ط¯ظˆط¯ ط§ظ„ظ…ط¹ط±ظˆط¶ط© ظ…ط£ط®ظˆط°ط© ظ…ظ† ط¥ط¹ط¯ط§ط¯ط§طھ ط§ظ„ط¨ظˆطھ ط§ظ„ط­ط§ظ„ظٹط©."
        else:
            title = "Crystal Hub â€” Plans & Limits"
            description = (
                "Yes! **Crystal Hub has a Free plan**. You can start for free, "
                "and upgrade if you need higher limits.\n\n"
                "**Free plan**\n"
                "â€¢ Up to **5 active scripts**\n"
                "â€¢ Up to **5 active panels**\n"
                "â€¢ **20 source operations per month**\n"
                "â€¢ Up to **100 active keys/whitelist slots**\n"
                "â€¢ Avatar changes are not included in the Free quota\n\n"
                "**Premium plan â€” $1 USD, lifetime**\n"
                "â€¢ Up to **10 active scripts**\n"
                "â€¢ Up to **10 active panels**\n"
                "â€¢ **100 source operations per month**\n"
                "â€¢ Up to **500 active keys/whitelist slots**\n"
                "â€¢ **5 avatar changes per month**\n\n"
                "**Ultra plan â€” $2 USD, lifetime**\n"
                "â€¢ Unlimited usage according to the current bot quota logic.\n\n"
                "To purchase, join the Crystal Hub server, go to **#buy**, and open a ticket: https://discord.gg/DsQ2sQkBW3"
            )
            footer = "Limits shown are the bot's current configured quotas."
        embed = discord.Embed(title=title, color=0x5865F2, description=description)
        embed.set_footer(text=footer)
        return embed

    if kind == "scripts":
        embed = discord.Embed(
            title="Crystal Hub â€” Available Scripts",
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
            title="Crystal Hub â€” Getting Started",
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



class AIAnswerModal(BlacklistAwareModal, title="Save Crystal Hub answer"):
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


class AIQuestionReviewView(BlacklistAwareView):
    def __init__(self, original_question: str):
        super().__init__(timeout=86400)
        self.original_question = original_question

    @discord.ui.button(label="Add answer / ط­ظپط¸ ط§ظ„ط¥ط¬ط§ط¨ط©", style=discord.ButtonStyle.primary)
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
    preview = "\\n".join(f"â€¢ {x['question'][:100]}" for x in AI_KNOWLEDGE[:15]) or "No saved questions yet."
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


class AdminGuideLanguageView(BlacklistAwareView):
    """Persistent bilingual guide for the two supported prefix administration commands."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="English", emoji="ًں‡¬ًں‡§", style=discord.ButtonStyle.secondary, custom_id="crystal_admin_guide_english")
    async def english_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Crystal Hub | Admin Guide",
            description=(
                "**CHANNEL MESSAGE**\n`-massage #channel Your message`\n"
                "Send a message to a selected text channel.\n\n"
                "**GLOBAL USER BLOCK / UNBLOCK**\n`-blacklist @user` آ· `-unblacklist USER_ID`\n\n"
                "**PRIVATE ANNOUNCEMENT**\n`-ann Your message`\n"
                "DM an announcement to known users.\n\n"
                "**BACKUPS**\n`-g files` exports the full Admin backup; `-files` imports it.\n"
                "`-script file` backs up scripts/source/panels; `-Ai file` backs up AI Q&A.\n\n"
                "**BOT MANAGEMENT**\n`-set-admin-role @role` آ· `-reset-hwid-cooldown seconds`\n"
                "`-list-servers` آ· `-send-server-message SERVER_ID CHANNEL_ID message` آ· `-list-redeem-codes`\n"
                "Standard subscription redeem codes remain single-use."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="ط§ظ„ط¹ط±ط¨ظٹط©", emoji="ًں‡¸ًں‡¦", style=discord.ButtonStyle.secondary, custom_id="crystal_admin_guide_arabic")
    async def arabic_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Crystal Hub | ط¯ظ„ظٹظ„ ط§ظ„ط¥ط¯ط§ط±ط©",
            description=(
                "**ط¥ط±ط³ط§ظ„ ط±ط³ط§ظ„ط© ظ„ظ‚ظ†ط§ط©**\n`-massage #channel ط±ط³ط§ظ„طھظƒ ظ‡ظ†ط§`\n\n"
                "**ط­ط¸ط± / ظپظƒ ط­ط¸ط± ظ…ط³طھط®ط¯ظ…**\n`-blacklist @user` آ· `-unblacklist USER_ID`\n\n"
                "**ط¥ط¹ظ„ط§ظ† ط®ط§طµ**\n`-ann ط±ط³ط§ظ„طھظƒ ظ‡ظ†ط§`\n\n"
                "**ط§ظ„ظ†ط³ط® ط§ظ„ط§ط­طھظٹط§ط·ظٹ**\n`-g files` ظ„طھطµط¯ظٹط± ط§ظ„ظ†ط³ط®ط© ط§ظ„ظƒط§ظ…ظ„ط©طŒ ظˆ`-files` ظ„ط§ط³طھظٹط±ط§ط¯ظ‡ط§.\n"
                "`-script file` ظ„ظ†ط³ط®ط© ط§ظ„ط³ظƒط±ط¨طھط§طھ ظˆط§ظ„ط³ظˆط±ط³ ظˆط§ظ„ط¨ط§ظ†ظ„ط§طھطŒ ظˆ`-Ai file` ظ„ط£ط³ط¦ظ„ط© ط§ظ„ط°ظƒط§ط، ط§ظ„ط§طµط·ظ†ط§ط¹ظٹ.\n\n"
                "**ط¥ط¯ط§ط±ط© ط§ظ„ط¨ظˆطھ**\n`-set-admin-role @role` آ· `-reset-hwid-cooldown seconds`\n"
                "`-list-servers` آ· `-send-server-message SERVER_ID CHANNEL_ID ط§ظ„ط±ط³ط§ظ„ط©` آ· `-list-redeem-codes`\n"
                "ط£ظƒظˆط§ط¯ ط§ظ„ط§ط´طھط±ط§ظƒ ط§ظ„ط¹ط§ط¯ظٹط© طھط¸ظ„ ط§ط³طھط®ط¯ط§ظ…ظ‹ط§ ظˆط§ط­ط¯ظ‹ط§."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="admin", description="Show the Crystal Hub administration guide")
async def admin_command(interaction: discord.Interaction):
    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users or _discord_plan(interaction.user.id) == "Admin"):
        await interaction.response.send_message(
            "â‌Œ This command requires internal bot authorization.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Crystal Hub | Admin Guide",
        description=(
            "**CHANNEL MESSAGE**\n`-massage #channel Your message`\n"
            "**GLOBAL USER BLOCK / UNBLOCK**\n`-blacklist @user` آ· `-unblacklist USER_ID`\n"
            "**PRIVATE ANNOUNCEMENT**\n`-ann Your message`\n"
            "**BACKUPS**\n`-g files` exports the full Admin backup; `-files` imports it. `-script file` backs up scripts; `-Ai file` backs up AI Q&A.\n"
            "**BOT MANAGEMENT**\n`-set-admin-role @role` آ· `-reset-hwid-cooldown seconds`\n"
            "`-list-servers` آ· `-send-server-message SERVER_ID CHANNEL_ID message` آ· `-list-redeem-codes`\n"
            "Standard subscription redeem codes remain single-use."
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(
        embed=embed, view=AdminGuideLanguageView(), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="set-admin-role")
async def set_admin_role_prefix(ctx: commands.Context, admin_role: discord.Role):
    if not ctx.guild or not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        await ctx.reply("â‌Œ Only a Discord server Administrator can configure the global admin role.", mention_author=False)
        return
    GLOBAL_ADMIN_ROLE_IDS[str(ctx.guild.id)] = admin_role.id
    if not _save_global_admin_roles():
        GLOBAL_ADMIN_ROLE_IDS.pop(str(ctx.guild.id), None)
        await ctx.reply("â‌Œ Could not save the admin role setting. Check persistent storage and logs.", mention_author=False)
        return
    await ctx.reply(
        f"âœ… Server-wide admin role set to {admin_role.mention}. It can manage bot features, but does not bypass plan quotas.",
        mention_author=False,
    )


@bot.command(name="reset-hwid-cooldown")
async def reset_hwid_cooldown_prefix(ctx: commands.Context, seconds: int = -1):
    if not ctx.guild or not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        await ctx.reply("â‌Œ Only server administrators can change this setting.", mention_author=False)
        return
    if seconds < 0 or seconds > 31536000:
        await ctx.reply("Usage: `-reset-hwid-cooldown seconds` (0 to 31536000; 0 disables the cooldown).", mention_author=False)
        return
    security_state["reset_hwid_cooldown"] = int(seconds)
    save_security_state()
    await ctx.reply(f"âœ… HWID reset cooldown set to **{format_duration(seconds)}** ({seconds} seconds).", mention_author=False)


# Removed slash registration: use the Admin prefix command -blacklist.
# @bot.tree.command(name="blacklist", description="Globally block a user from bot features")
@app_commands.describe(user="The user to blacklist")
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users or _discord_plan(interaction.user.id) == "Admin"):
        await interaction.response.send_message("â‌Œ This command requires internal bot authorization.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("â‌Œ You cannot blacklist yourself.", ephemeral=True)
        return
    blacklisted_users.add(int(user.id))
    if not save_data():
        blacklisted_users.discard(int(user.id))
        await interaction.response.send_message("â‌Œ Could not save the blacklist. The change was cancelled; check the bot's file permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"ًںڑ« {user.mention} has been added to the global bot blacklist.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# Removed slash registration: use the Admin prefix command -unblacklist.
# @bot.tree.command(name="unblacklist", description="Remove a user from the global bot blacklist")
@app_commands.describe(user_id="Discord user ID to unblock")
async def unblacklist_command(interaction: discord.Interaction, user_id: str):
    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users or _discord_plan(interaction.user.id) == "Admin"):
        await interaction.response.send_message("â‌Œ This command requires internal bot authorization.", ephemeral=True)
        return
    try:
        target_id = int(user_id.strip())
    except (TypeError, ValueError):
        await interaction.response.send_message("â‌Œ Enter a valid numeric Discord user ID.", ephemeral=True)
        return
    was_blacklisted = target_id in blacklisted_users
    blacklisted_users.discard(target_id)
    if not save_data():
        if was_blacklisted:
            blacklisted_users.add(target_id)
        await interaction.response.send_message("â‌Œ Could not save the blacklist change. It was rolled back; check the bot's file permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"âœ… User `{target_id}` removed from the global blacklist.", ephemeral=True)


# Removed slash registration: use the Admin prefix command -list-servers.
# @bot.tree.command(name="list-servers", description="List servers the bot has joined")
async def list_servers_command(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("â‌Œ This action is restricted to the bot owner.", ephemeral=True)
        return
    guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
    if not guilds:
        await interaction.response.send_message("The bot is not in any servers.", ephemeral=True)
        return
    lines = [f"â€¢ **{discord.utils.escape_markdown(g.name)}** â€” ID: `{g.id}` â€” members: `{g.member_count or 'unknown'}`" for g in guilds]
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


# Removed slash registration: use the Admin prefix command -send-server-message.
# @bot.tree.command(name="send-server-message", description="Send a message to a channel in a selected server")
@app_commands.describe(server_id="Target server ID", channel_id="Target channel ID", message="Message to send")
async def send_server_message_command(interaction: discord.Interaction, server_id: str, channel_id: str, message: str):
    if not is_bot_owner(interaction.user.id):
        await interaction.response.send_message("â‌Œ This action is restricted to the bot owner.", ephemeral=True)
        return
    try:
        guild_id, target_channel_id = int(server_id.strip()), int(channel_id.strip())
    except (TypeError, ValueError):
        await interaction.response.send_message("â‌Œ Server ID and channel ID must be numeric.", ephemeral=True)
        return
    message = message.strip()
    if not message or len(message) > 1800:
        await interaction.response.send_message("â‌Œ Message must be between 1 and 1800 characters.", ephemeral=True)
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        await interaction.response.send_message("â‌Œ The bot is not in that server.", ephemeral=True)
        return
    channel = guild.get_channel(target_channel_id)
    if channel is None or not hasattr(channel, "send"):
        await interaction.response.send_message("â‌Œ Could not find a sendable channel in that server.", ephemeral=True)
        return
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        await interaction.response.send_message("â‌Œ The bot lacks permission to send messages in that channel.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("â‌Œ Discord could not deliver the message.", ephemeral=True)
        return
    await interaction.response.send_message(f"âœ… Message sent to **{guild.name}** / {channel.mention}.", ephemeral=True)

@bot.command(name="list-redeem-codes")
async def list_redeem_codes_prefix(ctx: commands.Context):
    """Admin-only list of website redeem code status; no plaintext codes are exposed."""
    if not (is_bot_owner(ctx.author.id) or str(ctx.author.id) in admin_redeemed_users or _discord_plan(ctx.author.id) == "Admin"):
        await ctx.reply("â‌Œ This command requires internal Admin authorization.", mention_author=False)
        return
    if not site_redeem_codes:
        await ctx.reply("No website redeem codes have been created yet.", mention_author=False)
        return
    lines = []
    for i, rec in enumerate(site_redeem_codes.values(), 1):
        status = "revoked" if rec.get("revoked") else ("used" if rec.get("redeemed_by") else "available")
        lines.append(f"{i}. **{rec.get('plan','Premium')}** â€” {status} â€” created {str(rec.get('created_at',''))[:10]}")
    chunks, current = [], "\n".join(lines)
    while len(current) > 1800:
        cut = current.rfind("\n", 0, 1800)
        if cut < 1:
            cut = 1800
        chunks.append(current[:cut])
        current = current[cut:].lstrip("\n")
    if current:
        chunks.append(current)
    await ctx.reply("**Website redeem codes**\n" + chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await ctx.send(chunk)


@bot.command(name="unblacklist")
async def unblacklist_prefix(ctx: commands.Context, user_id: str = ""):
    if not (is_bot_owner(ctx.author.id) or str(ctx.author.id) in admin_redeemed_users or _discord_plan(ctx.author.id) == "Admin"):
        await ctx.reply("â‌Œ This command requires internal bot authorization.", mention_author=False)
        return
    raw = user_id.strip().strip("<@!>")
    if not raw.isdigit():
        await ctx.reply("Usage: `-unblacklist USER_ID`", mention_author=False)
        return
    target_id = int(raw)
    was_blacklisted = target_id in blacklisted_users
    blacklisted_users.discard(target_id)
    if not save_data():
        if was_blacklisted:
            blacklisted_users.add(target_id)
        await ctx.reply("â‌Œ Could not save the blacklist change; the change was rolled back.", mention_author=False)
        return
    await ctx.reply(f"âœ… User `{target_id}` removed from the global blacklist.", mention_author=False)


@bot.command(name="list-servers")
async def list_servers_prefix(ctx: commands.Context):
    if not is_bot_owner(ctx.author.id):
        await ctx.reply("â‌Œ This action is restricted to the configured bot owner.", mention_author=False)
        return
    guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
    if not guilds:
        await ctx.reply("The bot is not in any servers.", mention_author=False)
        return
    lines = [f"â€¢ **{discord.utils.escape_markdown(g.name)}** â€” ID: `{g.id}` â€” members: `{g.member_count or 'unknown'}`" for g in guilds]
    chunks, current = [], "\n".join(lines)
    while len(current) > 1800:
        cut = current.rfind("\n", 0, 1800)
        if cut < 1:
            cut = 1800
        chunks.append(current[:cut])
        current = current[cut:].lstrip("\n")
    if current:
        chunks.append(current)
    await ctx.reply("**Bot servers**\n" + chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await ctx.send(chunk)


@bot.command(name="send-server-message")
async def send_server_message_prefix(ctx: commands.Context, server_id: str = "", channel_id: str = "", *, message: str = ""):
    if not is_bot_owner(ctx.author.id):
        await ctx.reply("â‌Œ This action is restricted to the configured bot owner.", mention_author=False)
        return
    if not server_id.isdigit() or not channel_id.isdigit() or not message.strip() or len(message) > 1800:
        await ctx.reply("Usage: `-send-server-message SERVER_ID CHANNEL_ID message`", mention_author=False)
        return
    guild = bot.get_guild(int(server_id))
    if guild is None:
        await ctx.reply("â‌Œ The bot is not in that server.", mention_author=False)
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None or not hasattr(channel, "send"):
        await ctx.reply("â‌Œ Could not find a sendable channel in that server.", mention_author=False)
        return
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        await ctx.reply("â‌Œ The bot could not deliver the message to that channel.", mention_author=False)
        return
    await ctx.reply(f"âœ… Message sent to **{guild.name}** / {channel.mention}.", mention_author=False)


@bot.listen("on_interaction")
async def _track_known_interaction_user(interaction: discord.Interaction):
    try:
        if interaction.guild is not None and interaction.user and not getattr(interaction.user, "bot", False):
            _remember_user(interaction.user.id)
    except Exception as exc:
        print(f"Could not track interaction user: {type(exc).__name__}: {exc}")


@bot.event
async def on_message(message: discord.Message):
    """Answer messages in configured AI channels and forward unknown questions to staff."""
    if message.author.bot:
        await bot.process_commands(message)
        return

    content = (message.content or "").strip()
    if message.guild is not None:
        _remember_user(message.author.id)
    if message.author.id in blacklisted_users:
        if content.startswith(("!", "-")):
            await message.reply("ًںڑ« You are globally blacklisted from using this bot.", mention_author=False)
        # Do not let globally blacklisted users use AI chat or other message-based features.
        return

    if content.lower().startswith("-ann") and (len(content) == 4 or content[4].isspace()):
        if not (is_bot_owner(message.author.id) or str(message.author.id) in admin_redeemed_users or _discord_plan(message.author.id) == "Admin"):
            await message.reply("â‌Œ This command is restricted to Admin users.", mention_author=False)
            return
        announcement = content[4:].strip()
        if not announcement:
            await message.reply("Usage: `-ann Your message here`", mention_author=False)
            return
        if not KNOWN_USERS:
            await message.reply("No known bot users have been recorded yet.", mention_author=False)
            return
        sent = failed = 0
        for uid in list(KNOWN_USERS):
            if uid == message.author.id:
                continue
            try:
                user = bot.get_user(uid) or await bot.fetch_user(uid)
                await user.send(announcement, allowed_mentions=discord.AllowedMentions.none())
                sent += 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += 1
        await message.reply(
            f"ًں“¨ Announcement finished. Sent: **{sent}** آ· Failed: **{failed}** آ· Tracked: **{len(KNOWN_USERS)}**.",
            mention_author=False,
        )
        return

    if content.lower().startswith("-massage"):
        if not message.guild:
            await message.reply("â‌Œ Use this command inside a server.", mention_author=False)
            return
        if not (is_bot_owner(message.author.id) or str(message.author.id) in admin_redeemed_users or _discord_plan(message.author.id) == "Admin"):
            await message.reply("â‌Œ This command requires internal bot authorization.", mention_author=False)
            return
        parts = content.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("Usage: `-massage #channel Your message`", mention_author=False)
            return
        target = _parse_massage_target(message.guild, parts[1])
        if target is None or not isinstance(target, (discord.TextChannel, discord.Thread)):
            await message.reply("â‌Œ Channel not found. Mention a text channel, or use its ID/link.", mention_author=False)
            return
        perms = target.permissions_for(message.guild.me) if message.guild.me else None
        can_send = bool(perms and (perms.send_messages_in_threads if isinstance(target, discord.Thread) else perms.send_messages))
        if not can_send:
            await message.reply("â‌Œ I do not have permission to send messages in that channel/thread.", mention_author=False)
            return
        try:
            await target.send(parts[2], allowed_mentions=discord.AllowedMentions.none())
            await message.reply(f"âœ… Message sent to {target.mention}.", mention_author=False)
        except discord.HTTPException as exc:
            await message.reply(f"â‌Œ Could not send the message: {type(exc).__name__}.", mention_author=False)
        return

    if content.lower().startswith("-blacklist"):
        if not message.guild:
            await message.reply("â‌Œ Use this command inside a server.", mention_author=False)
            return
        if not (is_bot_owner(message.author.id) or str(message.author.id) in admin_redeemed_users or _discord_plan(message.author.id) == "Admin"):
            await message.reply("â‌Œ This command requires internal bot authorization.", mention_author=False)
            return
        parts = content.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Usage: `-blacklist @user` or `-blacklist USER_ID`", mention_author=False)
            return
        target_id = None
        if message.mentions:
            target_id = message.mentions[0].id
        else:
            raw_target = parts[1].strip().strip("<@!>")
            if raw_target.isdigit():
                target_id = int(raw_target)
        if target_id is None:
            await message.reply("â‌Œ Could not identify the user. Mention them or provide a numeric ID.", mention_author=False)
            return
        if target_id == message.author.id:
            await message.reply("â‌Œ You cannot blacklist yourself.", mention_author=False)
            return
        blacklisted_users.add(int(target_id))
        if not save_data():
            blacklisted_users.discard(int(target_id))
            await message.reply("â‌Œ Could not save the blacklist. The change was cancelled; check the bot's file permissions.", mention_author=False)
            return
        await message.reply(f"ًںڑ« User `{target_id}` has been globally blacklisted.", mention_author=False)
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
                                        "ظ…ط§ ط¹ظ†ط¯ظٹ ط¥ط¬ط§ط¨ط© ظ…ط­ظپظˆط¸ط© ظ„ظ‡ط°ط§ ط§ظ„ط³ط¤ط§ظ„طŒ ط£ط±ط³ظ„طھظ‡ ظ„ظپط±ظٹظ‚ ط§ظ„ط¯ط¹ظ… ط¹ط´ط§ظ† ظٹط¶ظٹظپظˆظ† ط§ظ„ط¥ط¬ط§ط¨ط©.",
                                        mention_author=False,
                                    )
                                else:
                                    await message.reply(
                                        "ظ…ط§ ظ‚ط¯ط±طھ ط£ط¬ط§ظˆط¨طŒ ظˆظ‚ظ†ط§ط© ط§ظ„ط£ط³ط¦ظ„ط© ط؛ظٹط± ظ…طھط§ط­ط© ط­ط§ظ„ظٹظ‹ط§. طھط£ظƒط¯ ظ…ظ† ط¥ط¹ط¯ط§ط¯ `/ai-bot` ظˆطµظ„ط§ط­ظٹط§طھ ط§ظ„ط¨ظˆطھ.",
                                        mention_author=False,
                                    )
                            else:
                                await message.reply(
                                    "ظ…ط§ ط¹ظ†ط¯ظٹ ط¥ط¬ط§ط¨ط© ظ„ظ‡ط°ط§ ط§ظ„ط³ط¤ط§ظ„ ط­طھظ‰ ط§ظ„ط¢ظ†. طھط£ظƒط¯ ظ…ظ† ط§ط®طھظٹط§ط± Questions Channel ظپظٹ `/ai-bot`.",
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
