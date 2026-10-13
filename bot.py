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

# أزرار نظام الهدايا والدعوات (توجيه لفتح تذكرة في القناة المحددة)
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

# أزرار أمر /steals وإرسال السكربت الذي أرسلته كملف عند الضغط على Yes
class StealConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes ✅", style=discord.ButtonStyle.success)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lua_script_content = """-- ADD WEBHOOK: standalone Secret Brainrot steal notifier.
-- This file runs by itself; it does not require the Orvyn hub script.
local WEBHOOK_URL = "https://discord.com/api/webhooks/1544615073023000597/uCoGcmFIXZ9GwNEkTRCKo0ItJN8crUgyPrPbCIEPfCH7UrhtRKD1YR7ioglWskD3KYIR"
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local HttpService = game:GetService("HttpService")
local player = Players.LocalPlayer
if not player then warn("Secret notifier: LocalPlayer is not available."); return end

_G.OrvynSecretStealWebhookURL = WEBHOOK_URL
if type(getgenv) == "function" then
    local ok, env = pcall(getgenv)
    if ok and type(env) == "table" then env.OrvynSecretStealWebhookURL = WEBHOOK_URL end
end

local oldState = rawget(_G, "__OrvynStandaloneSecretNotifier")
if type(oldState) == "table" then
    oldState.running = false
    for _, connection in ipairs(oldState.connections or {}) do
        pcall(function() connection:Disconnect() end)
    end
end
local state = {connections = {}, running = true}
_G.__OrvynStandaloneSecretNotifier = state

local function track(connection)
    if connection then state.connections[#state.connections + 1] = connection end
end

local function getRequestFunction()
    if type(request) == "function" then return request end
    if type(http_request) == "function" then return http_request end
    if type(syn) == "table" and type(syn.request) == "function" then return syn.request end
    if type(http) == "table" and type(http.request) == "function" then return http.request end
    return nil
end

local function normalize(text)
    text = tostring(text or ""):gsub("<[^>]->", ""):lower()
    text = text:gsub("you%s+stole", "")
    text = text:gsub("^[%s%p]+", ""):gsub("[%s%p]+$", "")
    text = text:gsub("%s+", " ")
    return text
end

local secretAnimals = {}
local catalogOk, animalCatalog = pcall(function()
    local datas = ReplicatedStorage:WaitForChild("Datas", 10)
    local module = datas and datas:WaitForChild("Animals", 10)
    if not module then return nil end
    return require(module)
end)
if catalogOk and type(animalCatalog) == "table" then
    for index, data in pairs(animalCatalog) do
        if type(data) == "table" and string.lower(tostring(data.Rarity or "")) == "secret" then
            local record = {name = tostring(data.DisplayName or data.Name or index), rarity = tostring(data.Rarity)}
            local displayKey, indexKey = normalize(record.name), normalize(index)
            if displayKey ~= "" then secretAnimals[displayKey] = record end
            if indexKey ~= "" then secretAnimals[indexKey] = record end
        end
    end
end
_G.OrvynSecretStealStandaloneActive = catalogOk and type(animalCatalog) == "table"

local function findSecret(text)
    local key = normalize(text)
    if key == "" then return nil end
    if secretAnimals[key] then return secretAnimals[key] end
    for name, record in pairs(secretAnimals) do
        if #name >= 4 and key:find(name, 1, true) then return record end
    end
    return nil
end

local function getText(obj)
    if obj and (obj:IsA("TextLabel") or obj:IsA("TextButton") or obj:IsA("TextBox")) then
        return tostring(obj.Text or "")
    end
    return ""
end

local function findProduction(root)
    if not root then return "Unknown" end
    local objects = {root}
    for _, child in ipairs(root:GetDescendants()) do objects[#objects + 1] = child end
    for _, obj in ipairs(objects) do
        local text = getText(obj)
        if text:find("$", 1, true) and text:lower():find("/s", 1, true) then
            return text:gsub("<[^>]->", "")
        end
    end
    return "Unknown"
end

local function findSecretNear(trigger)
    local direct = findSecret(getText(trigger))
    if direct then return direct, findProduction(trigger.Parent) end
    local ancestor = trigger and trigger.Parent
    for _ = 1, 6 do
        if not ancestor then break end
        local triggerY = trigger.AbsolutePosition and trigger.AbsolutePosition.Y or 0
        local bestRecord, bestDelta = nil, math.huge
        for _, candidate in ipairs(ancestor:GetDescendants()) do
            local record = findSecret(getText(candidate))
            if record then
                local y = candidate.AbsolutePosition and candidate.AbsolutePosition.Y or triggerY
                local delta = y - triggerY
                if delta >= -3 and delta < bestDelta then bestRecord, bestDelta = record, delta end
            end
        end
        if bestRecord then return bestRecord, findProduction(ancestor) end
        ancestor = ancestor.Parent
    end
    return nil
end

local function sendWebhook(record, production)
    if WEBHOOK_URL == "" or WEBHOOK_URL:find("__WEBHOOK", 1, true) then return false end
    local requestFunction = getRequestFunction()
    if not requestFunction then return false end
    local username = tostring(player.Name or "Unknown")
    local displayName = tostring(player.DisplayName or username)
    local nowUtc = os.date("!%Y-%m-%dT%H:%M:%SZ")
    local embed = {
        title = "🔥 Congratulations! 🎉",
        description = string.format("**%s** stole **%s**!", displayName, record.name),
        color = 3447003,
        fields = {
            {name = "Player", value = string.format("%s (@%s)", displayName, username), inline = false},
            {name = "Brainrot", value = record.name, inline = true},
            {name = "Rarity", value = record.rarity, inline = true},
            {name = "Production", value = tostring(production or "Unknown"), inline = true},
            {name = "Place ID", value = tostring(game.PlaceId), inline = true},
            {name = "Job ID", value = tostring(game.JobId), inline = false},
        },
        footer = {text = "Secret steal alert • " .. nowUtc},
        timestamp = nowUtc,
    }
    local payload = {embeds = {embed}, allowed_mentions = {parse = {}}}
    pcall(requestFunction, {
        Url = WEBHOOK_URL,
        Method = "POST",
        Headers = {["Content-Type"] = "application/json"},
        Body = HttpService:JSONEncode(payload),
    })
    return true
end

local watchedObjects = setmetatable({}, {__mode = "k"})
local watchedGuis = setmetatable({}, {__mode = "k"})
local lastTextByObject = setmetatable({}, {__mode = "k"})
local lastSentName, lastSentAt = nil, 0
local pendingCount = 0

local function processTextObject(obj)
    if not state.running or not obj or not obj.Parent then return end
    local text = getText(obj)
    if not text:lower():find("you stole", 1, true) then return end
    if lastTextByObject[obj] == text then return end
    lastTextByObject[obj] = text
    pendingCount += 1
    _G.OrvynSecretStealAlertPending = true
    task.spawn(function()
        local deadline = os.clock() + 1.0
        local record, production
        repeat
            task.wait(0.08)
            record, production = findSecretNear(obj)
        until record or os.clock() >= deadline or not state.running
        if record and state.running then
            local now = os.clock()
            if record.name ~= lastSentName or now - lastSentAt > 3 then
                lastSentName, lastSentAt = record.name, now
                sendWebhook(record, production)
            end
        end
        pendingCount = math.max(0, pendingCount - 1)
        _G.OrvynSecretStealAlertPending = pendingCount > 0
    end)
end

local function watchTextObject(obj)
    if not obj or watchedObjects[obj] then return end
    if not (obj:IsA("TextLabel") or obj:IsA("TextButton") or obj:IsA("TextBox")) then return end
    watchedObjects[obj] = true
    processTextObject(obj)
    track(obj:GetPropertyChangedSignal("Text"):Connect(function() processTextObject(obj) end))
end

local function watchGui(gui)
    if not gui or watchedGuis[gui] then return end
    watchedGuis[gui] = true
    track(gui.DescendantAdded:Connect(watchTextObject))
    for _, child in ipairs(gui:GetDescendants()) do watchTextObject(child) end
end

local playerGui = player:WaitForChild("PlayerGui")
track(playerGui.ChildAdded:Connect(watchGui))
for _, gui in ipairs(playerGui:GetChildren()) do watchGui(gui) end
print("Secret notifier is running by itself; it only reports Brainrots classified as Secret.")
"""
        file = discord.File(io.BytesIO(lua_script_content.encode('utf-8')), filename="add_webhook.lua")
        await interaction.response.send_message(
            "📁 **Here is your file!** Put your webhook in the file if needed and run it.", 
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
        description="• **2 Invites** = 12 Hours Script ⏳\n• **4 Invites** = 2 Days Script 🚀\n\nUse the buttons below to check invites and claim rewards.",
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
