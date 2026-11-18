import discord
from discord.ext import commands, tasks
import json
import time
import os
import asyncio

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
IGNORED_CHANNELS = [int(x.strip()) for x in os.getenv("IGNORED_CHANNELS", "").split(",") if x.strip().isdigit()]
MIN_MEMBERS = int(os.getenv("MIN_MEMBERS", "1"))

# Сохраняем в папку data/, чтобы файл не удалялся при пересборке контейнера на BotHost
DATA_DIR = "data" if os.path.isdir("data") else "."
DATA_FILE = os.path.join(DATA_DIR, "voice_stats.json")

# Стартовое время по умолчанию: 17075 сек (4h 44m 35s)
DEFAULT_TOTAL_SECONDS = float(os.getenv("DEFAULT_TOTAL_SECONDS", "17075"))
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "0")) or None

print("1. Скрипт запустился, подключаюсь к Discord...")

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

active_channels = {}


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("total_seconds", DEFAULT_TOTAL_SECONDS), data.get("channel_id", DEFAULT_CHANNEL_ID)
        except Exception as e:
            print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось прочитать {DATA_FILE}: {e}")
    return DEFAULT_TOTAL_SECONDS, DEFAULT_CHANNEL_ID


def save_data(seconds, channel_id):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"total_seconds": seconds, "channel_id": channel_id}, f, indent=4)


total_seconds, stats_channel_id = load_data()
if not os.path.exists(DATA_FILE) and (total_seconds > 0 or stats_channel_id):
    save_data(total_seconds, stats_channel_id)


def format_time(seconds):
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return hours, minutes, secs


def get_live_total_seconds():
    now = time.time()
    live_extra = sum(now - start_time for start_time in active_channels.values())
    return total_seconds + live_extra


def is_valid_voice(channel):
    if channel is None or channel.id in IGNORED_CHANNELS:
        return False
    return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))


def sync_channel_tracking(channel, now):
    global total_seconds
    if not is_valid_voice(channel):
        return

    human_members = [m for m in channel.members if not m.bot]
    if len(human_members) >= MIN_MEMBERS:
        if channel.id not in active_channels:
            active_channels[channel.id] = now
            print(f"[СТАРТ] Канал '{channel.name}' активен ({len(human_members)} чел.). Время пошло.")
    else:
        if channel.id in active_channels:
            total_seconds += now - active_channels.pop(channel.id)
            save_data(total_seconds, stats_channel_id)
            h, m, s = format_time(total_seconds)
            print(f"[ПАУЗА] Канал '{channel.name}' опустел (< {MIN_MEMBERS} чел.). Сохранено: {h}ч {m}м {s}с")


@bot.event
async def on_ready():
    print(f"2. [OK] Бот успешно подключился как {bot.user}!")
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print(f"[ОШИБКА] Сервер с ID {GUILD_ID} не найден! Проверь GUILD_ID.")
        return

    now = time.time()
    for vc in list(guild.voice_channels) + list(guild.stage_channels):
        sync_channel_tracking(vc, now)

    if not auto_save_loop.is_running():
        auto_save_loop.start()
    if not update_channel_name.is_running():
        update_channel_name.start()


@bot.command()
@commands.has_permissions(administrator=True)
async def create_stats(ctx):
    global stats_channel_id
    h, m, s = format_time(get_live_total_seconds())

    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        ctx.guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
    }

    try:
        channel = await ctx.guild.create_voice_channel(
            name=f"Total Voice: {h}h {m}m {s}s",
            overwrites=overwrites
        )
        stats_channel_id = channel.id
        save_data(total_seconds, stats_channel_id)
        print(f"[КАНАЛ СОЗДАН] {channel.name} (ID: {channel.id})")
        await ctx.send(
            f"✅ Создан канал-счетчик: **{channel.name}**.\n"
            f"Можешь перетащить его в нужную категорию (главное — не нажимай «Синхронизировать с категорией»)."
        )
    except discord.Forbidden:
        await ctx.send("❌ У бота нет права «Управлять каналами» на сервере!")


@bot.command()
async def voice(ctx):
    live_total = get_live_total_seconds()
    h, m, s = format_time(live_total)
    await ctx.send(
        f"🎙 **Общая статистика голосовых каналов:**\n"
        f"⏱ **{h}** ч. **{m}** мин. **{s}** сек.\n"
        f"*(Активных комнат сейчас: {len(active_channels)})*"
    )


@bot.command()
async def live(ctx):
    msg = await ctx.send("⏳ Запуск живого счетчика...")
    for _ in range(60):
        h, m, s = format_time(get_live_total_seconds())
        try:
            await msg.edit(content=f"🎙 **В войсе сейчас (Live):** `{h}ч {m}м {s}с`")
        except discord.HTTPException:
            break
        await asyncio.sleep(1)


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    now = time.time()
    if before.channel is not None:
        sync_channel_tracking(before.channel, now)
    if after.channel is not None and after.channel != before.channel:
        sync_channel_tracking(after.channel, now)


@tasks.loop(minutes=1)
async def auto_save_loop():
    global total_seconds
    if not active_channels:
        return

    now = time.time()
    added = 0
    for ch_id, start_time in list(active_channels.items()):
        added += (now - start_time)
        active_channels[ch_id] = now

    if added > 0:
        total_seconds += added
        save_data(total_seconds, stats_channel_id)
        h, m, s = format_time(total_seconds)
        print(f"[АВТОСОХРАНЕНИЕ] {h}ч {m}м {s}с (активных комнат: {len(active_channels)})")


@tasks.loop(minutes=10)
async def update_channel_name():
    if not stats_channel_id:
        print("[ИНФО] Канал-счетчик еще не создан. Напиши в чат сервера !create_stats")
        return

    channel = bot.get_channel(stats_channel_id)
    if not channel:
        print(f"[ОШИБКА] Канал с ID {stats_channel_id} не найден! Напиши !create_stats заново.")
        return

    h, m, s = format_time(get_live_total_seconds())
    new_name = f"Total Voice: {h}h {m}m {s}s"
    if channel.name != new_name:
        try:
            await channel.edit(name=new_name)
            print(f"[КАНАЛ ОБНОВЛЕН] -> {new_name}")
        except discord.HTTPException as e:
            print(f"[ОШИБКА ОБНОВЛЕНИЯ] {e}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Не задан DISCORD_TOKEN в переменных окружения!")
    bot.run(TOKEN)
