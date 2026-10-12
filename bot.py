import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# متغير لحفظ رقم الإصدار الحالي
current_version = 1.1

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"تم مزامنة {len(synced)} أمر slash بنجاح.")
    except Exception as e:
        print(f"حدث خطأ أثناء المزامنة: {e}")
    print(f'تم تشغيل البوت بنجاح باسم: {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send('Pong! 🏓')

# أمر التحديثات الجديد
@bot.tree.command(name="update", description="إرسال تحديث جديد للأعضاء")
@app_commands.describe(details="اكتب هنا تفاصيل التحديث")
async def update_command(interaction: discord.Interaction, details: str):
    global current_version
    
    # تنسيق رقم الإصدار
    version_text = f"v{current_version:.1f}"
    
    # زيادة رقم الإصدار تلقائياً للمرة القادمة
    current_version += 0.1
    
    # تنسيق رسالة التحديث بالشكل المطلوب
    message_content = (
        "||@everyone|| ||@here||\n\n"
        f"update {version_text}\n"
        f"```\n{details}\n```"
    )
    
    await interaction.response.send_message(message_content)

# تشغيل البوت بالتوكن الخاص بك
bot.run('MTU1OTI3NTU4MDM1OTMxNTUzNw.G3VwVn.u9cboSRl4F0WoiAyPN6aGHukl5W7Ezz05v5fZI')
