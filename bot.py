# Crystal Hub Bot v4 — Guest redeem + plan quota enforcement
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
from discord.ext import commands, tasks

BUILD_TAG = "CrystalHub-2026-10-30-ACCOUNT-LOGIN-API-V2"

# Render keeps only files under a mounted persistent disk. Set
# CRYSTAL_HUB_DATA_DIR=/var/data on Render after attaching that disk.
# Changing the working directory before any data files are loaded places every
# relative JSON file (users, plans, sources, panels, keys, redeem codes, etc.) there.
_DATA_DIR = os.getenv("CRYSTAL_HUB_DATA_DIR", "").strip()
# Do not auto-select /var/data on Render: an unmounted /var/data path can be
# present but unwritable and crash the bot before Supabase sync starts.
if not _DATA_DIR:
    _DATA_DIR = os.getcwd()
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

def _load_admin_roles_from_supabase():
    try:
        rows=_supabase_rest("crystal_guild_settings",{"select":"guild_id,admin_role_id"})
        for row in rows:
            gid=str(row.get("guild_id") or ""); rid=str(row.get("admin_role_id") or "")
            if gid.isdigit() and rid.isdigit(): GLOBAL_ADMIN_ROLE_IDS[gid]=int(rid)
    except Exception as exc: print(f"Crystal guild role restore warning: {type(exc).__name__}")

def _save_admin_role_to_supabase(guild_id,role_id):
    try:
        _supabase_rest("crystal_guild_settings",{},"POST",{"guild_id":str(guild_id),"admin_role_id":str(role_id),"updated_at":_iso_now()},"resolution=merge-duplicates,return=minimal")
    except Exception as exc: print(f"Crystal guild role save warning: {type(exc).__name__}")

def _global_admin_status(interaction):
    if is_bot_owner(getattr(interaction.user, "id", 0)):
        return True, ""
    if not interaction.guild:
        return False, "This command can only be used inside a server."
    # Either the configured global Admin role OR Discord Administrator permission.
    try:
        if bool(interaction.user.guild_permissions.administrator):
            return True, ""
    except AttributeError:
        pass
    if _has_global_admin_access(interaction):
        return True, ""
    if not _global_admin_role_id(interaction.guild.id):
        return False, "❌ Set the server-wide admin role first with `/admin-role`, or use Discord Administrator permission."
    return False, "❌ You need the configured admin role or Discord Administrator permission."

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
                    s_info.setdefault("loader_code", None)
                    s_info.setdefault("access_mode", "free")
                    s_info.setdefault("disabled", False)
                    s_info.setdefault("webhook_url", "")
                    s_info.setdefault("settings", {})
                    s_info.setdefault("source_name", None)
                    s_info.setdefault("source_path", None)
                    s_info.setdefault("guild_id", None)
                    s_info.setdefault("discord_user_id", None)
                    s_info.setdefault("discord_username", None)
                    s_info.setdefault("guest_id", None)
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
                "api_key": s_info.get("api_key", ""),
                "supabase_user_id": s_info.get("supabase_user_id"),
                "supabase_row_id": s_info.get("supabase_row_id"),
                "fast_mode": bool(s_info.get("fast_mode", False)),
                "obfuscator": bool(s_info.get("obfuscator", False)),
                "ultra_safe": bool(s_info.get("ultra_safe", False)),
                "loader_code": s_info.get("loader_code"),
                "access_mode": s_info.get("access_mode", "free"),
                "disabled": bool(s_info.get("disabled", False)),
                "webhook_url": s_info.get("webhook_url", ""),
                "settings": s_info.get("settings", {}) if isinstance(s_info.get("settings", {}), dict) else {},
                "source_name": s_info.get("source_name"),
                "source_path": s_info.get("source_path"),
                "guild_id": str(s_info.get("guild_id") or "") or None,
                "discord_user_id": str(s_info.get("discord_user_id") or "") or None,
                "discord_username": str(s_info.get("discord_username") or "") or None,
                "guest_id": str(s_info.get("guest_id") or "") or None,
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
    try:
        uid=str(user_id or '').strip()
        if is_bot_owner(int(uid)) or uid in admin_redeemed_users: return "Admin"
        rows=_supabase_rest("crystal_accounts", {"select":"plan,plan_expires_at","discord_user_id":"eq."+urllib.parse.quote(uid,safe=""),"limit":"1"}) if uid else []
        if rows:
            rec=rows[0]; exp=rec.get("plan_expires_at")
            if exp:
                try:
                    if datetime.datetime.fromisoformat(str(exp).replace('Z','+00:00'))<=datetime.datetime.now(datetime.timezone.utc): return "Free"
                except Exception: pass
            p=str(rec.get("plan") or "Free")
            if p.lower() in ("admin","ultra","premium"): return p.title()
    except Exception: pass
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
    try:
        _supabase_rest("crystal_redeem_codes", {}, "POST", {
            "code_hash": code_hash, "plan": plan, "duration_days": int(days),
            "created_by": "discord-owner", "created_at": _iso_now(), "revoked": False
        }, "resolution=merge-duplicates,return=minimal")
    except Exception as exc:
        print(f"Crystal redeem-code Supabase sync warning: {type(exc).__name__}")
    return raw

def _hash_redeem_code(code):
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()

def _verify_supabase_access_token(access_token):
    """Verify identity with Supabase Auth; never trust a user ID sent by the browser."""
    project_url = (os.getenv("CRYSTAL_SUPABASE_URL", "").strip() or os.getenv("SUPABASE_URL", "").strip() or "https://qhxlclqcfncgkglwqwpx.supabase.co").rstrip("/")
    publishable_key = (os.getenv("SUPABASE_KEY", "").strip()
                       or os.getenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_GiQrr4SPvTJJtHjjxisC1w_TfFe7vjq").strip())
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


def _supabase_rest(table: str, query: dict = None, method: str = "GET", payload=None, prefer: str = ""):
    """Server-side Supabase REST helper. The private key is read only from Render env vars."""
    base = (os.getenv("CRYSTAL_SUPABASE_URL", "").strip() or os.getenv("SUPABASE_URL", "").strip() or "https://qhxlclqcfncgkglwqwpx.supabase.co").rstrip("/")
    secret = (os.getenv("SUPA_PRIVATE", "").strip()
              or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())
    if not base or not secret:
        raise RuntimeError("Supabase server credentials are missing (SUPABASE_URL / SUPA_PRIVATE).")
    url = f"{base}/rest/v1/{urllib.parse.quote(table, safe='')}"
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    headers = {"apikey": secret, "Authorization": "Bearer " + secret,
               "Accept": "application/json", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read(2_000_000)
            if not raw:
                return []
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", "replace")
        raise RuntimeError(f"Supabase {table} returned HTTP {exc.code}: {detail}") from exc


def _supabase_upsert_subscription(user_id, plan, expires_at=None, source="dashboard", redeem_code_id=None):
    if not user_id:
        return False
    row = {
        "user_id": str(user_id),
        "plan": str(plan),
        "expires_at": expires_at,
        "source": str(source)[:120],
        "redeem_code_id": redeem_code_id,
        "updated_at": _iso_now(),
    }
    try:
        _supabase_rest("crystal_subscriptions", {}, "POST", row, "resolution=merge-duplicates,return=minimal")
        return True
    except Exception as exc:
        print(f"Crystal subscription sync failed: {type(exc).__name__}: {exc}")
        return False

def _send_script_webhook(s_info, event_type, *, username="", discord_user_id="", key="", device="", country="", duration="", extra=None):
    url = str(s_info.get("webhook_url") or "").strip()
    if not url or not url.startswith(("https://", "http://")):
        return False
    payload = {
        "username": "Crystal Hub",
        "embeds": [{
            "title": f"Crystal Hub — {event_type}",
            "color": 0x5865F2,
            "fields": [
                {"name": "User", "value": str(username or "Unknown")[:1024], "inline": True},
                {"name": "Discord ID", "value": str(discord_user_id or "Unknown")[:1024], "inline": True},
                {"name": "Script", "value": str(s_info.get("name") or "Unknown")[:1024], "inline": True},
                {"name": "Key", "value": str(key or "Unknown")[:1024], "inline": True},
                {"name": "Device", "value": str(device or "Unknown")[:1024], "inline": True},
                {"name": "Country", "value": str(country or "Unknown")[:1024], "inline": True},
                {"name": "Key type", "value": str((s_info.get("key_meta") or {}).get(key, {}).get("mode") or "Unknown")[:1024], "inline": True},
                {"name": "Duration", "value": str(duration or "Unknown")[:1024], "inline": True},
                {"name": "Access", "value": str(s_info.get("access_mode") or "free").upper()[:1024], "inline": True},
                {"name": "Expiration", "value": str(((s_info.get("key_meta") or {}).get(key, {}) or {}).get("expires_at") or "Lifetime")[:1024], "inline": True},
            ],
            "footer": {"text": "Crystal Hub"}
        }]
    }
    if isinstance(extra, dict):
        payload["embeds"][0]["fields"].extend({"name": str(k)[:256], "value": str(v)[:1024], "inline": True} for k,v in extra.items())
    try:
        req=urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as response:
            response.read(256)
        return True
    except Exception as exc:
        print(f"Crystal webhook failed: {type(exc).__name__}")
        return False

def _persist_script_row(row_id, patch):
    if not row_id:
        return False
    try:
        _supabase_rest("crystal_scripts", {"id": "eq." + str(row_id)}, "PATCH", patch, "return=minimal")
        return True
    except Exception as exc:
        print(f"Crystal script persistence failed: {type(exc).__name__}: {exc}")
        return False

def _load_script_from_supabase_api_key(api_key: str) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("Enter a Crystal Hub API key or a full script API URL.")
    try:
        # Only select columns present in the shared schema. Version and role IDs
        # live inside bot_data; selecting them as top-level columns causes HTTP 400
        # on databases where those optional columns were never created.
        # Lookup by API key uses only core columns so optional schema changes
        # (for example user_access) cannot make a valid key look missing.
        rows = _supabase_rest("crystal_scripts", {
            "select": "id,name,api_key,source_code,script,user_id,discord_user_id,discord_username,fast_mode,obfuscator,ultra_safe,bot_data,loader_code,access_mode,disabled,webhook_url,settings,key_meta,source_name,source_path",
            "api_key": "eq." + urllib.parse.quote(key, safe=""), "limit": "1"
        })
    except RuntimeError as exc:
        # Preserve the actual PostgREST error so Render logs/UI reveal whether the
        # secret is invalid, a column is missing, or the table/policy is wrong.
        raise ValueError(f"Supabase API-key lookup failed: {exc}") from exc
    if not rows:
        try:
            retry_rows = _supabase_rest("crystal_scripts", {
                "select": "id,name,api_key,source_code,script,user_id,discord_user_id,bot_data,loader_code,access_mode,disabled,webhook_url,settings,key_meta,source_name,source_path",
                "api_key": "ilike." + urllib.parse.quote(key, safe=""), "limit": "1"
            })
        except RuntimeError:
            retry_rows = []
        if retry_rows:
            rows = retry_rows
    if not rows:
        try:
            _sync_dashboard_scripts_from_supabase()
        except Exception:
            pass
        for local_name, info in scripts_data.items():
            if secrets.compare_digest(str(info.get("api_key", "")).strip(), key):
                return {"name": local_name, "source_code": info.get("source_code", "-- Default Script"),
                        "version": info.get("version", "1.0.0"), "buyer_role_id": info.get("buyer_role_id"),
                        "admin_role_id": info.get("admin_role_id"), "_api_key": key,
                        "_supabase_user_id": info.get("supabase_user_id"), "_supabase_row_id": info.get("supabase_row_id")}
        raise ValueError("API key not found. Paste the exact API Key shown on your Dashboard.")
    row = rows[0]
    bot_data = row.get("bot_data") if isinstance(row.get("bot_data"), dict) else {}
    source = row.get("source_code") or row.get("script") or "-- Default Script"
    access_mode = str(row.get("access_mode") or "free").lower()
    return {"name": row.get("name", "Imported Script"), "source_code": source,
            "version": bot_data.get("version", "1.0.0"), "buyer_role_id": bot_data.get("buyer_role_id"),
            "admin_role_id": bot_data.get("admin_role_id"), "_api_key": key,
            "_supabase_user_id": row.get("user_id"), "_supabase_row_id": row.get("id"),
            "_discord_user_id": row.get("discord_user_id"),
            "loader_code": row.get("loader_code"), "access_mode": access_mode,
            "disabled": bool(row.get("disabled", False)), "webhook_url": row.get("webhook_url") or "",
            "settings": row.get("settings") if isinstance(row.get("settings"), dict) else {},
            "key_meta": row.get("key_meta") if isinstance(row.get("key_meta"), dict) else {},
            "user_access": bot_data.get("user_access") if isinstance(bot_data.get("user_access"), dict) else {},
            "trail_enabled": bool(bot_data.get("trail_enabled", access_mode == "free")),
            "trail_mode": bot_data.get("trail_mode", "PERM"),
            "trail_expires_at": bot_data.get("trail_expires_at"),
            "source_name": row.get("source_name"), "source_path": row.get("source_path")}


def _account_password_hash(password: str) -> str:
    password = str(password or '')
    if len(password) < 6 or len(password) > 256:
        raise ValueError('Password must be between 6 and 256 characters.')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 210000)
    return 'pbkdf2_sha256$210000$' + salt.hex() + '$' + digest.hex()

def _account_password_verify(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = str(stored or '').split('$', 3)
        if algo != 'pbkdf2_sha256':
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac('sha256', str(password or '').encode('utf-8'), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def _account_public(account: dict) -> dict:
    if not isinstance(account, dict):
        return {}
    return {k: account.get(k) for k in ('id','account_type','api_key','guest_id','username','avatar_url','discord_user_id','discord_username') if k in account}

def _account_api_create_account(guest_id: str, username: str, password: str, api_key: str = '', avatar_url: str = '') -> dict:
    gid = str(guest_id or '').strip()[:128]
    uname = str(username or '').strip()[:32]
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', gid):
        raise ValueError('Invalid Guest ID.')
    if not re.fullmatch(r'[A-Za-z0-9_ .-]{3,32}', uname):
        raise ValueError('Username must be 3-32 characters and use letters, numbers, spaces, _, -, or .')
    if len(password or '') < 6:
        raise ValueError('Password must be at least 6 characters.')
    # Reuse an existing Account API for this guest when one exists. This preserves scripts and old API links.
    existing = _account_api_get_by_owner('', gid)
    key = str(api_key or '').strip()
    if key and not re.fullmatch(r'CA-[A-Za-z0-9_-]{12,100}', key):
        raise ValueError('Account API must start with CA-.')
    if existing:
        if key and key != str(existing.get('api_key') or ''):
            raise ValueError('This Guest account already has a different Account API key.')
        key = str(existing.get('api_key') or '')
        patch = {
            'username': uname,
            'password_hash': _account_password_hash(password),
            'avatar_url': str(avatar_url or '')[:500000],
            'updated_at': _iso_now(),
        }
        rows = _supabase_rest('crystal_api_accounts', {'id':'eq.'+str(existing.get('id'))}, 'PATCH', patch, 'return=representation')
        return _account_public({**existing, **patch})
    # Username is unique for named accounts.
    taken = _supabase_rest('crystal_api_accounts', {'select':'id,guest_id','account_type':'eq.account','username':'eq.'+urllib.parse.quote(uname, safe=''),'limit':'1'})
    if taken:
        raise ValueError('That username is already in use.')
    if not key:
        key = _account_api_generate()
    while _supabase_rest('crystal_api_accounts', {'select':'id','api_key':'eq.'+urllib.parse.quote(key,safe=''),'limit':'1'}):
        key = _account_api_generate()
    now = _iso_now()
    payload = {
        'account_type':'account','api_key':key,'guest_id':gid,'username':uname,
        'password_hash':_account_password_hash(password),'avatar_url':str(avatar_url or '')[:500000],
        'linked_at':now,'updated_at':now
    }
    rows = _supabase_rest('crystal_api_accounts', {}, 'POST', payload, 'resolution=merge-duplicates,return=representation')
    return _account_public(rows[0] if rows else payload)

def _account_api_login(api_key: str) -> dict:
    key = str(api_key or '').strip()
    if not re.fullmatch(r'CA-[A-Za-z0-9_-]{12,100}', key):
        raise ValueError('Invalid Account API. It must start with CA-.')
    account = _api_account_get(key)
    if not account:
        raise ValueError('Account API key not found.')
    if str(account.get('account_type') or 'account') != 'account':
        raise ValueError('This is not an Account API key.')
    if not account.get('guest_id'):
        raise ValueError('This Account API is not attached to a Dashboard account yet.')
    return _account_public(account)

def _account_api_generate():
    return 'CA-' + secrets.token_urlsafe(36).replace('-', '').replace('_', '')[:48]


def _account_api_get_by_owner(user_id: str = '', guest_id: str = ''):
    uid = str(user_id or '').strip()
    gid = str(guest_id or '').strip()
    if uid:
        rows = _supabase_rest('crystal_api_accounts', {
            'select': '*', 'account_type': 'eq.account',
            'supabase_user_id': 'eq.' + urllib.parse.quote(uid, safe=''), 'limit': '1'
        })
    elif gid:
        rows = _supabase_rest('crystal_api_accounts', {
            'select': '*', 'account_type': 'eq.account',
            'guest_id': 'eq.' + urllib.parse.quote(gid, safe=''), 'limit': '1'
        })
    else:
        return None
    return rows[0] if rows else None


def _account_api_get_by_discord(discord_user_id: str = ''):
    did = str(discord_user_id or '').strip()
    if not did:
        return None
    rows = _supabase_rest('crystal_api_accounts', {
        'select': '*', 'account_type': 'eq.account',
        'discord_user_id': 'eq.' + urllib.parse.quote(did, safe=''), 'limit': '1'
    })
    return rows[0] if rows else None


def _account_api_get_or_create(user_id: str = '', guest_id: str = '', discord_user_id: str = '', discord_username: str = ''):
    uid = str(user_id or '').strip()
    gid = str(guest_id or '').strip()
    if not uid and not gid:
        raise ValueError('Account identity is required.')
    existing = _account_api_get_by_owner(uid, gid)
    now = _iso_now()
    if existing:
        patch = {'updated_at': now}
        if discord_user_id:
            patch['discord_user_id'] = str(discord_user_id)
            patch['discord_username'] = str(discord_username or '')[:100]
        _supabase_rest('crystal_api_accounts', {'id': 'eq.' + str(existing.get('id'))}, 'PATCH', patch, 'return=minimal')
        return {**existing, **patch}
    key = _account_api_generate()
    while _supabase_rest('crystal_api_accounts', {'select': 'id', 'api_key': 'eq.' + urllib.parse.quote(key, safe=''), 'limit': '1'}):
        key = _account_api_generate()
    payload = {
        'account_type': 'account', 'api_key': key,
        'supabase_user_id': uid or None, 'guest_id': gid or None,
        'discord_user_id': str(discord_user_id or '') or None,
        'discord_username': str(discord_username or '')[:100] or None,
        'linked_at': now, 'updated_at': now,
    }
    rows = _supabase_rest('crystal_api_accounts', {}, 'POST', payload, 'resolution=merge-duplicates,return=representation')
    return rows[0] if rows else payload


def _account_api_reset(user_id: str = '', guest_id: str = ''):
    old = _account_api_get_by_owner(user_id, guest_id)
    if not old:
        return _account_api_get_or_create(user_id, guest_id)
    key = _account_api_generate()
    while _supabase_rest('crystal_api_accounts', {'select': 'id', 'api_key': 'eq.' + urllib.parse.quote(key, safe=''), 'limit': '1'}):
        key = _account_api_generate()
    patch = {'api_key': key, 'discord_user_id': None, 'discord_username': None, 'linked_at': _iso_now(), 'updated_at': _iso_now()}
    rows = _supabase_rest('crystal_api_accounts', {'id': 'eq.' + str(old.get('id'))}, 'PATCH', patch, 'return=representation')
    return rows[0] if rows else {**old, **patch}


def _account_api_link(api_key: str, discord_user_id: str, discord_username: str):
    key = str(api_key or '').strip()
    uid = str(discord_user_id or '').strip()
    if not key or not uid:
        raise ValueError('API key and Discord user are required.')
    rows = _supabase_rest('crystal_api_accounts', {
        'select': '*', 'api_key': 'eq.' + urllib.parse.quote(key, safe=''),
        'account_type': 'eq.account', 'limit': '1'
    })
    if not rows:
        raise ValueError('Account API key not found. Generate it from Dashboard Settings.')
    account = rows[0]
    owner = str(account.get('discord_user_id') or '')
    if owner and owner != uid:
        raise ValueError('This Account API key is already linked to another Discord account.')
    patch = {'discord_user_id': uid, 'discord_username': str(discord_username or '')[:100], 'linked_at': _iso_now(), 'updated_at': _iso_now()}
    updated = _supabase_rest('crystal_api_accounts', {'id': 'eq.' + str(account.get('id'))}, 'PATCH', patch, 'return=representation')
    # Link the Discord identity to the Dashboard account without tying the Account API to a script.
    if account.get('supabase_user_id'):
        _supabase_rest('crystal_discord_users', {}, 'POST', {
            'discord_user_id': uid, 'discord_username': str(discord_username or '')[:100],
            'supabase_user_id': str(account.get('supabase_user_id')),
            'api_key': key, 'plan': _linked_subscription_plan(account.get('supabase_user_id')),
            'updated_at': _iso_now(),
        }, 'resolution=merge-duplicates,return=minimal')
    return updated[0] if updated else {**account, **patch}


def _api_account_get(api_key: str):
    key = str(api_key or '').strip()
    if not key:
        return None
    rows = _supabase_rest('crystal_api_accounts', {
        'select': '*', 'api_key': 'eq.' + urllib.parse.quote(key, safe=''), 'limit': '1'
    })
    return rows[0] if rows else None


def _linked_subscription_plan(user_id):
    """Return the current Dashboard subscription plan for a Supabase user."""
    uid=str(user_id or '').strip()
    if not uid:
        return 'Free'
    try:
        rows=_supabase_rest('crystal_subscriptions', {
            'select':'plan,expires_at,created_at',
            'user_id':'eq.'+urllib.parse.quote(uid,safe=''),
            'order':'created_at.desc',
            'limit':'20'
        })
        now=datetime.datetime.now(datetime.timezone.utc)
        for row in rows:
            exp=row.get('expires_at')
            if exp:
                try:
                    dt=datetime.datetime.fromisoformat(str(exp).replace('Z','+00:00'))
                    if dt <= now:
                        continue
                except Exception:
                    pass
            plan=str(row.get('plan') or 'Free')
            if plan.lower() in ('admin','ultra','premium'):
                return plan.title()
            return 'Free'
    except Exception as exc:
        print(f'Crystal subscription lookup warning: {type(exc).__name__}')
    return 'Free'

def _api_account_link(api_key: str, discord_user_id: str, discord_username: str):
    key = str(api_key or '').strip()
    uid = str(discord_user_id or '').strip()
    if not key or not uid:
        raise ValueError('API key and Discord user are required.')
    rows = _supabase_rest('crystal_scripts', {
        'select': 'id,name,api_key,user_id,guest_id,discord_user_id,discord_username,bot_data',
        'api_key': 'eq.' + urllib.parse.quote(key, safe=''), 'limit': '1'
    })
    if not rows:
        raise ValueError('API key not found. Copy the API key from the Dashboard.')
    script = rows[0]
    existing = _api_account_get(key)
    if existing and str(existing.get('discord_user_id') or '') not in ('', uid):
        raise ValueError('This API key is already linked to another Discord account.')
    owner = str(script.get('discord_user_id') or '')
    if owner and owner != uid:
        raise ValueError('This script is already linked to another Discord account.')
    now = _iso_now()
    bd = script.get('bot_data') if isinstance(script.get('bot_data'), dict) else {}
    bd = dict(bd)
    bd['api_account_linked'] = True
    bd['api_account_discord_user_id'] = uid
    _supabase_rest('crystal_scripts', {'id': 'eq.' + str(script['id'])}, 'PATCH', {
        'discord_user_id': uid,
        'discord_username': str(discord_username or '')[:100],
        'bot_data': bd,
        'updated_at': now,
    }, 'return=minimal')
    _supabase_rest('crystal_api_accounts', {}, 'POST', {
        'api_key': key,
        'script_id': str(script['id']),
        'discord_user_id': uid,
        'discord_username': str(discord_username or '')[:100],
        'supabase_user_id': str(script.get('user_id') or '') or None,
        'guest_id': str(script.get('guest_id') or '') or None,
        'updated_at': now,
        'linked_at': (existing or {}).get('linked_at') or now,
    }, 'resolution=merge-duplicates,return=minimal')
    # Keep the existing identity bridge in sync when this API key belongs to a
    # logged-in Dashboard account. Some Discord-created scripts have no Supabase
    # user yet, so do not insert a NULL supabase_user_id into that legacy table.
    if script.get('user_id'):
        _supabase_rest('crystal_discord_users', {}, 'POST', {
            'discord_user_id': uid,
            'discord_username': str(discord_username or '')[:100],
            'supabase_user_id': str(script.get('user_id')),
            'api_key': key,
            'plan': _linked_subscription_plan(script.get('user_id')),
            'updated_at': now,
        }, 'resolution=merge-duplicates,return=minimal')
    return script


def _api_account_reset(api_key: str, owner_user_id: str = '', guest_id: str = '', discord_user_id: str = ''):
    key = str(api_key or '').strip()
    if not key:
        raise ValueError('API key is required.')
    rows = _supabase_rest('crystal_scripts', {
        'select': 'id,name,api_key,user_id,guest_id,discord_user_id,discord_username,bot_data',
        'api_key': 'eq.' + urllib.parse.quote(key, safe=''), 'limit': '1'
    })
    if not rows:
        raise ValueError('API key not found.')
    script = rows[0]
    if owner_user_id and str(script.get('user_id') or '') != str(owner_user_id):
        raise PermissionError('This API key does not belong to your account.')
    if guest_id and str(script.get('guest_id') or '') != str(guest_id):
        raise PermissionError('This API key does not belong to this Guest account.')
    if discord_user_id and str(script.get('discord_user_id') or '') not in ('', str(discord_user_id)):
        raise PermissionError('This API key is linked to another Discord account.')
    new_key = 'CH-' + secrets.token_urlsafe(24).replace('-', '').replace('_', '')[:28] + '-' + secrets.token_hex(5)
    while _supabase_rest('crystal_scripts', {'select': 'id', 'api_key': 'eq.' + urllib.parse.quote(new_key, safe=''), 'limit': '1'}):
        new_key = 'CH-' + secrets.token_urlsafe(24).replace('-', '').replace('_', '')[:28] + '-' + secrets.token_hex(5)
    _supabase_rest('crystal_api_accounts', {'api_key': 'eq.' + urllib.parse.quote(key, safe='')}, 'DELETE', None, 'return=minimal')
    # Remove the old API key from the Discord identity bridge as well.
    try:
        _supabase_rest('crystal_discord_users', {'api_key': 'eq.' + urllib.parse.quote(key, safe='')}, 'DELETE', None, 'return=minimal')
    except Exception:
        pass
    bd = script.get('bot_data') if isinstance(script.get('bot_data'), dict) else {}
    bd = dict(bd)
    bd.pop('api_account_linked', None)
    bd.pop('api_account_discord_user_id', None)
    _supabase_rest('crystal_scripts', {'id': 'eq.' + str(script['id'])}, 'PATCH', {
        'api_key': new_key,
        'discord_user_id': None,
        'discord_username': None,
        'bot_data': bd,
        'updated_at': _iso_now(),
    }, 'return=minimal')
    # Update the live bot copy immediately so the old API disappears without waiting
    # for the next background sync cycle.
    try:
        for local_name, local_info in scripts_data.items():
            if str(local_info.get('supabase_row_id') or '') == str(script['id']) or str(local_info.get('api_key') or '') == key:
                local_info['api_key'] = new_key
                local_info.pop('supabase_row_id', None) if not script.get('id') else None
                local_info['created_by'] = local_info.get('created_by')
                local_info['discord_user_id'] = None
                local_info['discord_username'] = None
                local_info['api_account_linked'] = False
        save_data()
    except Exception as exc:
        print(f'API reset local-state warning: {type(exc).__name__}: {exc}')
    return {**script, 'api_key': new_key, 'discord_user_id': None, 'discord_username': None}


def _api_account_reset_from_script(script_id: str, owner_user_id: str = '', guest_id: str = ''):
    sid = str(script_id or '').strip()
    if not sid:
        raise ValueError('Script id is required.')
    rows = _supabase_rest('crystal_scripts', {
        'select': 'id,name,api_key,user_id,guest_id,discord_user_id,discord_username,bot_data',
        'id': 'eq.' + urllib.parse.quote(sid, safe=''), 'limit': '1'
    })
    if not rows:
        raise ValueError('Script not found.')
    script = rows[0]
    return _api_account_reset(str(script.get('api_key') or ''), owner_user_id, guest_id)


def _sync_script_to_supabase(script_name: str, info: dict, discord_user_id: str, discord_username: str, guild_id=None):
    """Persist a bot script in Supabase even when the Discord user has no Dashboard login yet.

    Supabase is the durable source of truth for scripts. The complete bot-side state is
    stored in bot_data so keys, whitelist grants, panels, trial/access state and HWID
    bindings survive Render redeploys and bot updates.
    """
    try:
        script_name = str(script_name or info.get("name") or info.get("script_name") or "").strip()
        if not script_name:
            raise ValueError("Script name is missing; refusing to write an unnamed row to Supabase.")
        info["name"] = script_name
        discord_user_id = str(discord_user_id or info.get("discord_user_id") or info.get("created_by") or "").strip()
        discord_username = str(discord_username or info.get("discord_username") or "")[:100]
        if discord_user_id:
            info["discord_user_id"] = discord_user_id
        if discord_username:
            info["discord_username"] = discord_username
        if guild_id:
            info["guild_id"] = str(guild_id)
        user_id = info.get("supabase_user_id") or None
        if not user_id and discord_user_id:
            try:
                linked = _supabase_rest("crystal_discord_users", {
                    "select": "supabase_user_id", "discord_user_id": "eq." + urllib.parse.quote(discord_user_id, safe=""), "limit": "1"
                })
                user_id = linked[0].get("supabase_user_id") if linked else None
            except Exception:
                user_id = None
        if user_id:
            info["supabase_user_id"] = str(user_id)
            try:
                _supabase_rest("crystal_discord_users", {}, "POST", {
                    "discord_user_id": discord_user_id, "discord_username": discord_username,
                    "supabase_user_id": str(user_id), "api_key": info.get("api_key", ""),
                    "plan": _linked_subscription_plan(user_id),
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
                }, "resolution=merge-duplicates,return=minimal")
            except Exception as link_exc:
                print(f"Crystal identity-link upsert warning for {discord_user_id}: {type(link_exc).__name__}: {link_exc}")

        # JSONB-safe snapshot of ALL bot-only state. Sets/dict keys are normalized.
        bot_data = {
            "crystal_bot_state_version": 2,
            "version": info.get("version", "1.0.0"),
            "buyer_role_id": info.get("buyer_role_id"),
            "admin_role_id": info.get("admin_role_id"),
            "created_by": str(info.get("created_by") or discord_user_id),
        "name": script_name,
            "panel_created_by": info.get("panel_created_by"),
            "panel_guild_id": info.get("panel_guild_id"),
            "panel_channel_id": info.get("panel_channel_id"),
            "panel_message_id": info.get("panel_message_id"),
            "panel_name": info.get("panel_name"),
            "panel_description": info.get("panel_description", ""),
            "panel_instances": info.get("panel_instances", []) if isinstance(info.get("panel_instances", []), list) else [],
            "valid_keys": sorted(str(x) for x in (info.get("valid_keys") or set())),
            "whitelisted_users": sorted(str(x) for x in (info.get("whitelisted_users") or set())),
            "user_keys": {str(k): v for k, v in (info.get("user_keys") or {}).items()},
            "key_meta": info.get("key_meta", {}) if isinstance(info.get("key_meta"), dict) else {},
            "user_access": {str(k): v for k, v in (info.get("user_access") or {}).items()},
            "hwid_bindings": info.get("hwid_bindings", {}) if isinstance(info.get("hwid_bindings"), dict) else {},
            "trail_enabled": bool(info.get("trail_enabled", str(info.get("access_mode", "free")).lower() == "free")),
            "trail_mode": info.get("trail_mode", "PERM"),
            "trail_expires_at": info.get("trail_expires_at"),
            "file_id": info.get("file_id"),
            "source_obfuscated": bool(info.get("source_obfuscated", False)),
            "api_key": info.get("api_key", ""),
            "fast_mode": bool(info.get("fast_mode", False)),
            "obfuscator": bool(info.get("obfuscator", False)),
            "ultra_safe": bool(info.get("ultra_safe", False)),
        }
        row = {
            "user_id": str(user_id) if user_id else None,
            "guest_id": str(info.get("guest_id") or "") or None,
            "name": script_name,
            "api_key": info.get("api_key", ""),
            "script": info.get("source_code", ""),
            "source_code": info.get("source_code", ""),
            "fast_mode": bool(info.get("fast_mode", False)),
            "obfuscator": bool(info.get("obfuscator", False)),
            "ultra_safe": bool(info.get("ultra_safe", False)),
            "discord_user_id": discord_user_id or None,
            "discord_username": discord_username or None,
            "guild_id": str(guild_id) if guild_id else info.get("guild_id"),
            "loader_code": info.get("loader_code") or info.get("file_id") or secrets.token_urlsafe(12).replace("-", "").replace("_", ""),
            "access_mode": info.get("access_mode", "free"),
            "disabled": bool(info.get("disabled", False)),
            "webhook_url": str(info.get("webhook_url") or "")[:2000],
            "settings": info.get("settings", {}) if isinstance(info.get("settings", {}), dict) else {},
            "key_meta": info.get("key_meta", {}) if isinstance(info.get("key_meta", {}), dict) else {},
            "source_name": info.get("source_name"),
            "source_path": info.get("source_path"),
            "bot_data": bot_data,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        info["loader_code"] = str(row["loader_code"])
        info["file_id"] = info["loader_code"]

        row_id = info.get("supabase_row_id")
        if row_id:
            updated = _supabase_rest("crystal_scripts", {"id": "eq." + str(row_id)}, "PATCH", row, "return=representation")
            if updated:
                return True
        api_key = str(row.get("api_key") or "").strip()
        existing = _supabase_rest("crystal_scripts", {
            "select": "id", "api_key": "eq." + urllib.parse.quote(api_key, safe=""), "limit": "1"
        }) if api_key else []
        if existing:
            updated = _supabase_rest("crystal_scripts", {"id": "eq." + str(existing[0]["id"])}, "PATCH", row, "return=representation")
            if updated:
                info["supabase_row_id"] = updated[0].get("id")
                return True
        inserted = _supabase_rest("crystal_scripts", {}, "POST", row, "return=representation")
        if inserted:
            info["supabase_row_id"] = inserted[0].get("id")
        return bool(inserted)
    except Exception as exc:
        print(f"Crystal sync to Supabase failed for {script_name}: {type(exc).__name__}: {exc}")
        return False


def _sync_dashboard_scripts_from_supabase():
    """Load durable Crystal Hub scripts from Supabase into the bot's runtime state."""
    try:
        links = _supabase_rest("crystal_discord_users", {"select": "discord_user_id,supabase_user_id"})
        user_map = {str(x.get("supabase_user_id")): str(x.get("discord_user_id"))
                    for x in links if x.get("supabase_user_id") and x.get("discord_user_id")}
        try:
            api_links = _supabase_rest("crystal_api_accounts", {"select": "discord_user_id,supabase_user_id"})
            for x in api_links:
                if x.get("supabase_user_id") and x.get("discord_user_id"):
                    user_map[str(x.get("supabase_user_id"))] = str(x.get("discord_user_id"))
        except Exception:
            pass

        rows = _supabase_rest("crystal_scripts", {
            "select": "id,user_id,name,api_key,source_code,script,discord_user_id,discord_username,guild_id,bot_data,fast_mode,obfuscator,ultra_safe,loader_code,access_mode,disabled,webhook_url,settings,key_meta,source_name,source_path"
        })
        count = 0
        for row in rows:
            bot_data = row.get("bot_data") if isinstance(row.get("bot_data"), dict) else {}
            owner = str(row.get("discord_user_id") or bot_data.get("created_by") or user_map.get(str(row.get("user_id")), ""))
            name = str(row.get("name") or "").strip()
            if not owner or not name:
                continue

            info = scripts_data.get(name, {})
            # Never let another owner's script with the same name be overwritten.
            if info and str(info.get("created_by", "")) not in ("", owner):
                continue

            def _int_set(value):
                out = set()
                for x in (value or []):
                    try: out.add(int(x))
                    except (TypeError, ValueError): out.add(x)
                return out

            valid_keys = set(str(x) for x in (bot_data.get("valid_keys") or []))
            whitelisted = _int_set(bot_data.get("whitelisted_users"))
            user_keys = {}
            for k, v in (bot_data.get("user_keys") or {}).items():
                try: user_keys[int(k)] = v
                except (TypeError, ValueError): pass
            user_access = {}
            for k, v in (bot_data.get("user_access") or {}).items():
                try: user_access[int(k)] = v
                except (TypeError, ValueError): pass

            # Backward compatibility: older rows stored only some state in top-level columns.
            if not valid_keys and info.get("valid_keys"):
                valid_keys = set(str(x) for x in info.get("valid_keys", set()))
            if not whitelisted and info.get("whitelisted_users"):
                whitelisted = info.get("whitelisted_users", set())
            if not user_keys and info.get("user_keys"):
                user_keys = info.get("user_keys", {})
            if not user_access and isinstance(bot_data.get("user_access"), dict) is False:
                user_access = info.get("user_access", {})

            info.update({
                "version": bot_data.get("version", "1.0.0"),
                "source_code": row.get("source_code") or row.get("script") or "-- Default Script",
                "buyer_role_id": bot_data.get("buyer_role_id"),
                "admin_role_id": bot_data.get("admin_role_id"),
                "valid_keys": valid_keys,
                "whitelisted_users": whitelisted,
                "user_keys": user_keys,
                "created_by": owner,
                "name": name,
                "panel_created_by": bot_data.get("panel_created_by"),
                "panel_guild_id": bot_data.get("panel_guild_id"),
                "panel_channel_id": bot_data.get("panel_channel_id"),
                "panel_message_id": bot_data.get("panel_message_id"),
                "panel_name": bot_data.get("panel_name"),
                "panel_description": bot_data.get("panel_description", ""),
                "panel_instances": bot_data.get("panel_instances", []) if isinstance(bot_data.get("panel_instances", []), list) else [],
                "api_key": row.get("api_key", ""),
                "supabase_user_id": str(row.get("user_id") or "") or None,
                "supabase_row_id": row.get("id"),
                "fast_mode": bool(row.get("fast_mode", bot_data.get("fast_mode", False))),
                "obfuscator": bool(row.get("obfuscator", bot_data.get("obfuscator", False))),
                "ultra_safe": bool(row.get("ultra_safe", bot_data.get("ultra_safe", False))),
                "file_id": row.get("loader_code") or bot_data.get("file_id") or info.get("file_id"),
                "loader_code": row.get("loader_code") or bot_data.get("file_id") or info.get("loader_code"),
                "access_mode": str(row.get("access_mode") or "free").lower(),
                "disabled": bool(row.get("disabled", False)),
                "webhook_url": row.get("webhook_url") or "",
                "settings": row.get("settings") if isinstance(row.get("settings"), dict) else {},
                "key_meta": row.get("key_meta") if isinstance(row.get("key_meta"), dict) else (bot_data.get("key_meta") if isinstance(bot_data.get("key_meta"), dict) else {}),
                "user_access": user_access,
                "trail_enabled": bool(bot_data.get("trail_enabled", str(row.get("access_mode") or "free").lower() == "free")),
                "trail_mode": bot_data.get("trail_mode", "PERM"),
                "trail_expires_at": bot_data.get("trail_expires_at"),
                "source_name": row.get("source_name"),
                "source_path": row.get("source_path"),
                "source_obfuscated": bool(bot_data.get("source_obfuscated", info.get("source_obfuscated", False))),
                "guild_id": row.get("guild_id"),
                "discord_user_id": owner,
                "discord_username": row.get("discord_username"),
            })
            info["hwid_bindings"] = bot_data.get("hwid_bindings") if isinstance(bot_data.get("hwid_bindings"), dict) else info.get("hwid_bindings", {})
            scripts_data[name] = info
            count += 1

        if count:
            save_data()
        print(f"Crystal durable sync: restored {count} script(s) from Supabase.")
        return count
    except Exception as exc:
        print(f"Crystal sync from Supabase failed: {type(exc).__name__}: {exc}")
        return 0

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
    """Accept either a Crystal Hub API key or a full API URL."""
    raw_value = (api_url or "").strip()
    parsed = urllib.parse.urlparse(raw_value)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return _load_script_from_supabase_api_key(raw_value)

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

def _crystal_account_public(row: dict) -> dict:
    """Safe account payload for the separate Crystal login site."""
    # Return both camelCase (login portal) and snake_case (dashboard) fields.
    guest_id = str(row.get("guest_id") or "")
    discord_id = str(row.get("discord_user_id") or "")
    discord_username = str(row.get("discord_username") or "")
    username = str(row.get("website_username") or "")
    avatar_url = str(row.get("avatar_url") or "")
    api_key = str(row.get("api_key") or "")
    plan = str(row.get("plan") or "Free")
    expires = row.get("plan_expires_at")
    created = row.get("created_at")
    return {
        "guestId": guest_id, "guest_id": guest_id,
        "discordId": discord_id, "discord_user_id": discord_id,
        "discordUsername": discord_username, "discord_username": discord_username,
        "username": username, "website_username": username,
        "avatarUrl": avatar_url, "avatar_url": avatar_url,
        "apiKey": api_key, "api_key": api_key,
        "plan": plan,
        "planExpiresAt": expires, "plan_expires_at": expires,
        "createdAt": created, "created_at": created,
    }


def _crystal_account_by_discord(discord_user_id: str):
    did = str(discord_user_id or "").strip()
    if not re.fullmatch(r"[0-9]{17,20}", did):
        return None
    rows = _supabase_rest("crystal_accounts", {"select": "*", "discord_user_id": "eq." + did, "limit": "1"})
    return rows[0] if rows else None


def _crystal_account_by_key(api_key: str):
    key = str(api_key or "").strip()
    if not re.fullmatch(r"CA-[A-Za-z0-9_-]{24,100}", key):
        return None
    rows = _supabase_rest("crystal_accounts", {
        "select": "*", "api_key": "eq." + urllib.parse.quote(key, safe=""), "limit": "1"
    })
    return rows[0] if rows else None


def _crystal_account_create(discord_user, website_username: str) -> dict:
    did = str(getattr(discord_user, "id", "") or "").strip()
    uname = str(website_username or "").strip()
    if not did.isdigit() or not (17 <= len(did) <= 20):
        raise ValueError("Discord ID is invalid.")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", uname):
        raise ValueError("Website username must be 3-24 characters (letters, numbers, underscore).")
    existing = _supabase_rest("crystal_accounts", {
        "select": "*", "discord_user_id": "eq." + did, "limit": "1"
    })
    # A Discord identity can own only one site account. Do not silently replace its API key.
    if existing:
        row = existing[0]
        if str(row.get("website_username") or "").lower() != uname.lower():
            taken = _supabase_rest("crystal_accounts", {
                "select": "guest_id", "website_username": "eq." + urllib.parse.quote(uname, safe=""), "limit": "1"
            })
            if taken and str(taken[0].get("guest_id")) != str(row.get("guest_id")):
                raise ValueError("That website username is already taken.")
            _supabase_rest("crystal_accounts", {"guest_id": "eq." + urllib.parse.quote(str(row.get("guest_id")), safe="")}, "PATCH", {
                "website_username": uname
            }, "return=minimal")
            row["website_username"] = uname
        return row
    taken = _supabase_rest("crystal_accounts", {
        "select": "guest_id", "website_username": "eq." + urllib.parse.quote(uname, safe=""), "limit": "1"
    })
    if taken:
        raise ValueError("That website username is already taken.")
    avatar = ""
    try:
        avatar = str(discord_user.display_avatar.url)
    except Exception:
        pass
    guest_id = secrets.token_urlsafe(24)
    api_key = "CA-" + secrets.token_urlsafe(36).replace("-", "").replace("_", "")[:48]
    row = {
        "guest_id": guest_id,
        "discord_user_id": did,
        "discord_username": str(discord_user)[:100],
        "website_username": uname,
        "avatar_url": avatar[:2000],
        "api_key": api_key,
        "plan": "Free",
    }
    created = _supabase_rest("crystal_accounts", {}, "POST", row, "return=representation")
    return created[0] if created else row


def _crystal_account_reset_key(account):
    if not account: raise PermissionError("Invalid Account API key.")
    new_key=_account_api_generate()
    while _crystal_account_by_key(new_key): new_key=_account_api_generate()
    gid=str(account.get("guest_id") or "")
    rows=_supabase_rest("crystal_accounts",{"guest_id":"eq."+urllib.parse.quote(gid,safe="")},"PATCH",{"api_key":new_key},"return=representation")
    return rows[0] if rows else {**account,"api_key":new_key}

def _dashboard_account_from_request(handler, body=None):
    body=body if isinstance(body,dict) else {}; key=str(body.get("apiKey") or handler.headers.get("X-Crystal-Account") or "").strip()
    if not key:
        q=urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query); key=str((q.get("apiKey") or [""])[0]).strip()
    account=_crystal_account_by_key(key)
    if not account: raise PermissionError("Invalid Account API key.")
    return account

def _dashboard_member(account,guild_id):
    gid=str(guild_id or ""); did=str(account.get("discord_user_id") or "")
    if not gid.isdigit() or not did.isdigit(): return None,None
    guild=bot.get_guild(int(gid));
    if not guild: return None,None
    member=guild.get_member(int(did))
    if not member:
        try: member=asyncio.run_coroutine_threadsafe(guild.fetch_member(int(did)),bot.loop).result(timeout=8)
        except Exception: member=None
    return guild,member

def _dashboard_guilds(account):
    did=str(account.get("discord_user_id") or ""); out=[]
    if not did.isdigit(): return out
    for guild in bot.guilds:
        member=guild.get_member(int(did))
        if not member: continue
        role_id=_global_admin_role_id(guild.id)
        can=bool(getattr(member.guild_permissions,"administrator",False)) or bool(role_id and any(int(getattr(r,"id",0))==int(role_id) for r in member.roles))
        out.append({"id":str(guild.id),"name":guild.name,"icon":str(guild.icon.url if guild.icon else ""),"canManage":can,"adminRoleId":role_id})
    return sorted(out,key=lambda x:x["name"].lower())

def _dashboard_scripts(account,guild_id):
    rows=[]; seen=set(); gid=str(guild_id or ""); guest=str(account.get("guest_id") or ""); did=str(account.get("discord_user_id") or "")
    for field,val in (("guest_id",guest),("discord_user_id",did)):
        if not val: continue
        q={"select":"*",field:"eq."+urllib.parse.quote(val,safe=""),"order":"created_at.desc"}
        if gid: q["guild_id"]="eq."+urllib.parse.quote(gid,safe="")
        try:
            for row in _supabase_rest("crystal_scripts",q):
                rid=str(row.get("id") or "")
                if rid and rid not in seen: seen.add(rid); rows.append(row)
        except Exception as exc: print(f"Dashboard script query warning: {type(exc).__name__}")
    return rows

def _dashboard_state_http(handler):
    try:
        account=_dashboard_account_from_request(handler); q=urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query); gid=str((q.get("guildId") or [""])[0]).strip(); guilds=_dashboard_guilds(account)
        if gid and not any(x["id"]==gid for x in guilds): handler._send_text(403,json.dumps({"error":"You are not connected to that Discord server."})); return
        if not gid and guilds: gid=guilds[0]["id"]
        scripts=_dashboard_scripts(account,gid); discord_plan=str(_discord_plan(account.get("discord_user_id")) or "Free"); stored_plan=str(account.get("plan") or "Free"); plan="Admin" if discord_plan.lower()=="admin" else (stored_plan if stored_plan.lower() in ("premium","ultra") else discord_plan)
        admin=plan.lower()=="admin"; role=_global_admin_role_id(gid) if gid else None; _,member=_dashboard_member(account,gid) if gid else (None,None); server_admin=bool(member and (getattr(member.guild_permissions,"administrator",False) or (role and any(int(getattr(r,"id",0))==int(role) for r in member.roles))))
        handler._send_text(200,json.dumps({"ok":True,"account":_crystal_account_public(account),"plan":plan,"adminRedeemed":admin,"guilds":guilds,"guildId":gid,"scripts":scripts,"adminRoleId":role,"serverAdmin":server_admin},ensure_ascii=False))
    except PermissionError as exc: handler._send_text(401,json.dumps({"error":str(exc)}))
    except Exception as exc: print(f"Dashboard state failed: {type(exc).__name__}: {exc}"); handler._send_text(500,json.dumps({"error":"Could not load Dashboard state."}))

def _dashboard_script_save_http(handler):
    try:
        length=int(handler.headers.get("Content-Length","0")); body=json.loads(handler.rfile.read(length).decode("utf-8")); account=_dashboard_account_from_request(handler,body); gid=str(body.get("guildId") or ""); name=str(body.get("name") or "").strip()[:100]
        if not gid.isdigit() or not name: handler._send_text(400,json.dumps({"error":"guildId and script name are required."})); return
        if not any(x["id"]==gid for x in _dashboard_guilds(account)): handler._send_text(403,json.dumps({"error":"You are not connected to the selected Discord server."})); return
        existing_id=str(body.get("id") or ""); existing=next((x for x in _dashboard_scripts(account,gid) if str(x.get("id"))==existing_id),None) if existing_id else None
        plan=str(account.get("plan") or "Free"); limit=PLAN_LIMITS.get(plan,PLAN_LIMITS["Free"])["scripts"]
        if not existing and limit is not None and len(_dashboard_scripts(account,gid))>=limit: handler._send_text(403,json.dumps({"error":f"{plan} plan script limit reached."})); return
        src=str(body.get("source_code") or body.get("script") or ""); api=str(body.get("api_key") or (existing or {}).get("api_key") or "")
        if not re.fullmatch(r"CH-[A-Za-z0-9_-]{18,120}",api): api="CH-"+secrets.token_urlsafe(30).replace("-","").replace("_","")[:40]
        row={"guest_id":str(account.get("guest_id") or ""),"user_id":None,"name":name,"api_key":api,"script":src,"source_code":src,"fast_mode":bool(body.get("fast_mode")),"obfuscator":bool(body.get("obfuscator")),"ultra_safe":bool(body.get("ultra_safe")),"discord_user_id":str(account.get("discord_user_id") or ""),"discord_username":str(account.get("discord_username") or ""),"guild_id":gid,"loader_code":str(body.get("loader_code") or (existing or {}).get("loader_code") or secrets.token_urlsafe(12)),"access_mode":str(body.get("access_mode") or (existing or {}).get("access_mode") or "free").lower(),"disabled":bool(body.get("disabled",False)),"settings":body.get("settings") if isinstance(body.get("settings"),dict) else {},"key_meta":body.get("key_meta") if isinstance(body.get("key_meta"),dict) else {},"source_name":str(body.get("source_name") or "")[:255] or None,"source_path":"dashboard-text" if src else None,"bot_data":body.get("bot_data") if isinstance(body.get("bot_data"),dict) else {},"updated_at":_iso_now()}
        row["access_mode"]="paid" if row["access_mode"]=="paid" else "free"; bd=dict(row["bot_data"]); bd["created_by"]=str(account.get("discord_user_id") or ""); bd["trail_enabled"]=row["access_mode"]=="free"; row["bot_data"]=bd
        if existing: _supabase_rest("crystal_scripts",{"id":"eq."+urllib.parse.quote(existing_id,safe="")},"PATCH",row,"return=minimal"); rid=existing_id
        else:
            created=_supabase_rest("crystal_scripts",{},"POST",row,"return=representation"); rid=str(created[0].get("id")) if created else ""
        _sync_dashboard_scripts_from_supabase(); handler._send_text(200,json.dumps({"ok":True,"id":rid,"script":{**row,"id":rid}},ensure_ascii=False))
    except PermissionError as exc: handler._send_text(401,json.dumps({"error":str(exc)}))
    except Exception as exc: print(f"Dashboard script save failed: {type(exc).__name__}: {exc}"); handler._send_text(500,json.dumps({"error":"Could not save script: "+str(exc)[:240]}))

def _dashboard_admin_script_action_http(handler):
    try:
        length=int(handler.headers.get("Content-Length","0")); body=json.loads(handler.rfile.read(length).decode("utf-8")); account=_dashboard_account_from_request(handler,body); gid=str(body.get("guildId") or ""); sid=str(body.get("id") or ""); action=str(body.get("action") or "").lower()
        plan=str(account.get("plan") or "Free")
        if plan.lower()!="admin": handler._send_text(403,json.dumps({"error":"Admin redeem is required."})); return
        guild,member=_dashboard_member(account,gid)
        if not guild or not member: handler._send_text(403,json.dumps({"error":"Selected Discord server is not available."})); return
        role=_global_admin_role_id(guild.id); allowed=bool(getattr(member.guild_permissions,"administrator",False)) or bool(role and any(int(getattr(r,"id",0))==int(role) for r in member.roles))
        if not allowed: handler._send_text(403,json.dumps({"error":"You need the Admin Role configured for this server or Discord Administrator permission."})); return
        row=next((x for x in _dashboard_scripts(account,gid) if str(x.get("id"))==sid),None)
        if not row: handler._send_text(404,json.dumps({"error":"Script not found in this server."})); return
        if action=="delete": _supabase_rest("crystal_scripts",{"id":"eq."+urllib.parse.quote(sid,safe="")},"DELETE",None,"return=minimal")
        elif action=="set_access":
            mode="paid" if str(body.get("access_mode")).lower()=="paid" else "free"; bd=dict(row.get("bot_data") if isinstance(row.get("bot_data"),dict) else {}); bd["trail_enabled"]=mode=="free"; _supabase_rest("crystal_scripts",{"id":"eq."+urllib.parse.quote(sid,safe="")},"PATCH",{"access_mode":mode,"bot_data":bd,"updated_at":_iso_now()},"return=minimal")
        elif action=="set_disabled": _supabase_rest("crystal_scripts",{"id":"eq."+urllib.parse.quote(sid,safe="")},"PATCH",{"disabled":bool(body.get("disabled")),"updated_at":_iso_now()},"return=minimal")
        else: handler._send_text(400,json.dumps({"error":"Unknown admin action."})); return
        _sync_dashboard_scripts_from_supabase(); handler._send_text(200,json.dumps({"ok":True,"action":action}))
    except PermissionError as exc: handler._send_text(401,json.dumps({"error":str(exc)}))
    except Exception as exc: print(f"Dashboard admin action failed: {type(exc).__name__}: {exc}"); handler._send_text(500,json.dumps({"error":"Admin action failed."}))

def _dashboard_script_delete_http(handler):
    try:
        length=int(handler.headers.get("Content-Length","0")); body=json.loads(handler.rfile.read(length).decode("utf-8")); account=_dashboard_account_from_request(handler,body); sid=str(body.get("id") or ""); gid=str(body.get("guildId") or ""); row=next((x for x in _dashboard_scripts(account,gid) if str(x.get("id"))==sid),None)
        if not row: handler._send_text(404,json.dumps({"error":"Script not found."})); return
        _supabase_rest("crystal_scripts",{"id":"eq."+urllib.parse.quote(sid,safe=""),"guest_id":"eq."+urllib.parse.quote(str(account.get("guest_id")),safe="")},"DELETE",None,"return=minimal"); scripts_data.pop(str(row.get("name") or ""),None); save_data(); _sync_dashboard_scripts_from_supabase(); handler._send_text(200,json.dumps({"ok":True}))
    except PermissionError as exc: handler._send_text(401,json.dumps({"error":str(exc)}))
    except Exception as exc: handler._send_text(500,json.dumps({"error":"Could not delete script."}))

def _discord_oauth_login_http(handler):
    """Verify a short-lived Discord OAuth access token server-side and return the linked Crystal account."""
    try:
        length=int(handler.headers.get("Content-Length","0"))
        if length<=0 or length>32768:
            handler._send_text(400,json.dumps({"error":"Invalid Discord sign-in request."})); return
        body=json.loads(handler.rfile.read(length).decode("utf-8"))
        token=str(body.get("accessToken") or "").strip()
        if not token or len(token)>4096:
            handler._send_text(400,json.dumps({"error":"Discord authorization token is missing."})); return
        req=urllib.request.Request("https://discord.com/api/users/@me",headers={"Authorization":"Bearer "+token,"User-Agent":"CrystalHub/1.0"})
        with urllib.request.urlopen(req,timeout=12) as response:
            profile=json.loads(response.read().decode("utf-8"))
        did=str(profile.get("id") or "")
        if not re.fullmatch(r"[0-9]{17,20}",did):
            handler._send_text(401,json.dumps({"error":"Discord did not return a valid account."})); return
        rows=_supabase_rest("crystal_accounts",{"select":"*","discord_user_id":"eq."+did,"limit":"1"})
        if rows:
            account=rows[0]
            patch={"discord_username":str(profile.get("global_name") or profile.get("username") or did)[:100]}
            avatar=profile.get("avatar")
            if avatar:
                ext="gif" if str(avatar).startswith("a_") else "png"
                patch["avatar_url"]=f"https://cdn.discordapp.com/avatars/{did}/{avatar}.{ext}?size=256"
            _supabase_rest("crystal_accounts",{"guest_id":"eq."+urllib.parse.quote(str(account.get("guest_id") or ""),safe="")},"PATCH",patch,"return=minimal")
            account={**account,**patch}
        else:
            base=re.sub(r"[^A-Za-z0-9_]","",str(profile.get("global_name") or profile.get("username") or "Crystal"))[:18] or "CrystalUser"
            username=base
            for n in range(1,1000):
                taken=_supabase_rest("crystal_accounts",{"select":"guest_id","website_username":"eq."+urllib.parse.quote(username,safe=""),"limit":"1"})
                if not taken: break
                username=(base[:18]+str(n))[:24]
            avatar=""
            if profile.get("avatar"):
                ext="gif" if str(profile["avatar"]).startswith("a_") else "png"
                avatar=f"https://cdn.discordapp.com/avatars/{did}/{profile['avatar']}.{ext}?size=256"
            row={"guest_id":secrets.token_urlsafe(24),"discord_user_id":did,"discord_username":str(profile.get("global_name") or profile.get("username") or did)[:100],"website_username":username,"avatar_url":avatar,"api_key":_account_api_generate(),"plan":"Free"}
            created=_supabase_rest("crystal_accounts",{},"POST",row,"return=representation")
            account=created[0] if created else row
        if not account.get("api_key"):
            handler._send_text(500,json.dumps({"error":"Crystal account has no Account API key. Contact support."})); return
        handler._send_text(200,json.dumps({"ok":True,"account":_crystal_account_public(account)},ensure_ascii=False))
    except urllib.error.HTTPError as exc:
        handler._send_text(401,json.dumps({"error":"Discord sign-in was rejected. Please authorize again."}))
    except Exception as exc:
        print(f"Discord OAuth login failed: {type(exc).__name__}: {exc}")
        handler._send_text(500,json.dumps({"error":"Could not finish Discord sign-in."}))

class DynamicScriptHandler(BaseHTTPRequestHandler):
    def _send_text(self, status, body):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", os.getenv("SITE_ORIGIN", "*"))
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Crystal-Account, X-Access-Token")
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

    def _guest_plan(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        guest_id = str((query.get("guestId") or [""])[0]).strip()[:128]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", guest_id):
            self._send_text(400, json.dumps({"error": "invalid guest id"})); return
        try:
            rows = _supabase_rest("crystal_guest_subscriptions", {
                "select": "plan,expires_at,created_at,updated_at",
                "guest_id": "eq." + urllib.parse.quote(guest_id, safe=""), "limit": "1"
            })
            record = rows[0] if rows else None
            if not record:
                self._send_text(200, json.dumps({"ok": True, "plan": "Free", "expiresAt": None})); return
            expires = record.get("expires_at")
            if expires and datetime.datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= datetime.datetime.now(datetime.timezone.utc):
                self._send_text(200, json.dumps({"ok": True, "plan": "Free", "expiresAt": expires})); return
            self._send_text(200, json.dumps({"ok": True, "plan": record.get("plan", "Free"), "expiresAt": expires}, ensure_ascii=False))
        except Exception as exc:
            print(f"Guest plan lookup failed: {type(exc).__name__}: {exc}")
            self._send_text(200, json.dumps({"ok": True, "plan": "Free", "expiresAt": None}))

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
            account_api = str(payload.get("apiKey", "")).strip()
            account = _crystal_account_by_key(account_api) if account_api else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_text(400, json.dumps({"error": "Invalid request."})); return
        guest_id = str(payload.get("guestId", "")).strip()[:128]
        if guest_id and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", guest_id):
            self._send_text(400, json.dumps({"error": "Invalid Guest ID."})); return
        user = _verify_supabase_access_token(access_token) if access_token else None
        if not user and not guest_id and not account:
            self._send_text(401, json.dumps({"error": "Account API login is required."})); return
        if account: guest_id=str(account.get("guest_id") or ""); user=None
        key = _hash_redeem_code(code)
        # The hidden Admin code can be redeemed from the website without creating an account.
        # Signed-in users are stored against their Supabase account; guests are stored against
        # their stable guest_id so the Admin entitlement survives page reloads on that device.
        if hmac.compare_digest(code, ADMIN_REDEEM_CODE):
            if account:
                _supabase_rest("crystal_accounts",{"guest_id":"eq."+urllib.parse.quote(str(account.get("guest_id")),safe="")},"PATCH",{"plan":"Admin","plan_expires_at":None},"return=minimal")
            elif user:
                _supabase_upsert_subscription(user["id"], "Admin", None, "admin-code")
            else:
                now_iso = _iso_now()
                try:
                    _supabase_rest(
                        "crystal_guest_subscriptions",
                        {},
                        "POST",
                        {"guest_id": guest_id, "plan": "Admin", "expires_at": None, "source": "admin-code", "updated_at": now_iso.isoformat() if hasattr(now_iso, "isoformat") else str(now_iso)},
                        "resolution=merge-duplicates,return=minimal"
                    )
                except Exception as exc:
                    print(f"Guest Admin redemption save failed: {type(exc).__name__}: {exc}")
                    self._send_text(500, json.dumps({"error": "Server could not save Admin redemption. Try again later."})); return
            self._send_text(200, json.dumps({"ok": True, "plan": "Admin", "duration": "Lifetime", "expiresAt": None})); return

        try:
            rows = _supabase_rest("crystal_redeem_codes", {"select": "id,plan,duration_days,redeemed_by,redeemed_guest_id,redeemed_at,revoked", "code_hash": "eq." + key, "limit": "1"})
        except Exception as exc:
            print(f"Crystal redeem DB lookup failed: {type(exc).__name__}")
            rows = []
        record = rows[0] if rows else None
        if record and record.get("revoked"):
            record = None
        if record and (record.get("redeemed_by") or record.get("redeemed_guest_id")):
            self._send_text(409, json.dumps({"error": "This code has already been used."})); return

        if record:
            plan = str(record.get("plan", "Premium"))
            days = int(record.get("duration_days") or 0)
            now = datetime.datetime.now(datetime.timezone.utc)
            expires_at = None if days <= 0 else (now + datetime.timedelta(days=days)).isoformat()
            if account:
                _supabase_rest("crystal_accounts",{"guest_id":"eq."+urllib.parse.quote(str(account.get("guest_id")),safe="")},"PATCH",{"plan":plan,"plan_expires_at":expires_at},"return=minimal")
            try:
                patch = {"redeemed_at": now.isoformat()}
                if user:
                    patch["redeemed_by"] = user["id"]
                else:
                    patch["redeemed_guest_id"] = guest_id
                _supabase_rest("crystal_redeem_codes", {"id": "eq." + str(record["id"])}, "PATCH", patch, "return=minimal")
                if user:
                    _supabase_upsert_subscription(user["id"], plan, expires_at, "redeem", record["id"])
                else:
                    _supabase_rest("crystal_guest_subscriptions", {}, "POST", {"guest_id": guest_id, "plan": plan, "expires_at": expires_at, "source": "redeem", "redeem_code_id": str(record["id"]), "updated_at": now.isoformat()}, "resolution=merge-duplicates,return=minimal")
                self._send_text(200, json.dumps({"ok": True, "plan": plan, "duration": "Lifetime" if days <= 0 else f"{days} days", "expiresAt": expires_at})); return
            except Exception as exc:
                print(f"Guest redemption save failed: {type(exc).__name__}: {exc}")
                self._send_text(500, json.dumps({"error": "Server could not save redemption. Try again later."})); return

        # Legacy fallback for codes created before the Supabase migration.
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
        old_code = dict(record)
        now_redeemed = _iso_now()
        try:
            if account:
                _supabase_rest("crystal_accounts",{"guest_id":"eq."+urllib.parse.quote(str(account.get("guest_id")),safe="")},"PATCH",{"plan":plan,"plan_expires_at":expires_at},"return=minimal")
                record["redeemed_by"] = None
                record["redeemed_guest_id"] = str(account.get("guest_id"))
            elif user:
                record["redeemed_by"] = user["id"]
                record["redeemed_guest_id"] = None
                site_entitlements[user["id"]] = {"plan": plan, "redeemed_at": now_redeemed, "expires_at": expires_at, "code_hash": key}
                _supabase_upsert_subscription(user["id"], plan, expires_at, "legacy-redeem")
            else:
                # Legacy website codes must also work in Guest mode. Never require
                # a Supabase account just to redeem a customer subscription.
                record["redeemed_by"] = None
                record["redeemed_guest_id"] = guest_id
                site_entitlements["guest:" + guest_id] = {"plan": plan, "redeemed_at": now_redeemed, "expires_at": expires_at, "code_hash": key}
                try:
                    _supabase_rest("crystal_guest_subscriptions", {}, "POST", {
                        "guest_id": guest_id, "plan": plan, "expires_at": expires_at,
                        "source": "legacy-redeem", "updated_at": now_redeemed
                    }, "resolution=merge-duplicates,return=minimal")
                except Exception:
                    pass
            record["redeemed_at"] = now_redeemed
            _save_json_dict(SITE_CODES_FILE, site_redeem_codes); _save_json_dict(SITE_ENTITLEMENTS_FILE, site_entitlements)
        except OSError:
            site_redeem_codes[key] = old_code
            self._send_text(500, json.dumps({"error": "Server could not save redemption. Try again later."})); return
        self._send_text(200, json.dumps({"ok": True, "plan": plan, "duration": "Lifetime" if days <= 0 else f"{days} days", "expiresAt": expires_at}))

    def _dashboard_script_sync(self):
        """Receive an authenticated dashboard script and make it available to bot API-key imports."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                self._send_text(400, json.dumps({"error": "invalid request size"})); return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                self._send_text(400, json.dumps({"error": "invalid request"})); return
            token = str(body.get("accessToken", "")).strip()
            user = _verify_supabase_access_token(token)
            if not user:
                self._send_text(401, json.dumps({"error": "Supabase login required"})); return
            data = body.get("script") if isinstance(body.get("script"), dict) else body
            api_key = str(data.get("api_key", "")).strip()
            name = str(data.get("name", "")).strip()
            if not api_key or not api_key.startswith("CH-"):
                self._send_text(400, json.dumps({"error": "A valid Crystal Hub API key is required."})); return
            if not name:
                self._send_text(400, json.dumps({"error": "Script name is required."})); return
            row = {
                "user_id": str(user["id"]), "name": name[:100], "api_key": api_key[:200],
                "script": str(data.get("script") or data.get("source_code") or ""),
                "source_code": str(data.get("source_code") or data.get("script") or ""),
                "fast_mode": bool(data.get("fast_mode", False)),
                "obfuscator": bool(data.get("obfuscator", False)),
                "ultra_safe": bool(data.get("ultra_safe", False)),
                "discord_user_id": str(data.get("discord_user_id") or "")[:100] or None,
                "discord_username": str(data.get("discord_username") or "")[:100] or None,
                "guild_id": str(data.get("guild_id") or "")[:32] or None,
                "guest_id": str(data.get("guest_id") or "")[:128] or None,
                "loader_code": str(data.get("loader_code") or "")[:300] or None,
                "access_mode": "paid" if str(data.get("access_mode", "free")).lower()=="paid" else "free",
                "disabled": bool(data.get("disabled", False)),
                "webhook_url": str(data.get("webhook_url") or "")[:2000],
                "settings": data.get("settings") if isinstance(data.get("settings"), dict) else {},
                "key_meta": data.get("key_meta") if isinstance(data.get("key_meta"), dict) else {},
                "source_name": str(data.get("source_name") or "")[:255] or None,
                "source_path": str(data.get("source_path") or "")[:1000] or None,
                "bot_data": data.get("bot_data") if isinstance(data.get("bot_data"), dict) else {},
                "updated_at": _iso_now(),
            }
            bd=dict(row["bot_data"]); bd["trail_enabled"]=(row["access_mode"]=="free"); row["bot_data"]=bd
            existing=_supabase_rest("crystal_scripts", {"select":"id,user_id", "api_key":"eq."+urllib.parse.quote(api_key, safe=""), "limit":"1"})
            if existing:
                if str(existing[0].get("user_id")) != str(user["id"]):
                    self._send_text(409, json.dumps({"error":"This API key belongs to another account."})); return
                _supabase_rest("crystal_scripts", {"id":"eq."+str(existing[0]["id"])}, "PATCH", row, "return=minimal")
                row_id=existing[0]["id"]
            else:
                created=_supabase_rest("crystal_scripts", {}, "POST", row, "return=representation")
                row_id=created[0].get("id") if created else None
            self._send_text(200, json.dumps({"ok":True,"id":row_id,"api_key":api_key,"name":name}))
        except Exception as exc:
            self._send_text(500, json.dumps({"error":"Dashboard sync failed: "+str(exc)[:300]}))


    def _guest_script_sync(self):
        """Persist a Guest dashboard script in Supabase without requiring Supabase Auth."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                self._send_text(400, json.dumps({"error": "invalid request size"})); return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                self._send_text(400, json.dumps({"error": "invalid request"})); return
            guest_id = str(body.get("guestId", "")).strip()[:128]
            data = body.get("script") if isinstance(body.get("script"), dict) else body
            name = str(data.get("name", "")).strip()[:100]
            api_key = str(data.get("api_key", "")).strip()[:200]
            if not guest_id or not name or not api_key:
                self._send_text(400, json.dumps({"error": "guestId, name and api_key are required"})); return
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", guest_id):
                self._send_text(400, json.dumps({"error": "invalid guest id"})); return
            # Guest keys are generated by the dashboard, but the server is the source of truth.
            row = {
                "guest_id": guest_id, "user_id": None,
                "name": name, "api_key": api_key,
                "script": str(data.get("script") or data.get("source_code") or ""),
                "source_code": str(data.get("source_code") or data.get("script") or ""),
                "fast_mode": bool(data.get("fast_mode", False)),
                "obfuscator": bool(data.get("obfuscator", False)),
                "ultra_safe": bool(data.get("ultra_safe", False)),
                "discord_user_id": None, "discord_username": None,
                "guild_id": str(data.get("guild_id") or "")[:32] or None,
                "loader_code": str(data.get("loader_code") or "")[:300] or None,
                "access_mode": "paid" if str(data.get("access_mode", "free")).lower() == "paid" else "free",
                "disabled": bool(data.get("disabled", False)),
                "webhook_url": str(data.get("webhook_url") or "")[:2000],
                "settings": data.get("settings") if isinstance(data.get("settings"), dict) else {},
                "key_meta": data.get("key_meta") if isinstance(data.get("key_meta"), dict) else {},
                "source_name": str(data.get("source_name") or "")[:255] or None,
                "source_path": "guest-supabase",
                "bot_data": data.get("bot_data") if isinstance(data.get("bot_data"), dict) else {},
                "updated_at": _iso_now(),
            }
            bd = dict(row["bot_data"]); bd["guest_id"] = guest_id; bd["trail_enabled"] = row["access_mode"] == "free"; row["bot_data"] = bd
            existing = _supabase_rest("crystal_scripts", {
                "select": "id", "guest_id": "eq." + urllib.parse.quote(guest_id, safe=""),
                "api_key": "eq." + urllib.parse.quote(api_key, safe=""), "limit": "1"
            })
            if existing:
                _supabase_rest("crystal_scripts", {"id": "eq." + str(existing[0]["id"])}, "PATCH", row, "return=minimal")
                row_id = existing[0]["id"]
            else:
                created = _supabase_rest("crystal_scripts", {}, "POST", row, "return=representation")
                row_id = created[0].get("id") if created else None
            self._send_text(200, json.dumps({"ok": True, "id": row_id, "script": {**row, "id": row_id}}, ensure_ascii=False))
        except Exception as exc:
            print(f"Guest script sync failed: {type(exc).__name__}: {exc}")
            self._send_text(500, json.dumps({"error": "Guest script sync failed: " + str(exc)[:300]}))

    def _guest_scripts_list(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        guest_id = str((query.get("guestId") or [""])[0]).strip()[:128]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", guest_id):
            self._send_text(400, json.dumps({"error": "invalid guest id"})); return
        try:
            rows = _supabase_rest("crystal_scripts", {
                "select": "*", "guest_id": "eq." + urllib.parse.quote(guest_id, safe=""),
                "order": "created_at.desc"
            })
            self._send_text(200, json.dumps({"ok": True, "scripts": rows}, ensure_ascii=False))
        except Exception as exc:
            print(f"Guest script list failed: {type(exc).__name__}: {exc}")
            self._send_text(500, json.dumps({"error": "Could not load Guest scripts."}))

    def _guest_script_delete(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                self._send_text(400, json.dumps({"error": "invalid request"})); return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            guest_id = str(body.get("guestId", "")).strip()[:128]
            script_id = str(body.get("id", "")).strip()[:100]
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", guest_id) or not script_id:
                self._send_text(400, json.dumps({"error": "invalid guest id or script id"})); return
            _supabase_rest("crystal_scripts", {
                "id": "eq." + urllib.parse.quote(script_id, safe=""),
                "guest_id": "eq." + urllib.parse.quote(guest_id, safe="")
            }, "DELETE", None, "return=minimal")
            self._send_text(200, json.dumps({"ok": True}))
        except Exception as exc:
            print(f"Guest script delete failed: {type(exc).__name__}: {exc}")
            self._send_text(500, json.dumps({"error": "Could not delete Guest script."}))

    def _account_create_http(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>600000:
                self._send_text(400,json.dumps({'error':'Invalid request.'})); return
            body=json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body, dict):
                raise ValueError('Invalid request.')
            # New login site: Discord ID first, website username second; no email/password.
            if body.get('discordId') is not None:
                did = str(body.get('discordId', '')).strip()
                if not re.fullmatch(r'[0-9]{17,20}', did):
                    raise ValueError('Enter a valid Discord ID.')
                if not bot.is_ready():
                    self._send_text(503, json.dumps({'error':'Discord bot is not ready. Try again shortly.'})); return
                future = asyncio.run_coroutine_threadsafe(bot.fetch_user(int(did)), bot.loop)
                try:
                    discord_user = future.result(timeout=12)
                except Exception:
                    raise ValueError('Could not find that Discord account. Check the Discord ID.')
                account = _crystal_account_create(discord_user, str(body.get('username', '')))
                self._send_text(200, json.dumps({'ok':True,'account':_crystal_account_public(account)},ensure_ascii=False))
            else:
                # Backward compatibility for the older Guest/password signup page.
                account=_account_api_create_account(str(body.get('guestId','')), str(body.get('username','')), str(body.get('password','')), str(body.get('apiKey','')), str(body.get('avatarUrl','')))
                self._send_text(200,json.dumps({'ok':True,'account':account},ensure_ascii=False))
        except ValueError as exc:
            self._send_text(400,json.dumps({'error':str(exc)}))
        except Exception as exc:
            print(f'Account create failed: {type(exc).__name__}: {exc}')
            self._send_text(500,json.dumps({'error':'Could not create account.'}))

    def _account_login_http(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>32768:
                self._send_text(400,json.dumps({'error':'Invalid request.'})); return
            body=json.loads(self.rfile.read(length).decode('utf-8'))
            api_key = str(body.get('apiKey','')).strip()
            account = _crystal_account_by_key(api_key)
            if account:
                self._send_text(200,json.dumps({'ok':True,'account':_crystal_account_public(account)},ensure_ascii=False))
            else:
                # Keep old Account API keys working for existing dashboard users.
                account=_account_api_login(api_key)
                self._send_text(200,json.dumps({'ok':True,'account':account},ensure_ascii=False))
        except ValueError as exc:
            self._send_text(401,json.dumps({'error':str(exc)}))
        except Exception as exc:
            print(f'Account login failed: {type(exc).__name__}: {exc}')
            self._send_text(500,json.dumps({'error':'Could not login with Account API.'}))

    def _account_api_http(self):
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = str((query.get('accessToken') or [''])[0]).strip() or str(self.headers.get('X-Access-Token','')).strip()
            guest_id = str((query.get('guestId') or [''])[0]).strip()[:128]
            user = _verify_supabase_access_token(token) if token else None
            if not user and not guest_id:
                self._send_text(401, json.dumps({'error':'Login or Guest account required.'})); return
            account = _account_api_get_or_create(str(user['id']) if user else '', guest_id)
            self._send_text(200, json.dumps({'ok':True,'account':_account_public(account)}, ensure_ascii=False))
        except Exception as exc:
            print(f'Account API get failed: {type(exc).__name__}: {exc}')
            self._send_text(500, json.dumps({'error':'Could not load Account API.'}))

    def _account_api_reset_http(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            body=json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            token=str(body.get('accessToken','')).strip() or str(self.headers.get('X-Access-Token','')).strip()
            guest_id=str(body.get('guestId','')).strip()[:128]
            user=_verify_supabase_access_token(token) if token else None
            if not user and not guest_id:
                self._send_text(401,json.dumps({'error':'Login or Guest account required.'})); return
            account=_account_api_reset(str(user['id']) if user else '',guest_id)
            self._send_text(200,json.dumps({'ok':True,'account':account},ensure_ascii=False))
        except Exception as exc:
            print(f'Account API reset failed: {type(exc).__name__}: {exc}')
            self._send_text(500,json.dumps({'error':'Could not reset Account API.'}))

    def _api_account_status(self):
        try:
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            api_key=str((query.get('apiKey') or [''])[0]).strip()
            if not api_key.startswith('CA-'):
                self._send_text(400,json.dumps({'error':'This endpoint requires an Account API key starting with CA-.'})); return
            account=_api_account_get(api_key)
            if not account:
                self._send_text(404,json.dumps({'error':'Account API key not found.'})); return
            self._send_text(200,json.dumps({'ok':True,'account':_account_public(account)},ensure_ascii=False))
        except Exception as exc:
            print(f'API account status failed: {type(exc).__name__}: {exc}')
            self._send_text(500,json.dumps({'error':'Could not load Account API status.'}))

    def _api_account_reset_http(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>32768:
                self._send_text(400,json.dumps({'error':'Invalid request.'})); return
            body=json.loads(self.rfile.read(length).decode('utf-8'))
            api_key=str(body.get('apiKey','')).strip()
            guest_id=str(body.get('guestId','')).strip()[:128]
            account=_api_account_get(api_key)
            if not account:
                self._send_text(404,json.dumps({'error':'Account API key not found.'})); return
            if guest_id and str(account.get('guest_id') or '')!=guest_id:
                self._send_text(403,json.dumps({'error':'This Account API does not belong to this account.'})); return
            old_key=api_key
            new_key=_account_api_generate()
            while _supabase_rest('crystal_api_accounts', {'select':'id','api_key':'eq.'+urllib.parse.quote(new_key,safe=''),'limit':'1'}):
                new_key=_account_api_generate()
            patch={'api_key':new_key,'discord_user_id':None,'discord_username':None,'linked_at':_iso_now(),'updated_at':_iso_now()}
            rows=_supabase_rest('crystal_api_accounts', {'id':'eq.'+str(account.get('id'))}, 'PATCH', patch, 'return=representation')
            result=_account_public({**account,**patch})
            try:
                _supabase_rest('crystal_discord_users', {'api_key':'eq.'+urllib.parse.quote(old_key,safe='')}, 'DELETE', None, 'return=minimal')
            except Exception:
                pass
            self._send_text(200,json.dumps({'ok':True,'account':result},ensure_ascii=False))
        except PermissionError as exc:
            self._send_text(403,json.dumps({'error':str(exc)}))
        except ValueError as exc:
            self._send_text(404,json.dumps({'error':str(exc)}))
        except Exception as exc:
            print(f'API account reset failed: {type(exc).__name__}: {exc}')
            self._send_text(500,json.dumps({'error':'Could not reset Account API.'}))

    def _admin_script_action(self):
        """Admin dashboard bridge: apply script changes to the same Supabase rows used by Discord."""
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>32768:
                self._send_text(400,json.dumps({'error':'invalid request'})); return
            body=json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body,dict):
                self._send_text(400,json.dumps({'error':'invalid request'})); return
            token=str(body.get('accessToken','')).strip()
            guest_id=str(body.get('guestId','')).strip()[:128]
            user=_verify_supabase_access_token(token) if token else None
            admin=False
            if user:
                admin=_linked_subscription_plan(str(user.get('id'))) == 'Admin'
            elif re.fullmatch(r'[A-Za-z0-9_-]{16,128}',guest_id):
                rows=_supabase_rest('crystal_guest_subscriptions',{'select':'plan,expires_at','guest_id':'eq.'+urllib.parse.quote(guest_id,safe=''),'limit':'1'})
                if rows:
                    plan=str(rows[0].get('plan') or 'Free')
                    exp=rows[0].get('expires_at')
                    active=True
                    if exp:
                        try: active=datetime.datetime.fromisoformat(str(exp).replace('Z','+00:00'))>datetime.datetime.now(datetime.timezone.utc)
                        except Exception: pass
                    admin=active and plan.lower()=='admin'
            if not admin:
                self._send_text(403,json.dumps({'error':'Admin plan required.'})); return
            action=str(body.get('action','')).strip().lower()
            sid=str(body.get('id','')).strip()
            if not sid:
                self._send_text(400,json.dumps({'error':'script id is required'})); return
            q={'select':'id,name,user_id,guest_id,bot_data,access_mode,disabled,source_code,script,api_key,loader_code,discord_user_id,discord_username,settings,key_meta,source_name,source_path'}
            q['id']='eq.'+urllib.parse.quote(sid,safe='')
            rows=_supabase_rest('crystal_scripts',q)
            if not rows:
                self._send_text(404,json.dumps({'error':'Script not found.'})); return
            row=rows[0]
            patch={'updated_at':_iso_now()}
            if action=='set_access':
                mode=str(body.get('access_mode','free')).lower()
                if mode not in ('free','paid'):
                    self._send_text(400,json.dumps({'error':'access_mode must be free or paid'})); return
                patch['access_mode']=mode
                bd=dict(row.get('bot_data') if isinstance(row.get('bot_data'),dict) else {})
                bd['trail_enabled']=(mode=='free')
                if mode=='paid':
                    bd['trail_mode']='PERM'; bd['trail_expires_at']=None
                patch['bot_data']=bd
            elif action=='set_disabled':
                patch['disabled']=bool(body.get('disabled'))
            elif action=='delete':
                _supabase_rest('crystal_scripts',{'id':'eq.'+urllib.parse.quote(sid,safe='')},'DELETE',None,'return=minimal')
                scripts_data.pop(str(row.get('name') or ''),None); save_data()
                self._send_text(200,json.dumps({'ok':True,'deleted':True})); return
            elif action=='sync':
                pass
            else:
                self._send_text(400,json.dumps({'error':'Unknown admin action.'})); return
            _supabase_rest('crystal_scripts',{'id':'eq.'+urllib.parse.quote(sid,safe='')},'PATCH',patch,'return=minimal')
            _sync_dashboard_scripts_from_supabase()
            self._send_text(200,json.dumps({'ok':True,'action':action,'script':row.get('name')}))
        except Exception as exc:
            print(f'Admin dashboard action failed: {type(exc).__name__}: {exc}')
            self._send_text(500,json.dumps({'error':'Admin action failed: '+str(exc)[:300]}))

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == "/api/dashboard-script-save": _dashboard_script_save_http(self); return
        if parsed_path.path == "/api/dashboard-script-delete": _dashboard_script_delete_http(self); return
        if parsed_path.path == "/api/account-key-reset":
            try:
                length=int(self.headers.get("Content-Length","0")); body=json.loads(self.rfile.read(length).decode("utf-8")); account=_dashboard_account_from_request(self,body); fresh=_crystal_account_reset_key(account); self._send_text(200,json.dumps({"ok":True,"account":_crystal_account_public(fresh)},ensure_ascii=False))
            except PermissionError as exc: self._send_text(401,json.dumps({"error":str(exc)}))
            except Exception as exc: self._send_text(500,json.dumps({"error":"Could not reset Account API."}))
            return
        if parsed_path.path == "/api/dashboard-admin-script-action":
            _dashboard_admin_script_action_http(self); return
        if parsed_path.path == "/api/account-create":
            self._account_create_http()
            return
        if parsed_path.path == "/api/account-login":
            self._account_login_http()
            return
        if parsed_path.path == "/api/discord-auth":
            _discord_oauth_login_http(self)
            return
        if parsed_path.path == '/api/account-api-reset':
            self._account_api_reset_http()
            return
        if parsed_path.path == '/api/api-account-reset':
            self._api_account_reset_http()
            return
        if parsed_path.path == "/api/admin-script-action":
            self._admin_script_action()
            return
        if parsed_path.path == "/api/dashboard-script-sync":
            self._dashboard_script_sync()
            return
        if parsed_path.path == "/api/guest-script-sync":
            self._guest_script_sync()
            return
        if parsed_path.path == "/api/guest-script-delete":
            self._guest_script_delete()
            return
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
            reported_country = str(payload.get("country", "Unknown")).strip()[:80] or "Unknown"
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

        # Trial/Free is public: no per-user HWID binding is created for the literal trail key.
        if user_key.strip().lower() == "trail" and trail_is_active(s_info):
            if parsed_path.path == "/script-source":
                self._send_text(200, s_info.get("source_code", "-- No source code loaded"))
            else:
                self._send_text(200, "ok")
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
            # A generated key is single-use: after the first successful script-source
            # request it is consumed and cannot be used by another user/device.
            meta = (s_info.get("key_meta") or {}).get(user_key, {})
            if user_key.strip().lower() != "trail" and not meta.get("consumed"):
                meta["consumed"] = True
                meta["consumed_at"] = _iso_now()
                meta["consumed_hwid"] = str(reported_hwid)[:256]
                meta["consumed_device"] = reported_device
                s_info.setdefault("key_meta", {})[user_key] = meta
                save_data()
            _send_script_webhook(s_info, "Key Used", username="", discord_user_id=str(meta.get("discord_user_id") or ""), key=user_key, device=reported_device, country=reported_country, duration=format_remaining(meta.get("expires_at")))
            _persist_script_row(s_info.get("supabase_row_id"), {"key_meta": s_info.get("key_meta", {}), "updated_at": _iso_now()})
            self._send_text(200, s_info.get("source_code", "-- No source code loaded"))
        else:
            self._send_text(200, "ok")

    def do_GET(self):
        if parsed_path.path == '/api/dashboard-state':
            _dashboard_state_http(self)
            return
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/api/account-api':
            self._account_api_http()
            return
        if parsed_path.path == '/api/api-account':
            self._api_account_status()
            return
        path_parts = parsed_path.path.strip("/").split("/")
        if parsed_path.path == "/api/guest-scripts":
            self._guest_scripts_list()
            return
        if parsed_path.path == "/api/guest-plan":
            self._guest_plan()
            return

        # Unique loader IDs: /files/v4/lua/<loader_code> and /files/v4/<loader_code>.
        if ((len(path_parts) >= 4 and path_parts[0] == "files" and path_parts[1] == "v4" and path_parts[2] == "lua")
                or (len(path_parts) >= 3 and path_parts[0] == "files" and path_parts[1] == "v4")):
            file_id = path_parts[3].strip() if len(path_parts) >= 4 and path_parts[2] == "lua" else path_parts[2].strip()
            script_name = None
            for name, info in scripts_data.items():
                if str(info.get("file_id") or "") == file_id or str(info.get("loader_code") or "") == file_id:
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
            self.send_header("Vary", "Origin")
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
            if bool(scripts_data[script_name].get("disabled", False)):
                self._send_text(403, "The script has been disabled by its owner.")
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
    # Dashboard Free is the authoritative Trial switch. Paid means key-only.
    if str(s_info.get("access_mode") or "free").lower() == "free":
        s_info["trail_enabled"] = True
    if not s_info.get("trail_enabled"):
        return False
    if (s_info.get("trail_mode") or "PERM").upper() == "TIME" and is_expired(s_info.get("trail_expires_at")):
        s_info["trail_enabled"] = False
        s_info["trail_mode"] = "PERM"
        s_info["trail_expires_at"] = None
        s_info["access_mode"] = "paid"
        try:
            save_data()
            owner = str(s_info.get("created_by") or s_info.get("_discord_user_id") or "")
            if owner:
                _sync_script_to_supabase("", s_info, owner, "", None)
        except Exception:
            pass
        return False
    return True


def key_is_usable(s_info: dict, key: str) -> tuple:
    """Return (ok, reason). Rejects disabled or expired keys."""
    key = str(key or "")
    if not key:
        return False, "Key is empty."
    if bool(s_info.get("disabled", False)):
        return False, "The script has been disabled by its owner."
    if key.strip().lower() == "trail" and trail_is_active(s_info):
        return True, ""
    meta = (s_info.get("key_meta") or {}).get(key, {})
    if meta.get("disabled"):
        return False, "This key has been disabled."
    if meta.get("consumed"):
        return False, "This key has already been used by another user."
    if key not in s_info.get("valid_keys", set()) and key not in set((s_info.get("user_keys") or {}).values()) and key not in (s_info.get("key_meta") or {}):
        return False, "Key is not valid for this script."
    if (meta.get("mode") or "PERM").upper() == "TIME" and is_expired(meta.get("expires_at")):
        return False, "This key has expired."
    return True, ""


def user_access_ok(s_info: dict, user_id: int) -> tuple:
    """Check whitelist / key access including TIME expiry. Trial is public while active."""
    if bool(s_info.get("disabled", False)):
        return False, "The script has been disabled by its owner.", None
    if trail_is_active(s_info):
        return True, "", "trail"
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
    active_guild = str(interaction.guild_id or "")
    for script_name, script_info in scripts_data.items():
        # A script is visible only inside the Discord server it is bound to.
        bound_guild = str((script_info or {}).get("guild_id") or "")
        if not active_guild or not bound_guild or bound_guild != active_guild:
            continue
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
    if not interaction.guild_id or is_bot_owner(getattr(interaction.user,"id",0)): return bool(interaction.guild_id)
    allowed,_=_global_admin_status(interaction)
    if not allowed: return False
    if script_name and script_name in scripts_data:
        bound=str((scripts_data.get(script_name) or {}).get("guild_id") or "")
        if bound and bound!=str(interaction.guild_id): return False
    return True

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

    # Enforce per-server script isolation for all slash commands with a script option.
    # Legacy rows without guild_id are intentionally unavailable until explicitly linked.
    if interaction.guild_id:
        def _walk_options(items):
            for item in items or []:
                if isinstance(item, dict):
                    yield item
                    yield from _walk_options(item.get("options", []))
        try:
            raw_options = (interaction.data or {}).get("options", [])
            for option in _walk_options(raw_options):
                if str(option.get("name", "")).lower() in ("script", "script_name"):
                    selected = str(option.get("value", ""))
                    if selected and selected in scripts_data:
                        bound = str((scripts_data.get(selected) or {}).get("guild_id") or "")
                        if bound != str(interaction.guild_id):
                            msg = "❌ This script is not linked to this server. Each Discord server can use only its own scripts."
                            try:
                                if interaction.response.is_done():
                                    await interaction.followup.send(msg, ephemeral=True)
                                else:
                                    await interaction.response.send_message(msg, ephemeral=True)
                            except discord.HTTPException:
                                pass
                            return False
        except Exception as _scope_exc:
            print(f"Crystal guild script guard warning: {type(_scope_exc).__name__}: {_scope_exc}")

    # Gate bot-management slash commands with the server-wide role. Keep user-facing
    # commands (/api, /hwid, /redeem) available to everyone; quota checks remain in place.
    # / has been permanently removed — use /api to link Dashboard account.
    command_name = getattr(getattr(interaction, "command", None), "name", "")
    management_commands = {
        "generate-key", "whitelist", "see-keys", "delete-key",
        "unwhitelist", "trail", "delete-script",
        "set-buyer-role", "update-script", "reset-hwid-cooldown",
        "ai-bot",
        "list-servers", "send-server-message",
    }
    if command_name in management_commands:
        is_admin_tier = _discord_plan(interaction.user.id) == "Admin"
        # /generate-key is intentionally available on every plan, but still requires
        # the configured Admin role or Discord Administrator permission.
        if command_name == "generate-key":
            allowed, message = _global_admin_status(interaction)
            if not allowed:
                try:
                    await interaction.response.send_message(message or "❌ Admin role or Administrator permission required.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False
            return True
        if not (is_bot_owner(interaction.user.id) or is_admin_tier or str(interaction.user.id) in admin_redeemed_users):
            message = "❌ This command requires internal bot authorization."
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
                    await interaction.followup.send(message or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
                else:
                    await interaction.response.send_message(message or "❌ You need the configured admin role or Discord Administrator permission.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
    return True

bot.tree.interaction_check = _global_blacklist_interaction_check


@bot.tree.command(name="bypass", description="Open a URL for a future Crystal Hub bypass integration")
@app_commands.describe(url="URL to process")
async def bypass_command(interaction: discord.Interaction, url: str):
    if not re.match(r"^https?://",str(url or "").strip(),re.I): await interaction.response.send_message("❌ Please provide a valid http(s) URL.",ephemeral=True); return
    await interaction.response.send_message("ℹ️ /bypass is installed, but its actual bypass behavior has not been configured yet.",ephemeral=True)

# ==================== Views & Buttons ==================== #

async def _deny_blacklisted_interaction(interaction: discord.Interaction) -> bool:
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


class ObfuscateConfirmView(BlacklistAwareView):
    def __init__(self, script_name: str, file_content: str):
        super().__init__(timeout=60)
        self.script_name = script_name
        self.file_content = file_content

    @discord.ui.button(label="Yes ✅", style=discord.ButtonStyle.success)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.script_name not in scripts_data:
            await interaction.response.send_message("❌ Script not found.", ephemeral=True)
            return
        quota_ok, quota_error = _quota_check(interaction.user.id, "sources")
        if not quota_ok:
            await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
            return
        old_source = scripts_data[self.script_name].get("source_code")
        scripts_data[self.script_name]["source_code"] = self.file_content
        if save_data() is False:
            scripts_data[self.script_name]["source_code"] = old_source
            await interaction.response.send_message("❌ Could not save the source.", ephemeral=True)
            return
        _quota_increment(interaction.user.id, "sources")
        await interaction.response.send_message(f"⚡ Source file saved & protected for `{self.script_name}`!\\n{_quota_status(interaction.user.id)}", ephemeral=True)

    @discord.ui.button(label="No ❌", style=discord.ButtonStyle.danger)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.script_name not in scripts_data:
            await interaction.response.send_message("❌ Script not found.", ephemeral=True)
            return
        quota_ok, quota_error = _quota_check(interaction.user.id, "sources")
        if not quota_ok:
            await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
            return
        old_source = scripts_data[self.script_name].get("source_code")
        scripts_data[self.script_name]["source_code"] = self.file_content
        if save_data() is False:
            scripts_data[self.script_name]["source_code"] = old_source
            await interaction.response.send_message("❌ Could not save the source.", ephemeral=True)
            return
        _quota_increment(interaction.user.id, "sources")
        await interaction.response.send_message(f"✅ Source file attached for `{self.script_name}`!\\n{_quota_status(interaction.user.id)}", ephemeral=True)


class AddSourceModal(BlacklistAwareModal, title="Crystal Hub — Add Source"):
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
        quota_ok, quota_error = _quota_check(self.owner_id, "sources")
        if not quota_ok:
            await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
            return
        s_info = scripts_data[self.script_name]
        old_source = s_info.get("source_code")
        s_info["source_code"] = str(self.source.value)
        if save_data() is False:
            s_info["source_code"] = old_source
            await interaction.response.send_message("❌ Could not save the source. Please try again.", ephemeral=True)
            return
        _quota_increment(self.owner_id, "sources")
        await interaction.response.send_message(
            f"✅ Source added to `{self.script_name}`.\\n{_quota_status(self.owner_id)}",
            ephemeral=True
        )


class CreateScriptActionsView(BlacklistAwareView):
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


class ScriptRedeemModal(BlacklistAwareModal, title="Crystal Hub — Redeem a Key"):
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


class ViewMobileResponseView(BlacklistAwareView):
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
        if bool(s_info.get("disabled", False)):
            return None, "The script has been disabled by its owner."
        if trail_is_active(s_info):
            ensure_file_id(self.script_name, s_info)
            return "trail", ""
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
        s_info["hwid_bindings"] = {k:v for k,v in hwid_bindings.items() if str(k).startswith(self.script_name + ":")}
        security_state.setdefault("reset_hwid_last_used", {})[cooldown_key] = now
        save_security_state()
        _send_script_webhook(s_info, "HWID Reset", username=getattr(interaction.user, "name", ""), discord_user_id=str(interaction.user.id), key=user_key, device="Unknown", duration=format_remaining((s_info.get("key_meta") or {}).get(user_key, {}).get("expires_at")))
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
                description = panel.get("description") or info.get("panel_description") or f"This control panel is for the project: **{script_name}. Script**\n\nIf you're a buyer, click on the buttons below to redeem your key, get the script or get your role"
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

def _push_all_local_scripts_to_supabase():
    """Backfill and continuously persist every bot-created script, including unlinked users."""
    pushed = 0
    for name, info in list(scripts_data.items()):
        owner = str(info.get("created_by") or info.get("discord_user_id") or "").strip()
        if not owner:
            continue
        username = str(info.get("discord_username") or "")
        if _sync_script_to_supabase(name, info, owner, username, info.get("guild_id")):
            pushed += 1
    return pushed


@bot.event
async def _crystal_dashboard_sync_loop():
    # Import remote state first so a redeploy never overwrites newer Supabase data
    # with an old local snapshot. Then push the complete local state back.
    while True:
        await asyncio.sleep(20)
        try:
            imported_count = await asyncio.to_thread(_sync_dashboard_scripts_from_supabase)
            pushed_count = await asyncio.to_thread(_push_all_local_scripts_to_supabase)
            print(f"Crystal durable sync: restored {imported_count}, persisted {pushed_count} script(s).")
            if scripts_data and pushed_count == 0:
                print("WARNING: no local scripts were confirmed saved to Supabase. Check SUPA_PRIVATE and crystal_scripts columns/permissions.")
        except Exception as exc:
            print(f"Crystal durable sync failed: {type(exc).__name__}: {exc}")


async def _sync_global_commands():
    try:
        synced = await bot.tree.sync()
        print(f"Crystal global slash sync: {len(synced)} commands.")
        print("Crystal global commands:", ", ".join(sorted(str(c.name) for c in synced)))
        return synced
    except Exception as exc:
        print(f"Crystal global slash sync failed: {type(exc).__name__}: {exc}")
        return []

@bot.event
async def on_ready():
    print(f"Crystal Hub build: {BUILD_TAG}")
    # Do not register these finite-timeout views with bot.add_view().
    # They are attached directly when their messages are sent.
    # Global-only registration prevents every slash command from appearing twice.
    # Discord guild copies are intentionally NOT created.
    await _sync_global_commands()
    await asyncio.to_thread(_load_admin_roles_from_supabase)
    print(f"Bot connected successfully as: {bot.user}")
    await asyncio.to_thread(_sync_dashboard_scripts_from_supabase)
    # Backfill older local scripts into Supabase immediately on first startup.
    await asyncio.to_thread(_push_all_local_scripts_to_supabase)
    if not getattr(bot, "_crystal_dashboard_sync_started", False):
        bot._crystal_dashboard_sync_started = True
        asyncio.create_task(_crystal_dashboard_sync_loop())
    if not getattr(bot, "_crystal_panels_refreshed_this_process", False):
        bot._crystal_panels_refreshed_this_process = True
        await _refresh_saved_panels_after_startup()

@bot.event
async def on_guild_join(guild):
    # Commands are global; do not create guild-specific copies.
    print(f"Crystal Hub joined guild {guild.id} ({guild.name}); global slash commands are used.")


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
    # /generate-key is available to Free/Premium/Ultra users too, but only
    # when the user has the configured Admin role or Discord Administrator permission.
    role_ok = has_admin_editor_role(interaction) or is_guild_administrator(interaction) or _has_global_admin_access(interaction)
    if not (is_bot_owner(interaction.user.id) or role_ok):
        await interaction.response.send_message("❌ You need the configured Admin role or Discord Administrator permission to generate keys.", ephemeral=True)
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
    new_key = generate_random_key()
    register_key(scripts_data[script], new_key, mode, expires_at, created_by=interaction.user.id)
    save_data()
    _sync_script_to_supabase(script, scripts_data[script], str(interaction.user.id), getattr(interaction.user, "name", ""), interaction.guild_id)
    when = "permanent" if mode == "PERM" else f"expires in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"🔑 **New Key Generated for `{script}`** ({mode} — {when}):\n```text\n{new_key}\n```\n\n{_quota_status(interaction.user.id)}",
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
    panel_link=""
    for panel in (s_info.get("panel_instances") or []):
        if str(panel.get("guild_id"))==str(interaction.guild_id) and panel.get("channel_id") and panel.get("message_id"):
            panel_link=f"\n\nPanel: https://discord.com/channels/{interaction.guild_id}/{panel.get('channel_id')}/{panel.get('message_id')}"; break
    await interaction.response.send_message(f"{user.mention} You have been whitelisted!\n\nScript: `{script}` ({when})\n{dm_status}{panel_link}")


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
        s_info["access_mode"] = "paid"
        save_data()
        _sync_script_to_supabase(script, s_info, str(interaction.user.id), getattr(interaction.user, "name", ""), interaction.guild_id)
        await interaction.response.send_message(f"✅ Trail **OFF** for `{script}` — Paid/Key access is active again on Dashboard and Discord.", ephemeral=True)
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
    s_info["access_mode"] = "free"
    ensure_file_id(script, s_info)
    save_data()
    _sync_script_to_supabase(script, s_info, str(interaction.user.id), getattr(interaction.user, "name", ""), interaction.guild_id)
    when = "permanent" if mode == "PERM" else f"TIME — ends in {format_remaining(expires_at)}"
    await interaction.response.send_message(
        f"✅ Trail **ON** for `{script}` ({when}) — Free access is now active on Dashboard and Discord.",
        ephemeral=True
    )


# 3. إنشاء السكربت
@bot.tree.command(name="create-script", description="Create a new script entry")
@app_commands.describe(
    name_script="Required: the name to use for this script",
    buyer_role="Select buyer role (optional when API is provided)",
    api="Paste a Crystal Hub API key (CH-...) or a full script API URL."
)
async def create_script_command(
    interaction: discord.Interaction,
    name_script: str,
    buyer_role: Optional[discord.Role] = None,
    api: Optional[str] = None,
):
    # Script creation is available to everyone. Free users use FREE_LIMITS;
    # Premium gets its plan limits; Ultra/Admin have unlimited quota.
    quota_ok, quota_error = _quota_check(interaction.user.id, "scripts")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
        return

    name_script = (name_script or "").strip()
    if not name_script:
        await interaction.response.send_message("❌ Script name is required.", ephemeral=True)
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
        # Keep the requested name, but clone source/config from the API entry.
        buyer_role_id = imported.get("buyer_role_id")
        admin_role_id = _global_admin_role_id(interaction.guild.id if interaction.guild else None)
        if buyer_role is not None:
            buyer_role_id = buyer_role.id
        source_code = imported["source_code"]
        version = imported["version"]
    else:
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
    if name_script in scripts_data:
        await interaction.response.send_message(
            "❌ A script with that name already exists. Choose a unique name so you do not overwrite another user's script.",
            ephemeral=True,
        )
        return

    api_key = (imported.get("_api_key") if imported and imported.get("_api_key") else "CH-" + secrets.token_urlsafe(24))
    scripts_data[name_script] = {
        "version": version,
        "buyer_role_id": buyer_role_id,
        "admin_role_id": admin_role_id,
        "source_code": source_code,
        "valid_keys": set(),
        "whitelisted_users": set(),
        "user_keys": {},
        "created_by": str(interaction.user.id),
        "guild_id": str(interaction.guild.id) if interaction.guild else None,
        "api_key": api_key,
        "supabase_user_id": (imported or {}).get("_supabase_user_id"),
        "supabase_row_id": (imported or {}).get("_supabase_row_id"),
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
            "❌ The script could not be saved to storage. On Render, attach a persistent disk and set CRYSTAL_HUB_DATA_DIR=/var/data.",
            ephemeral=True,
        )
        return

    _sync_script_to_supabase(name_script, scripts_data[name_script], str(interaction.user.id),
                             str(interaction.user), interaction.guild.id if interaction.guild else None)

    embed = discord.Embed(
        title="✅ Script Registered Successfully!",
        description=(
            f"📜 **Script Name:** `{name_script}`\n"
            f"👤 **Buyer Role:** {f'<@&{buyer_role_id}>' if buyer_role_id else 'Not configured'}\n"
            f"🛡️ **Admin Role:** {f'<@&{admin_role_id}>' if admin_role_id else 'Not configured'}\n"
            f"🔗 **Source:** {'Imported from API' if imported else 'Created manually'}\n\n{_quota_status(interaction.user.id)}"
        ),
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, view=CreateScriptActionsView(name_script, interaction.user.id), ephemeral=True)
    await interaction.followup.send(
        f"🔐 **Private API key for {name_script}** — save it securely. It is stored with the script and survives bot restarts/updates:\\n``{api_key}``",
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

    deleted_info = scripts_data.get(script, {})
    row_id = deleted_info.get("supabase_row_id")
    api_key = str(deleted_info.get("api_key") or "").strip()
    del scripts_data[script]
    save_data()
    try:
        if row_id:
            _supabase_rest("crystal_scripts", {"id": "eq." + str(row_id)}, "DELETE", None, "return=minimal")
        elif api_key:
            _supabase_rest("crystal_scripts", {"api_key": "eq." + urllib.parse.quote(api_key, safe="")}, "DELETE", None, "return=minimal")
    except Exception as exc:
        print(f"Crystal Supabase delete warning for {script}: {type(exc).__name__}: {exc}")
    await interaction.response.send_message(
        f"🗑️ Deleted `{script}` from the bot database. Its `/script/{urllib.parse.quote(script)}` route now returns 404.",
        ephemeral=True
    )


# 4. ربط السورس
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

        # Privacy: do not transmit a user's original source to the bot owner in the background.
        # Source is processed and saved only as part of the user's selected Source Setup action.

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
                content=f"❌ {'Obfuscation' if obfuscate else 'Source preparation'} failed: `{type(exc).__name__}: {str(exc)[:700]}`.",
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
                content=f"❌ Could not save the source: `{type(exc).__name__}: {str(exc)[:700]}`.",
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
                    content=f"❌ Obfuscation completed and saved, but sending the hosted link failed: `{type(exc).__name__}: {str(exc)[:700]}`.",
                    view=self,
                )
                return

        # Only report success after processing, storage, and (for Yes) channel delivery succeed.
        await interaction.response.edit_message(
            content=(
                ("✅ Script obfuscated successfully!" if obfuscate else "✅ Source saved successfully (not obfuscated).")
                + f"\n📁 Script: `{self.script}`"
                + f"\n🔐 Protection: **{mode_text}**"
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
        "If you choose **No**, the source will be stored normally.\n\n",
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
# Prefix-only admin command: -set-admin-role @role
@bot.tree.command(name="admin-role", description="Set the server-wide role allowed to manage Crystal Hub")
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
    _save_admin_role_to_supabase(interaction.guild.id, admin_role.id)

    await interaction.response.send_message(
        f"✅ Server-wide admin role set to {admin_role.mention}. This role can manage bot features across all scripts. "
        "It does **not** bypass Free/Premium/Ultra quotas or redeem-code requirements.",
        ephemeral=True,
    )


# API account linking — connects a Dashboard API key to this Discord account.
@bot.tree.command(name="api", description="Link your Discord account to your Crystal Hub Account API")
@app_commands.describe(
    acc="Your standalone Crystal Hub Account API key from Dashboard Settings (CA-...)",
    unlink="Unlink this Discord account from the Account API"
)
async def api_account_command(interaction: discord.Interaction, acc: Optional[str] = None, unlink: bool = False):
    acc = str(acc or '').strip()
    try:
        if unlink:
            acct = await asyncio.to_thread(_crystal_account_by_discord, str(interaction.user.id))
            if not acct:
                acct = await asyncio.to_thread(_account_api_get_by_discord, str(interaction.user.id))
            if not acct:
                await interaction.response.send_message('❌ No Account API is linked to your Discord account.', ephemeral=True); return
            if acc and str(acct.get('api_key') or '') != acc:
                await interaction.response.send_message('❌ The supplied Account API does not match your linked account.', ephemeral=True); return
            if acct.get('guest_id'):
                await asyncio.to_thread(_supabase_rest, 'crystal_accounts', {'guest_id':'eq.'+urllib.parse.quote(str(acct.get('guest_id')),safe='')}, 'PATCH', {'discord_user_id':None,'discord_username':None}, 'return=minimal')
            await interaction.response.send_message('✅ Account API unlinked. Your scripts and Script API keys were not changed.', ephemeral=True); return
        if not acc:
            acct = await asyncio.to_thread(_crystal_account_by_discord, str(interaction.user.id))
            if acct and acct.get('api_key'):
                await interaction.response.send_message(
                    '🔑 **Your Crystal Hub Account API**\n'
                    f"Username: `{acct.get('website_username') or 'Unknown'}`\n"
                    f"Account API: `{acct.get('api_key')}`\n"
                    'Keep this key private. It is different from Script API keys (`CH-...`).', ephemeral=True); return
            legacy = await asyncio.to_thread(_account_api_get_by_discord, str(interaction.user.id))
            if legacy and legacy.get('api_key'):
                await interaction.response.send_message(f"🔑 **Your Account API:** `{legacy['api_key']}`\nKeep this key private.", ephemeral=True); return
            await interaction.response.send_message('❌ No Crystal Hub account is linked to this Discord ID yet. Sign in with Discord on the Crystal Login page first, then run `/api` again.', ephemeral=True); return
        if not acc.startswith('CA-'):
            await interaction.response.send_message('❌ `/api` needs your Account API key starting with `CA-`. Script API keys start with `CH-` and cannot be used here.', ephemeral=True); return
        account = await asyncio.to_thread(_account_api_link, acc, str(interaction.user.id), str(interaction.user))
        # Keep the Dashboard account record linked as well, so Dashboard server/plan data uses this Discord identity.
        await asyncio.to_thread(_supabase_rest, 'crystal_accounts', {'api_key':'eq.'+urllib.parse.quote(acc,safe='')}, 'PATCH', {'discord_user_id':str(interaction.user.id),'discord_username':str(interaction.user)[:100]}, 'return=minimal')
        await asyncio.to_thread(_sync_dashboard_scripts_from_supabase)
        await interaction.response.send_message('✅ **Account API linked.** Your Discord and Dashboard accounts are now connected. Your Script API keys (`CH-...`) remain separate.', ephemeral=True)
    except ValueError as exc:
        await interaction.response.send_message(f'❌ {exc}', ephemeral=True)
    except Exception as exc:
        print(f'/api account error: {type(exc).__name__}: {exc}')
        await interaction.response.send_message('❌ Could not complete `/api`. Check the Supabase connection and bot logs.', ephemeral=True)

@bot.tree.command(name="hwid", description="Reset HWID binding so your key can lock to a new device")
@app_commands.describe(script="Optional: one script only. Leave empty to reset HWID for all your keys.")
@app_commands.autocomplete(script=script_autocomplete)
async def hwid_command(interaction: discord.Interaction, script: Optional[str] = None):
    uid = str(interaction.user.id)
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    last_used = int((security_state.get("reset_hwid_last_used") or {}).get(uid, 0) or 0)
    cooldown = int(security_state.get("reset_hwid_cooldown", DEFAULT_RESET_HWID_COOLDOWN) or 0)
    if cooldown > 0 and last_used > 0 and now - last_used < cooldown:
        remaining = cooldown - (now - last_used)
        await interaction.response.send_message(
            f"⏳ You can reset your HWID again in **{format_duration(remaining)}**.",
            ephemeral=True
        )
        return

    target_name = str(script or "").strip()
    if target_name and target_name not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return

    removed = 0
    touched_scripts = []
    for name, info in scripts_data.items():
        if target_name and name != target_name:
            continue
        meta = info.get("key_meta") if isinstance(info.get("key_meta"), dict) else {}
        access = info.get("user_access") if isinstance(info.get("user_access"), dict) else {}
        keys_for_user = set()
        for key_value, key_meta in meta.items():
            if isinstance(key_meta, dict) and str(key_meta.get("discord_user_id") or "") == uid:
                keys_for_user.add(str(key_value))
        access_entry = access.get(uid)
        if isinstance(access_entry, dict):
            for field in ("key", "api_key", "script_key"):
                if access_entry.get(field):
                    keys_for_user.add(str(access_entry[field]))
        elif isinstance(access_entry, str) and access_entry.strip():
            keys_for_user.add(access_entry.strip())

        for key_value in keys_for_user:
            binding_key = f"{name}:{key_value}"
            if binding_key in hwid_bindings:
                hwid_bindings.pop(binding_key, None)
                removed += 1
        if keys_for_user:
            touched_scripts.append(name)
            try:
                _persist_script_row(info.get("supabase_row_id"), {
                    "bot_data": {
                        "version": info.get("version", "1.0.0"),
                        "buyer_role_id": info.get("buyer_role_id"),
                        "admin_role_id": info.get("admin_role_id"),
                        "created_by": str(info.get("created_by") or ""),
                        "trail_enabled": bool(info.get("trail_enabled", False)),
                        "trail_mode": info.get("trail_mode", "PERM"),
                        "trail_expires_at": info.get("trail_expires_at"),
                        "user_access": info.get("user_access") if isinstance(info.get("user_access"), dict) else {},
                        "hwid_bindings": {k: v for k, v in hwid_bindings.items() if str(k).startswith(str(name) + ":")}
                    },
                    "updated_at": _iso_now()
                })
            except Exception:
                pass

    security_state.setdefault("reset_hwid_last_used", {})[uid] = now
    try:
        save_hwid_bindings()
        save_security_state()
    except OSError:
        await interaction.response.send_message("❌ Could not save the HWID reset. Please try again.", ephemeral=True)
        return

    if removed:
        scope = f"for **{', '.join(touched_scripts[:5])}**" if touched_scripts else ""
        more = " and more" if len(touched_scripts) > 5 else ""
        await interaction.response.send_message(
            f"✅ HWID reset successfully {scope}{more}. You can now run your key from the new device.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "ℹ️ No HWID binding was found for your account. Your next key use will bind normally.",
            ephemeral=True
        )


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
            "❌ Only a server administrator can use this command.", ephemeral=True
        )
        return
    if not avatar_url and not nickname and bio is None:
        await interaction.response.send_message(
            "❌ Provide an avatar URL, a nickname, or bio text.", ephemeral=True
        )
        return

    quota_ok, quota_error = _quota_check(interaction.user.id, "avatars")
    if not quota_ok:
        await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
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
                            "❌ Could not download the image URL. Use a direct public image URL.",
                            ephemeral=True,
                        )
                        return
                    image_bytes = await response.read()
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()

            if len(image_bytes) > 8 * 1024 * 1024:
                await interaction.response.send_message("❌ Image must be under 8 MB.", ephemeral=True)
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
                "❌ Could not download the image URL. Check that it is a direct public image URL.",
                ephemeral=True,
            )
            return

    if bio is not None:
        bio = bio.strip()
        if not bio:
            await interaction.response.send_message("❌ Bio text cannot be empty.", ephemeral=True)
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
                f"❌ The final server bio is {len(combined)}/190 characters. "
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
                "❌ Discord rejected the server profile update. Make sure the bot can change its nickname/profile in this server.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"❌ Discord could not update the server profile (HTTP {exc.status}).",
                ephemeral=True,
            )
        return
    except Exception as exc:
        print(f"avatar-bot server profile update failed: {exc}")
        await interaction.response.send_message(
            "❌ Could not update the bot's server profile.", ephemeral=True
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
        "✅ Crystal Hub: " + ", ".join(changes) +
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
            await interaction.response.send_message("❌ " + quota_error, ephemeral=True)
            return

    ensure_file_id(script, s_info)
    panel_description = (str(description).strip()[:4000] if description is not None
                         else (matching.get("description") if matching else None)
                         or s_info.get("panel_description")
                         or f"This control panel is for the project: **{script}. Script**\n\nIf you're a buyer, click on the buttons below to redeem your key, get the script or get your role")
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
        f"✅ Panel ready in {target_channel.mention}. This panel is saved and will be refreshed after the bot restarts/updates.\n\n{_quota_status(interaction.user.id)}",
        ephemeral=True
    )


# The old /panel-delete slash command is intentionally unregistered.
async def panel_delete_command(
    interaction: discord.Interaction,
    script: str,
    channel: discord.TextChannel,
):
    if script not in scripts_data:
        await interaction.response.send_message("❌ Script not found.", ephemeral=True)
        return
    if not is_admin_or_has_role(interaction, script):
        await interaction.response.send_message("❌ You do not have permission to delete this panel.", ephemeral=True)
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
        await interaction.response.send_message("❌ No panel you own was found in that channel.", ephemeral=True)
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
    await interaction.response.send_message(f"✅ Panel deleted from the saved inventory. Its quota slot is now free.\n\n{_quota_status(interaction.user.id)}", ephemeral=True)


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
# Prefix-only admin command: -reset-hwid-cooldown seconds
# @bot.tree.command(name="reset-hwid-cooldown", description="Set the minimum wait between HWID reset requests")
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
        "ai_chat_channels": sorted(int(x) for x in AI_CHAT_CHANNELS),
        "site_redeem_codes": site_redeem_codes,
        "site_entitlements": site_entitlements,
        "monthly_usage": monthly_usage,
        "known_users": sorted(KNOWN_USERS),
        "ai_information": AI_INFORMATION,
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

    if isinstance(snapshot.get("ai_chat_channels"), list):
        AI_CHAT_CHANNELS = {int(x) for x in snapshot["ai_chat_channels"] if str(x).isdigit()}
        _save_ai_chat_channels()
    if isinstance(snapshot.get("ai_information"), dict):
        AI_INFORMATION.clear()
        AI_INFORMATION.update({str(g): str(v)[:12000] for g, v in snapshot["ai_information"].items() if str(g).isdigit() and isinstance(v, str)})
        _save_ai_information()

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
        _supabase_upsert_subscription(str(interaction.user.id), plan, expires_at, "discord-redeem", None)

    save_data()

def _persist_admin_plan_for_discord(discord_user_id: str):
    """Mirror a successful Admin redeem into the durable account record.
    This makes Dashboard Admin state survive Render restarts and appear on every login.
    """
    did = str(discord_user_id or '').strip()
    if not did.isdigit():
        return False
    try:
        rows = _supabase_rest("crystal_accounts", {
            "select": "guest_id,plan",
            "discord_user_id": "eq." + urllib.parse.quote(did, safe=""),
            "limit": "1"
        })
        if not rows:
            return False
        gid = str(rows[0].get("guest_id") or "").strip()
        if not gid:
            return False
        _supabase_rest("crystal_accounts", {
            "guest_id": "eq." + urllib.parse.quote(gid, safe="")
        }, "PATCH", {"plan": "Admin", "plan_expires_at": None, "updated_at": _iso_now()}, "return=minimal")
        return True
    except Exception as exc:
        print(f"Admin plan persistence warning: {type(exc).__name__}: {exc}")
        return False

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
                "❌ This action requires internal authorization from the bot owner.",
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
        # Also persist the Admin entitlement in Supabase so the Dashboard sees it
        # immediately and the entitlement survives Render restarts/redeploys.
        persisted = await asyncio.to_thread(_persist_admin_plan_for_discord, user_id)
        if not persisted:
            print(f"WARNING: Admin redeem for {user_id} was saved locally but no durable crystal_accounts row was updated.")
        await interaction.response.send_message("✅ Internal authorization activated. Admin is now enabled for your Crystal account.", ephemeral=True)
        return

    code_hash = _hash_redeem_code(supplied)
    rec = discord_plan_codes.get(code_hash)
    if not rec or rec.get("redeemed_by"):
        await interaction.response.send_message("❌ Invalid or already-used Crystal Hub code.", ephemeral=True)
        return

    plan = str(rec.get("plan", "Premium"))
    if plan not in ("Premium", "Ultra"):
        await interaction.response.send_message("❌ This code does not grant a customer subscription.", ephemeral=True)
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



# Per-server owner-written AI instructions/context, persisted across restarts.
AI_INFORMATION_FILE = "crystal_ai_information.json"
def _load_ai_information():
    try:
        with open(AI_INFORMATION_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        rows = raw.get("guilds", {}) if isinstance(raw, dict) else {}
        return {str(g): str(v)[:12000] for g, v in rows.items() if str(g).isdigit() and isinstance(v, str)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
AI_INFORMATION = _load_ai_information()
def _save_ai_information():
    try:
        with open(AI_INFORMATION_FILE, "w", encoding="utf-8") as f:
            json.dump({"guilds": AI_INFORMATION}, f, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        print(f"Could not save AI Information: {exc}")
        return False

# AI chat reply channels are configured with /ai-bot.
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

# AI Information is the only editable server-specific AI knowledge store.


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
        return "Set the server-wide admin role first using `/admin-role`, then try again."
    return "This command requires the configured admin role or Discord Administrator permission."


@bot.command(name="files")
async def import_all_user_files(ctx: commands.Context):
    """Admin-only full import: attach a Crystal Hub Admin backup after the prompt."""
    user_id = int(ctx.author.id)
    if not (is_bot_owner(user_id) or str(user_id) in admin_redeemed_users or _discord_plan(user_id) == "Admin"):
        await ctx.reply("❌ This command is for the internal Admin rank only.", mention_author=False)
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
            "📁 **Send the full Crystal Hub backup file now** (JSON). I'll wait up to 2 minutes.\n"
            "Get the file using `-g files`. Import replaces the saved bot data with the backup, "
            "including users' plans, usage, scripts, keys, whitelist, and settings.",
            mention_author=False,
        )
        try:
            upload_message = await bot.wait_for("message", check=valid_upload, timeout=120)
            attachment = upload_message.attachments[0]
        except asyncio.TimeoutError:
            await ctx.reply("⌛ Import cancelled: no file was received within 2 minutes.", mention_author=False)
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
            f"✅ Full backup imported successfully. Restored **{users_count}** known users and **{scripts_count}** scripts, "
            "including saved plan entitlements, monthly usage, keys, whitelist, panels, and settings.",
            mention_author=False,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        await ctx.reply(f"❌ Could not import this backup: {exc}", mention_author=False)
    except Exception as exc:
        print(f"Admin full import failed: {type(exc).__name__}: {exc}")
        await ctx.reply("❌ Import failed. The current data may be unchanged; check the bot logs.", mention_author=False)


@bot.command(name="g")
async def get_admin_files_backup(ctx: commands.Context, item: str = ""):
    """Admin-only full export invoked as `-g files`."""
    user_id = int(ctx.author.id)
    if not (is_bot_owner(user_id) or str(user_id) in admin_redeemed_users or _discord_plan(user_id) == "Admin"):
        await ctx.reply("❌ This command is for the internal Admin rank only.", mention_author=False)
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
            f"📦 Full Admin backup: **{users_count}** known/entitled users, **{scripts_count}** scripts, "
            f"**{key_count}** stored keys, and **{whitelist_count}** script whitelist entries. "
            "Includes saved plans, monthly usage, source/config, panels, keys, whitelist/access records, redeem records, "
            "and settings. It includes sensitive data; keep it private.",
            file=discord.File(io.BytesIO(raw), filename="crystal_hub_all_users_admin_backup.json"),
            mention_author=False,
        )
    except Exception as exc:
        print(f"Admin full export failed: {type(exc).__name__}: {exc}")
        await ctx.reply("❌ Could not create the full backup. Check the bot logs.", mention_author=False)


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
    text = text.translate(str.maketrans({"أ":"ا", "إ":"ا", "آ":"ا", "ى":"ي", "ة":"ه", "ؤ":"و", "ئ":"ي"}))
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# AI FAQ support (OpenRouter primary, then Gemini/OpenAI if configured). Keep all API keys in host environment variables.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip()
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "https://crystal-hub67.netlify.app/").strip()
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME", "Crystal Hub").strip()
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


def _crystal_call_openrouter(prompt: str) -> str:
    """Call OpenRouter using a server-side environment key; never hard-code secrets."""
    if not OPENROUTER_API_KEY:
        return ""
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": (
                "You are Crystal Hub Discord support. Answer in the same language as the user's message. "
                "Be concise, friendly, and grounded only in the supplied context. Never invent prices, "
                "features, scripts, or purchase availability. If asked how to buy, direct them to the "
                "server's #buy channel and tell them to open a ticket; include the supplied invite. "
                "If details are unknown, say staff can confirm."
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_tokens": 500,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": OPENROUTER_SITE_NAME,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=22) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()


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


async def _crystal_ai_answer(content: str, history=None, guild_id=None) -> str:
    context_data = _crystal_ai_context()
    if guild_id is not None:
        context_data["server_owner_ai_information"] = AI_INFORMATION.get(str(guild_id), "")
    context = json.dumps(context_data, ensure_ascii=False)
    history_text = "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in (history or [])[-8:])
    prompt = (
        "You are Crystal Hub's friendly Discord AI chat assistant. Understand paraphrases, typos, follow-up messages, and casual conversation. "
        "Reply in the same language as the user (Arabic if their message is Arabic, otherwise English). Keep replies concise and natural. "
        "Use only this context for product facts; never invent prices or undocumented features. Only describe Free, Premium, and Ultra plans; never mention internal/admin-only plans or access. "
        "Premium costs $1 USD for lifetime access; Ultra costs $2 USD for lifetime access. For purchases, direct users to the server's #buy channel and tell them to open a ticket.\n\n"
        f"TRUSTED CRYSTAL HUB CONTEXT:\n{context}\n\nRECENT CHANNEL CONVERSATION:\n{history_text or '(no previous conversation)'}\n\nLATEST USER MESSAGE:\n{content[:1500]}"
    )
    # Use OpenRouter first when configured, then optional Gemini/OpenAI fallbacks.
    providers = (
        (_crystal_call_openrouter, OPENROUTER_API_KEY),
        (_crystal_call_gemini, GEMINI_API_KEY),
        (_crystal_call_openai, OPENAI_API_KEY),
    )
    for provider, api_key in providers:
        if not api_key:
            continue
        try:
            answer = await asyncio.to_thread(provider, prompt)
            if answer:
                return answer[:1900]
        except Exception as exc:
            print(f"Crystal Hub AI provider {provider.__name__} failed: {type(exc).__name__}: {exc}")
    return ""


@bot.tree.command(name="ai-information", description="Save instructions and server-owner information for the AI")
@app_commands.describe(information="Information/instructions the AI should use in this server")
async def ai_information_command(interaction: discord.Interaction, information: str):
    plan = _discord_plan(interaction.user.id)
    if plan not in ("Admin", "Ultra"):
        await interaction.response.send_message("❌ This command requires an **Admin or Ultra** subscription.", ephemeral=True)
        return
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Use this command inside a server.", ephemeral=True)
        return
    has_role = _has_global_admin_access(interaction)
    is_discord_admin = interaction.user.guild_permissions.administrator
    if not (has_role or is_discord_admin):
        await interaction.response.send_message("❌ You need the configured **Admin role** or Discord **Administrator** permission.", ephemeral=True)
        return
    text_value = information.strip()
    if not text_value:
        await interaction.response.send_message("❌ Enter the information you want the AI to remember.", ephemeral=True)
        return
    AI_INFORMATION[str(interaction.guild.id)] = text_value[:12000]
    if not _save_ai_information():
        await interaction.response.send_message("❌ Could not save AI Information. Check the bot storage.", ephemeral=True)
        return
    await interaction.response.send_message("✅ Saved. The AI will use this information in conversations in this server.", ephemeral=True)


@bot.tree.command(name="ai-bot", description="Turn AI replies on or off in a channel")
@app_commands.describe(
    mode="Turn AI replies on or off",
    reply_channel="Channel where members can ask the bot and receive replies",
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
):

    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if mode.value == "on":
        if reply_channel is None:
            await interaction.response.send_message(
                "Choose a **Reply Channel** where members can talk to the AI.",
                ephemeral=True,
            )
            return

        AI_CHAT_CHANNELS.add(reply_channel.id)
        _AI_CHAT_HISTORY[reply_channel.id] = []
        _save_ai_chat_channels()
        await interaction.response.send_message(
            f"AI replies are now **ON** in {reply_channel.mention}.",
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



# /explain tutorial videos (same three videos used by the Dashboard tutorial)
EXPLAIN_VIDEO_FILES = {
    "website": "videos/create-script-in-website.mp4",
    "commands": "videos/how-to-create-script-by-commands.mp4",
    "key": "videos/how-to-create-key-or-whitelist.mp4",
}


def _explain_video_path(key: str):
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, EXPLAIN_VIDEO_FILES[key])


class ExplainVideosView(BlacklistAwareView):
    """Three tutorial videos matching the Dashboard Tutorial videos."""
    def __init__(self, language="en"):
        super().__init__(timeout=900)
        self.language = language
        self.website_btn.label = "إنشاء سكربت من الموقع" if language == "ar" else "Create script in website"
        self.commands_btn.label = "إنشاء سكربت بالأوامر" if language == "ar" else "How to create script by commands"
        self.key_btn.label = "إنشاء Key أو Whitelist" if language == "ar" else "How to create key or whitelist"
        self.back_btn.label = "رجوع" if language == "ar" else "Back"
        self.language_btn.label = "English" if language == "ar" else "العربية"

    async def _send_video(self, interaction, key: str):
        path = _explain_video_path(key)
        if not os.path.isfile(path):
            msg = "الفيديو غير موجود في ملفات البوت." if self.language == "ar" else "This tutorial video is missing from the bot files."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        labels = {
            "website": ("إنشاء سكربت من الموقع" if self.language == "ar" else "Create script in website"),
            "commands": ("إنشاء سكربت بالأوامر" if self.language == "ar" else "How to create script by commands"),
            "key": ("إنشاء Key أو Whitelist" if self.language == "ar" else "How to create key or whitelist"),
        }
        await interaction.response.send_message(
            content=f"🎥 **{labels[key]}**",
            file=discord.File(path, filename=os.path.basename(path)),
            ephemeral=False,
        )

    @discord.ui.button(label="Create script in website", style=discord.ButtonStyle.primary, custom_id="crystal_explain_video_website")
    async def website_btn(self, interaction, button):
        await self._send_video(interaction, "website")

    @discord.ui.button(label="How to create script by commands", style=discord.ButtonStyle.primary, custom_id="crystal_explain_video_commands")
    async def commands_btn(self, interaction, button):
        await self._send_video(interaction, "commands")

    @discord.ui.button(label="How to create key or whitelist", style=discord.ButtonStyle.primary, custom_id="crystal_explain_video_key")
    async def key_btn(self, interaction, button):
        await self._send_video(interaction, "key")

    @discord.ui.button(label="Back", emoji="◀", style=discord.ButtonStyle.secondary, custom_id="crystal_explain_video_back")
    async def back_btn(self, interaction, button):
        await interaction.response.edit_message(
            embed=build_explain_home_embed(self.language),
            view=ExplainHomeView(self.language),
        )

    @discord.ui.button(label="العربية", emoji="🌐", style=discord.ButtonStyle.secondary, custom_id="crystal_explain_video_language")
    async def language_btn(self, interaction, button):
        self.language = "ar" if self.language == "en" else "en"
        await interaction.response.edit_message(
            embed=build_explain_videos_embed(self.language),
            view=ExplainVideosView(self.language),
        )

class ExplainHomeView(BlacklistAwareView):
    """Public bilingual /explain menu. Every screen includes its own language toggle."""
    def __init__(self, language="en"):
        super().__init__(timeout=900)
        self.language = language
        if language == "ar":
            self.script_btn.label = "شرح السكربت"
            self.website_btn.label = "شرح الموقع"
            self.bot_btn.label = "شرح البوت"
            self.commands_btn.label = "الأوامر"
            self.videos_btn.label = "الفيديوهات"
            self.language_btn.label = "English"
        else:
            self.script_btn.label = "Script Tutorial"
            self.website_btn.label = "Website Tutorial"
            self.bot_btn.label = "Bot Tutorial"
            self.commands_btn.label = "Commands"
            self.videos_btn.label = "Videos"
            self.language_btn.label = "العربية"

    async def _open(self, interaction: discord.Interaction, topic: str):
        await interaction.response.edit_message(
            embed=build_explain_embed(topic, self.language),
            view=ExplainPageView(topic, self.language),
        )

    @discord.ui.button(label="Script Tutorial", style=discord.ButtonStyle.primary, custom_id="crystal_explain_script")
    async def script_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "script")

    @discord.ui.button(label="Website Tutorial", style=discord.ButtonStyle.primary, custom_id="crystal_explain_website")
    async def website_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "website")

    @discord.ui.button(label="Bot Tutorial", style=discord.ButtonStyle.primary, custom_id="crystal_explain_bot")
    async def bot_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "bot")

    @discord.ui.button(label="Commands", style=discord.ButtonStyle.secondary, custom_id="crystal_explain_commands")
    async def commands_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "commands")

    @discord.ui.button(label="Videos", style=discord.ButtonStyle.success, custom_id="crystal_explain_videos")
    async def videos_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=build_explain_videos_embed(self.language),
            view=ExplainVideosView(self.language),
        )

    @discord.ui.button(label="العربية", emoji="🌐", style=discord.ButtonStyle.secondary, custom_id="crystal_explain_home_language")
    async def language_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.language = "ar" if self.language == "en" else "en"
        await interaction.response.edit_message(
            embed=build_explain_home_embed(self.language),
            view=ExplainHomeView(self.language),
        )


class ExplainPageView(BlacklistAwareView):
    """A bilingual explanation page with Back + language toggle."""
    def __init__(self, topic: str, language="en"):
        super().__init__(timeout=900)
        self.topic = topic
        self.language = language
        self.language_btn.label = "English" if language == "ar" else "العربية"

    @discord.ui.button(label="Back", emoji="◀", style=discord.ButtonStyle.secondary, custom_id="crystal_explain_back")
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=build_explain_home_embed(self.language),
            view=ExplainHomeView(self.language),
        )

    @discord.ui.button(label="العربية", emoji="🌐", style=discord.ButtonStyle.secondary, custom_id="crystal_explain_page_language")
    async def language_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.language = "ar" if self.language == "en" else "en"
        await interaction.response.edit_message(
            embed=build_explain_embed(self.topic, self.language),
            view=ExplainPageView(self.topic, self.language),
        )


def build_explain_home_embed(language="en"):
    if language == "ar":
        return discord.Embed(
            title="Crystal Hub | الشرح",
            description="اختر القسم الذي تريد شرحه من الأزرار بالأسفل. يمكنك تبديل اللغة من زر 🌐.",
            color=discord.Color.red(),
        )
    return discord.Embed(
        title="Crystal Hub | Explanation",
        description="Choose what you want to learn from the buttons below. You can switch the language with 🌐.",
        color=discord.Color.red(),
    )


def build_explain_videos_embed(language="en"):
    if language == "ar":
        return discord.Embed(
            title="Crystal Hub | فيديوهات الشرح",
            description="اختر الفيديو الذي تريد مشاهدته. هذه نفس فيديوهات Tutorial الموجودة في الموقع.",
            color=discord.Color.red(),
        )
    return discord.Embed(
        title="Crystal Hub | Tutorial Videos",
        description="Choose a video to watch. These are the same three videos used by the Dashboard Tutorial.",
        color=discord.Color.red(),
    )


def build_explain_embed(topic: str, language="en"):
    pages = {
        "script": {
            "en": ("Script Tutorial", "Create a script, give it a name, add your Lua source, then save it. After creation you can manage its access, file/source settings, keys and loader from the script workspace."),
            "ar": ("شرح السكربت", "أنشئ السكربت، اختر اسمه، أضف سورس Lua ثم احفظه. بعد الإنشاء تقدر تدير الوصول والملف أو السورس والمفاتيح ورابط التحميل من مساحة السكربت."),
        },
        "website": {
            "en": ("Website Tutorial", "Open the Dashboard, create or log in to your Account API, then use Scripts → Create Script. Your scripts and account data are saved to the connected backend."),
            "ar": ("شرح الموقع", "افتح الداشبورد، أنشئ أو سجّل دخولك باستخدام Account API، ثم ادخل Scripts → Create Script. يتم حفظ السكربتات وبيانات الحساب في النظام المرتبط."),
        },
        "bot": {
            "en": ("Bot Tutorial", "Invite Crystal Hub to your server, use the command menus to create and manage scripts, and use /api to link your Discord account to your Dashboard Account API."),
            "ar": ("شرح البوت", "أضف Crystal Hub إلى سيرفرك، واستخدم قوائم الأوامر لإنشاء وإدارة السكربتات، واستخدم /api لربط ديسكورد بحساب Account API في الداشبورد."),
        },
        "commands": {
            "en": ("Commands", "Main commands include /create-script, /generate-key, /hwid, /api, /trial, /source-setup, /panel-setup, /update-script, /delete-script and /avatar. Use the command's Discord help/options for its exact arguments."),
            "ar": ("الأوامر", "من أهم الأوامر: /create-script و /generate-key و /hwid و /api و /trial و /source-setup و /panel-setup و /update-script و /delete-script و /avatar. استخدم خيارات الأمر في ديسكورد لمعرفة البيانات المطلوبة."),
        },
    }
    title, description = pages.get(topic, pages["commands"])[language]
    return discord.Embed(title=f"Crystal Hub | {title}", description=description, color=discord.Color.red())


@bot.tree.command(name="explain", description="Open the Crystal Hub explanation and tutorial menu")
async def explain_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_explain_home_embed("en"),
        view=ExplainHomeView("en"),
        ephemeral=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class AdminGuideLanguageView(BlacklistAwareView):
    """Persistent bilingual guide for the two supported prefix administration commands."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="English", emoji="🇬🇧", style=discord.ButtonStyle.secondary, custom_id="crystal_admin_guide_english")
    async def english_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Crystal Hub | Admin Guide",
            description=(
                "**CHANNEL MESSAGE**\n`-massage @user Your private message` أو `-massage #channel Your message`\n"
                "Send a message to a selected text channel.\n\n"
                "**GLOBAL USER BLOCK / UNBLOCK**\n`-blacklist @user` · `-unblacklist USER_ID`\n\n"
                "**PRIVATE ANNOUNCEMENT**\n`-ann Your message`\n"
                "DM an announcement to known users.\n\n"
                "**BACKUPS**\n`-g files` exports the full Admin backup; `-files` imports it.\n"
                "`-script file` backs up scripts/source/panels; `/ai-information` saves server-specific AI instructions (Admin/Ultra + Admin role or Administrator).\n\n"
                "Standard subscription redeem codes remain single-use."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="العربية", emoji="🇸🇦", style=discord.ButtonStyle.secondary, custom_id="crystal_admin_guide_arabic")
    async def arabic_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Crystal Hub | دليل الإدارة",
            description=(
                "**إرسال رسالة لقناة**\n`-massage #channel رسالتك هنا`\n\n"
                "**حظر / فك حظر مستخدم**\n`-blacklist @user` · `-unblacklist USER_ID`\n\n"
                "**إعلان خاص**\n`-ann رسالتك هنا`\n\n"
                "**النسخ الاحتياطي**\n`-g files` لتصدير النسخة الكاملة، و`-files` لاستيرادها.\n"
                "`-script file` لنسخة السكربتات والسورس والبانلات، و`/ai-information` لحفظ معلومات الذكاء الاصطناعي.\n\n"
                "أكواد الاشتراك العادية تظل استخدامًا واحدًا."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="admin", description="Show the Crystal Hub administration guide")
async def admin_command(interaction: discord.Interaction):
    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users or _discord_plan(interaction.user.id) == "Admin"):
        await interaction.response.send_message(
            "❌ This command requires internal bot authorization.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Crystal Hub | Admin Guide",
        description=(
            "**CHANNEL MESSAGE**\n`-massage @user Your private message` أو `-massage #channel Your message`\n"
            "**GLOBAL USER BLOCK / UNBLOCK**\n`-blacklist @user` · `-unblacklist USER_ID`\n"
            "**PRIVATE ANNOUNCEMENT**\n`-ann Your message`\n"
            "**BACKUPS**\n`-g files` exports the full Admin backup; `-files` imports it. `-script file` backs up scripts; `/ai-information` saves server-specific AI instructions (Admin/Ultra + Admin role or Administrator).\n"
            "Standard subscription redeem codes remain single-use."
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(
        embed=embed, view=AdminGuideLanguageView(), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="admin-role", aliases=["set-admin-role"])
async def admin_role_prefix(ctx: commands.Context, admin_role: discord.Role):
    if not ctx.guild or not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ Only a Discord server Administrator can configure the global admin role.", mention_author=False)
        return
    GLOBAL_ADMIN_ROLE_IDS[str(ctx.guild.id)] = admin_role.id
    if not _save_global_admin_roles():
        GLOBAL_ADMIN_ROLE_IDS.pop(str(ctx.guild.id), None)
        await ctx.reply("❌ Could not save the admin role setting. Check persistent storage and logs.", mention_author=False)
        return
    await ctx.reply(
        f"✅ Server-wide admin role set to {admin_role.mention}. It can manage bot features, but does not bypass plan quotas.",
        mention_author=False,
    )


@bot.command(name="reset-hwid-cooldown")
async def reset_hwid_cooldown_prefix(ctx: commands.Context, seconds: int = -1):
    if not ctx.guild or not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ Only server administrators can change this setting.", mention_author=False)
        return
    if seconds < 0 or seconds > 31536000:
        await ctx.reply("Usage: `-reset-hwid-cooldown seconds` (0 to 31536000; 0 disables the cooldown).", mention_author=False)
        return
    security_state["reset_hwid_cooldown"] = int(seconds)
    save_security_state()
    await ctx.reply(f"✅ HWID reset cooldown set to **{format_duration(seconds)}** ({seconds} seconds).", mention_author=False)


# Removed slash registration: use the Admin prefix command -blacklist.
# @bot.tree.command(name="blacklist", description="Globally block a user from bot features")
@app_commands.describe(user="The user to blacklist")
async def blacklist_command(interaction: discord.Interaction, user: discord.Member):
    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users or _discord_plan(interaction.user.id) == "Admin"):
        await interaction.response.send_message("❌ This command requires internal bot authorization.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You cannot blacklist yourself.", ephemeral=True)
        return
    blacklisted_users.add(int(user.id))
    if not save_data():
        blacklisted_users.discard(int(user.id))
        await interaction.response.send_message("❌ Could not save the blacklist. The change was cancelled; check the bot's file permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"🚫 {user.mention} has been added to the global bot blacklist.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# Removed slash registration: use the Admin prefix command -unblacklist.
# @bot.tree.command(name="unblacklist", description="Remove a user from the global bot blacklist")
@app_commands.describe(user_id="Discord user ID to unblock")
async def unblacklist_command(interaction: discord.Interaction, user_id: str):
    if not (is_bot_owner(interaction.user.id) or str(interaction.user.id) in admin_redeemed_users or _discord_plan(interaction.user.id) == "Admin"):
        await interaction.response.send_message("❌ This command requires internal bot authorization.", ephemeral=True)
        return
    try:
        target_id = int(user_id.strip())
    except (TypeError, ValueError):
        await interaction.response.send_message("❌ Enter a valid numeric Discord user ID.", ephemeral=True)
        return
    was_blacklisted = target_id in blacklisted_users
    blacklisted_users.discard(target_id)
    if not save_data():
        if was_blacklisted:
            blacklisted_users.add(target_id)
        await interaction.response.send_message("❌ Could not save the blacklist change. It was rolled back; check the bot's file permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ User `{target_id}` removed from the global blacklist.", ephemeral=True)


# Removed slash registration: use the Admin prefix command -list-servers.
# @bot.tree.command(name="list-servers", description="List servers the bot has joined")
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


# Removed slash registration: use the Admin prefix command -send-server-message.
# @bot.tree.command(name="send-server-message", description="Send a message to a channel in a selected server")
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

@bot.command(name="list-redeem-codes")
async def list_redeem_codes_prefix(ctx: commands.Context):
    """Admin-only list of website redeem code status; no plaintext codes are exposed."""
    if not (is_bot_owner(ctx.author.id) or str(ctx.author.id) in admin_redeemed_users or _discord_plan(ctx.author.id) == "Admin"):
        await ctx.reply("❌ This command requires internal Admin authorization.", mention_author=False)
        return
    if not site_redeem_codes:
        await ctx.reply("No website redeem codes have been created yet.", mention_author=False)
        return
    lines = []
    for i, rec in enumerate(site_redeem_codes.values(), 1):
        status = "revoked" if rec.get("revoked") else ("used" if rec.get("redeemed_by") else "available")
        lines.append(f"{i}. **{rec.get('plan','Premium')}** — {status} — created {str(rec.get('created_at',''))[:10]}")
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
        await ctx.reply("❌ This command requires internal bot authorization.", mention_author=False)
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
        await ctx.reply("❌ Could not save the blacklist change; the change was rolled back.", mention_author=False)
        return
    await ctx.reply(f"✅ User `{target_id}` removed from the global blacklist.", mention_author=False)


@bot.command(name="list-servers")
async def list_servers_prefix(ctx: commands.Context):
    if not is_bot_owner(ctx.author.id):
        await ctx.reply("❌ This action is restricted to the configured bot owner.", mention_author=False)
        return
    guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
    if not guilds:
        await ctx.reply("The bot is not in any servers.", mention_author=False)
        return
    lines = [f"• **{discord.utils.escape_markdown(g.name)}** — ID: `{g.id}` — members: `{g.member_count or 'unknown'}`" for g in guilds]
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
        await ctx.reply("❌ This action is restricted to the configured bot owner.", mention_author=False)
        return
    if not server_id.isdigit() or not channel_id.isdigit() or not message.strip() or len(message) > 1800:
        await ctx.reply("Usage: `-send-server-message SERVER_ID CHANNEL_ID message`", mention_author=False)
        return
    guild = bot.get_guild(int(server_id))
    if guild is None:
        await ctx.reply("❌ The bot is not in that server.", mention_author=False)
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None or not hasattr(channel, "send"):
        await ctx.reply("❌ Could not find a sendable channel in that server.", mention_author=False)
        return
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        await ctx.reply("❌ The bot could not deliver the message to that channel.", mention_author=False)
        return
    await ctx.reply(f"✅ Message sent to **{guild.name}** / {channel.mention}.", mention_author=False)


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
            await message.reply("🚫 You are globally blacklisted from using this bot.", mention_author=False)
        # Do not let globally blacklisted users use AI chat or other message-based features.
        return

    if content.lower().startswith("-ann") and (len(content) == 4 or content[4].isspace()):
        if not (is_bot_owner(message.author.id) or str(message.author.id) in admin_redeemed_users or _discord_plan(message.author.id) == "Admin"):
            await message.reply("❌ This command is restricted to Admin users.", mention_author=False)
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
                owned_scripts = [name for name, info in scripts_data.items() if str(info.get("created_by", "")) == str(uid)]
                status_line = ("You have created a script on Crystal Hub: " + ", ".join(owned_scripts[:10])) if owned_scripts else "You have not created a script on Crystal Hub yet."
                await user.send(announcement + "\n\n" + status_line, allowed_mentions=discord.AllowedMentions.none())
                sent += 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += 1
        await message.reply(
            f"📨 Announcement finished. Sent: **{sent}** · Failed: **{failed}** · Tracked: **{len(KNOWN_USERS)}**.",
            mention_author=False,
        )
        return

    if content.lower().startswith("-massage"):
        # Can be used in a server or in a direct message to the bot. User DMs
        # work by mention or Discord user ID even if the target shares no server.
        if not (is_bot_owner(message.author.id) or str(message.author.id) in admin_redeemed_users or _discord_plan(message.author.id) == "Admin"):
            await message.reply("❌ This command requires internal bot authorization.", mention_author=False)
            return
        parts = content.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("Usage: `-massage @user Your private message` or `-massage @user Your private message` أو `-massage #channel Your message`", mention_author=False)
            return
        target_token, body = parts[1], parts[2]
        target_user = None
        raw_user_id = target_token.strip().strip("<@!>")
        if message.mentions:
            target_user = message.mentions[0]
        elif raw_user_id.isdigit():
            try:
                target_user = bot.get_user(int(raw_user_id)) or await bot.fetch_user(int(raw_user_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target_user = None
        else:
            target_user = next((u for u in bot.users if u.name.lower() == target_token.lower()), None)
        if target_user is not None:
            try:
                await target_user.send(body, allowed_mentions=discord.AllowedMentions.none())
                await message.reply(f"✅ Private message sent to {target_user.mention}.", mention_author=False)
            except discord.HTTPException as exc:
                await message.reply(f"❌ Could not DM that user: {type(exc).__name__}. They may have DMs disabled.", mention_author=False)
            return
        if message.guild is None:
            await message.reply("❌ User not found. In DMs, use `-massage USER_ID Your message` or a user mention. The target does not need to share a server with the bot.", mention_author=False)
            return
        target = _parse_massage_target(message.guild, target_token)
        if target is None or not isinstance(target, (discord.TextChannel, discord.Thread)):
            await message.reply("❌ User/channel not found. Mention a user or use their Discord ID for a DM, or specify a channel for public messages.", mention_author=False)
            return
        perms = target.permissions_for(message.guild.me) if message.guild.me else None
        can_send = bool(perms and (perms.send_messages_in_threads if isinstance(target, discord.Thread) else perms.send_messages))
        if not can_send:
            await message.reply("❌ I do not have permission to send messages in that channel/thread.", mention_author=False)
            return
        try:
            await target.send(body, allowed_mentions=discord.AllowedMentions.none())
            await message.reply(f"✅ Message sent to {target.mention}.", mention_author=False)
        except discord.HTTPException as exc:
            await message.reply(f"❌ Could not send the message: {type(exc).__name__}.", mention_author=False)
        return

    if content.lower().startswith("-blacklist"):
        if not message.guild:
            await message.reply("❌ Use this command inside a server.", mention_author=False)
            return
        if not (is_bot_owner(message.author.id) or str(message.author.id) in admin_redeemed_users or _discord_plan(message.author.id) == "Admin"):
            await message.reply("❌ This command requires internal bot authorization.", mention_author=False)
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
            await message.reply("❌ Could not identify the user. Mention them or provide a numeric ID.", mention_author=False)
            return
        if target_id == message.author.id:
            await message.reply("❌ You cannot blacklist yourself.", mention_author=False)
            return
        blacklisted_users.add(int(target_id))
        if not save_data():
            blacklisted_users.discard(int(target_id))
            await message.reply("❌ Could not save the blacklist. The change was cancelled; check the bot's file permissions.", mention_author=False)
            return
        await message.reply(f"🚫 User `{target_id}` has been globally blacklisted.", mention_author=False)
        return

    try:
        if message.guild is not None and message.channel.id in AI_CHAT_CHANNELS:
            content = (message.content or "").strip()
            if content and not content.startswith(("/", "-", "!")):
                now = asyncio.get_running_loop().time()
                last = _ai_last_reply_by_user.get(message.author.id, 0)
                if now - last >= 2:
                    _ai_last_reply_by_user[message.author.id] = now
                    history = _AI_CHAT_HISTORY.setdefault(message.channel.id, [])
                    answer = await _crystal_ai_answer(content, history=history, guild_id=message.guild.id)
                    history.append({"role": "user", "content": content[:1000]})
                    if answer:
                        history.append({"role": "assistant", "content": answer[:1500]})
                        del history[:-12]
                        await message.reply(answer[:1900], mention_author=False)
                    else:
                        await message.reply("ما قدرت أجيب رد من الـAI حاليًا. تأكد من إعداد مفاتيح مزوّد الـAI في Render.", mention_author=False)
    except Exception as exc:
        print(f"AI message handler failed: {type(exc).__name__}: {exc}")

    await bot.process_commands(message)


TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN environment variable is missing!")  
