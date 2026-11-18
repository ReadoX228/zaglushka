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

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "voice_stats.json")

DEFAULT_TOTAL_SECONDS = float(os.getenv("DEFAULT_TOTAL_SECONDS", "17075"))
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "0")) or None

print("1. Скрипт запустился, подключаюсь к Discord...")

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    heartbeat_timeout=150.0,
    help_command=None
)

# Активные комнаты: {channel_id: timestamp_старта}
active_channels = {}
# Активные пользователи в войсе: {user_id_str: timestamp_старта}
active_users = {}


def load_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return (
                    data.get("total_seconds", DEFAULT_TOTAL_SECONDS),
                    data.get("channel_id", DEFAULT_CHANNEL_ID),
                    data.get("users", {})
                )
        except Exception as e:
            print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось прочитать {DATA_FILE}: {e}")
    return DEFAULT_TOTAL_SECONDS, DEFAULT_CHANNEL_ID, {}


def save_data(seconds, channel_id, users_dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total_seconds": seconds,
                "channel_id": channel_id,
                "users": users_dict
            },
            f,
            indent=4
        )


total_seconds, stats_channel_id, user_seconds = load_data()
if not os.path.exists(DATA_FILE):
    save_data(total_seconds, stats_channel_id, user_seconds)


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


def get_live_user_seconds():
    """Возвращает словарь {user_id: общее_время} с учётом тех, кто сейчас в войсе"""
    now = time.time()
    live_users = dict(user_seconds)
    for uid, start_time in active_users.items():
        live_users[uid] = live_users.get(uid, 0.0) + (now - start_time)
    return live_users


def is_valid_voice(channel):
    if channel is None or channel.id in IGNORED_CHANNELS:
        return False
    return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))


def sync_channel_tracking(channel, now):
    global total_seconds
    if not is_valid_voice(channel):
        return

    human_members = [m for m in channel.members if not m.bot]
    current_human_ids = {str(m.id) for m in human_members}

    if len(human_members) >= MIN_MEMBERS:
        if channel.id not in active_channels:
            active_channels[channel.id] = now
            print(f"[СТАРТ] Канал '{channel.name}' активен ({len(human_members)} чел.). Время пошло.")

        # Запускаем таймер каждому участнику в этой комнате
        for uid in current_human_ids:
            if uid not in active_users:
                active_users[uid] = now
    else:
        changed = False
        if channel.id in active_channels:
            total_seconds += now - active_channels.pop(channel.id)
            changed = True

        # Ставим на паузу таймеры участников, оставшихся в этой комнате
        for uid in current_human_ids:
            if uid in active_users:
                user_seconds[uid] = user_seconds.get(uid, 0.0) + (now - active_users.pop(uid))
                changed = True

        if changed:
            save_data(total_seconds, stats_channel_id, user_seconds)
            h, m, s = format_time(total_seconds)
            print(f"[ПАУЗА] Канал '{channel.name}' опустел (< {MIN_MEMBERS} чел.). Сохранено: {h}ч {m}м {s}с")


@tasks.loop(minutes=5)
async def keep_presence():
    if bot.is_ready():
        await bot.change_presence(status=discord.Status.online)


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
    if not keep_presence.is_running():
        keep_presence.start()


@bot.command(name="help")
async def help_command(ctx):
    """Выводит список всех доступных команд бота"""
    embed = discord.Embed(
        title="📋 Команды бота Voice Stats",
        description="Отслеживание времени в голосовых каналах сервера:",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="`!voice`",
        value="Показывает общее накопленное время всех участников до секунды.",
        inline=False
    )
    embed.add_field(
        name="`!top`",
        value="Выводит топ-10 самых активных участников в голосовых каналах.",
        inline=False
    )
    embed.add_field(
        name="`!live`",
        value="Запускает живой таймер в чате на 60 секунд с обновлением раз в секунду.",
        inline=False
    )
    embed.add_field(
        name="`!create_stats` *(только для админов)*",
        value="Создаёт закрытый голосовой канал-счётчик с замочком.",
        inline=False
    )
    embed.add_field(
        name="`!help`",
        value="Показывает это справочное сообщение.",
        inline=False
    )
    await ctx.send(embed=embed)


@bot.command()
async def top(ctx):
    """Выводит топ-10 участников по времени в голосовых каналах"""
    live_users = get_live_user_seconds()
    # Отфильтровываем нулевые значения и сортируем по убыванию
    sorted_users = sorted(
        [(uid, secs) for uid, secs in live_users.items() if secs >= 1],
        key=lambda item: item[1],
        reverse=True
    )

    if not sorted_users:
        await ctx.send("📉 Пока никто не успел накопить время в голосовых каналах.")
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for idx, (uid, secs) in enumerate(sorted_users[:10], start=1):
        h, m, s = format_time(secs)
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"<@{uid}>"
        prefix = medals.get(idx, f"**{idx}.**")
        lines.append(f"{prefix} **{name}** — `{h}ч {m}м {s}с`")

    embed = discord.Embed(
        title="🏆 Топ участников по времени в войсе",
        description="\n".join(lines),
        color=discord.Color.gold()
    )

    # Показываем место автора команды, если он есть в статистике
    author_id = str(ctx.author.id)
    for idx, (uid, secs) in enumerate(sorted_users, start=1):
        if uid == author_id:
            ah, am, as_ = format_time(secs)
            embed.set_footer(text=f"Твоё место: #{idx} ({ah}ч {am}м {as_}с)")
            break

    await ctx.send(embed=embed)


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
        save_data(total_seconds, stats_channel_id, user_seconds)
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
    uid = str(member.id)

    # Если пользователь полностью вышел из голосовых каналов — фиксируем его сессию
    if after.channel is None and uid in active_users:
        user_seconds[uid] = user_seconds.get(uid, 0.0) + (now - active_users.pop(uid))
        save_data(total_seconds, stats_channel_id, user_seconds)

    if before.channel is not None:
        sync_channel_tracking(before.channel, now)
    if after.channel is not None and after.channel != before.channel:
        sync_channel_tracking(after.channel, now)


@tasks.loop(minutes=1)
async def auto_save_loop():
    global total_seconds
    if not active_channels and not active_users:
        return

    now = time.time()
    added_total = 0.0
    for ch_id, start_time in list(active_channels.items()):
        added_total += (now - start_time)
        active_channels[ch_id] = now

    for uid, start_time in list(active_users.items()):
        user_seconds[uid] = user_seconds.get(uid, 0.0) + (now - start_time)
        active_users[uid] = now

    if added_total > 0 or active_users:
        total_seconds += added_total
        save_data(total_seconds, stats_channel_id, user_seconds)
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
    bot.run(TOKEN, reconnect=True)
