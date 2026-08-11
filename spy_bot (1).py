"""
Discord-бот для игры "Шпион" (Spyfall).

Как играть:
1. Один игрок пишет !spy_create — создаётся лобби в текущем канале.
2. Остальные пишут !spy_join, чтобы присоединиться (нужно минимум 3 игрока).
3. Создатель лобби (или любой участник) пишет !spy_start, чтобы начать раунд.
   - Если админ сервера задал свой пул слов (см. ниже), бот выбирает случайное
     слово из этого пула: все игроки получают в ЛС это слово, а один случайный
     игрок — пометку "ШПИОН".
   - Если пул слов пуст, используется встроенный режим с локациями и ролями.
4. Игроки обсуждают и задают друг другу вопросы, чтобы вычислить шпиона.
5. Любой участник может завершить раунд командой !spy_end — бот объявит,
   кто был шпионом и какое было слово/локация. Лобби и список игроков при
   этом НЕ распускаются — сразу можно снова написать !spy_start и начать
   новый раунд с теми же игроками, без повторного сбора через !spy_join.
6. Когда лобби реально больше не нужно (все расходятся), кто-то пишет
   !spy_close, чтобы полностью распустить его.

Команды для игроков:
!spy_create          — создать новое лобби в этом канале
!spy_join            — присоединиться к лобби
!spy_leave           — выйти из лобби
!spy_players         — показать список игроков
!spy_locations       — показать список встроенных локаций
!spy_start [минуты]  — начать (или перезапустить) раунд в этом лобби
                        (по умолчанию — максимум, 180 минут; можно указать своё число)
!spy_end             — завершить текущий раунд и раскрыть роли,
                        лобби остаётся открытым для следующего !spy_start
!spy_close           — полностью распустить лобби (очистить список игроков)
!spy_help            — показать это сообщение

Админ-команды для управления пулом слов (нужны права "Управление сервером"):
!spy_words add <слово>          — добавить одно слово в пул
!spy_words addmany сл1, сл2, ...— добавить несколько слов через запятую
!spy_words remove <слово>       — убрать слово из пула
!spy_words list                 — показать текущий пул слов
!spy_words clear                — очистить пул (вернуться к встроенным локациям)
"""

import asyncio
import random
from pathlib import Path

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS, help_command=None)

# Картинка, которая будет прикрепляться к каждому ЛС с ролью/словом.
# Положи файл с этим именем рядом со скриптом spy_bot.py (или поменяй путь ниже).
ROLE_IMAGE_PATH = Path(__file__).parent / "spy_image.png"

LOCATIONS = {
    "Пляж": ["Спасатель", "Отдыхающий", "Продавец мороженого", "Серфер", "Фотограф"],
    "Казино": ["Крупье", "Игрок", "Охранник", "Бармен", "Менеджер"],
    "Больница": ["Хирург", "Медсестра", "Пациент", "Санитар", "Анестезиолог"],
    "Школа": ["Учитель", "Ученик", "Директор", "Уборщик", "Охранник"],
    "Самолёт": ["Пилот", "Стюардесса", "Пассажир", "Механик", "Диспетчер"],
    "Ресторан": ["Шеф-повар", "Официант", "Посетитель", "Администратор", "Сомелье"],
    "Космическая станция": ["Командир", "Инженер", "Учёный", "Врач", "Пилот шаттла"],
    "Цирк": ["Клоун", "Дрессировщик", "Акробат", "Зритель", "Директор цирка"],
    "Банк": ["Кассир", "Охранник", "Клиент", "Управляющий", "Инкассатор"],
    "Полицейский участок": ["Детектив", "Патрульный", "Задержанный", "Криминалист", "Дежурный"],
}

MIN_PLAYERS = 3
MAX_ROUND_MINUTES = 180  # 3 часа

# Пул слов, заданный админами, отдельно для каждого сервера (guild_id -> список слов).
# Если для сервера пул непустой, игра использует режим "слово / шпион" вместо
# встроенных локаций.
custom_word_pools: dict[int, list[str]] = {}

# ---------------------------------------------------------------------------
# Состояние игры (на канал)
# ---------------------------------------------------------------------------


class SpyGame:
    def __init__(self, channel_id: int, host: discord.Member):
        self.channel_id = channel_id
        self.host = host
        self.players: list[discord.Member] = [host]
        self.started = False
        self.location: str | None = None  # используется в режиме локаций
        self.word: str | None = None       # используется в режиме "слово / шпион"
        self.spy: discord.Member | None = None
        self.timer_task: asyncio.Task | None = None

    @property
    def word_mode(self) -> bool:
        return self.word is not None

    def add_player(self, member: discord.Member) -> bool:
        if member in self.players:
            return False
        self.players.append(member)
        return True

    def remove_player(self, member: discord.Member) -> bool:
        if member in self.players:
            self.players.remove(member)
            return True
        return False


games: dict[int, SpyGame] = {}  # channel_id -> SpyGame


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def get_game(channel_id: int) -> SpyGame | None:
    return games.get(channel_id)


def get_role_image() -> discord.File | None:
    """Возвращает свежий объект discord.File с картинкой для ЛС, либо None,
    если файл не найден (например, забыли его загрузить на сервер)."""
    if not ROLE_IMAGE_PATH.exists():
        return None
    # Каждый member.send() требует свой собственный объект File — старый
    # после отправки становится непригодным для повторного использования.
    return discord.File(ROLE_IMAGE_PATH, filename=ROLE_IMAGE_PATH.name)


async def send_role_dm(member: discord.Member, game: SpyGame):
    try:
        if game.word_mode:
            # Режим "слово / шпион": у всех одно и то же слово, у шпиона его нет.
            if member == game.spy:
                await member.send(
                    "🕵️ **Ты — ШПИОН! , а ера хуесос**\n"
                    #"У тебя нет загаданного слова. Слушай, о чём говорят "
                    #"остальные, старайся понять слово по их подсказкам "
                    #"и не спалиться сам.",
                    file=get_role_image(),
                )
            else:
                await member.send(
                    f"🔑 **Твоё слово:** {game.word}\n\n"
                    ", а ера хуесос "
                    #"Среди игроков прячется шпион, который не знает слово. "
                    #"Давайте подсказки и наводящие вопросы, чтобы вычислить "
                    #"его, но не называйте слово напрямую!",
                    file=get_role_image(),
                )
        else:
            if member == game.spy:
                await member.send(
                    "🕵️ **Ты — ШПИОН!, а ера хуесос**\n"
                    #"Ты не знаешь локацию. Слушай остальных, задавай общие вопросы "
                    #"и постарайся вычислить локацию, оставшись незамеченным.\n"
                    f"Вариантов локаций всего: {len(LOCATIONS)} — можешь свериться "
                    "командой `!spy_locations`, чтобы освежить в памяти список.",
                    file=get_role_image(),
                )
            else:
                role = random.choice(LOCATIONS[game.location])
                await member.send(
                    f"📍 **Локация:** {game.location}\n"
                    f"👤 **Твоя роль:** {role}\n\n"
                    #"Среди игроков прячется шпион, который не знает локацию. "
                    #"Задавайте друг другу вопросы, чтобы вычислить его, но не "
                    #"выдавайте локацию слишком явно!",
                    file=get_role_image(),
                )
    except discord.Forbidden:
        # Не удалось написать в личку (закрыты ЛС)
        raise


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user} (id: {bot.user.id})")


@bot.command(name="spy_help")
async def spy_help(ctx: commands.Context):
    await ctx.send(
        "**Команды игры «Шпион»:**\n"
        "`!spy_create` — создать лобби\n"
        "`!spy_join` — присоединиться\n"
        "`!spy_leave` — выйти из лобби\n"
        "`!spy_players` — список игроков\n"
        "`!spy_locations` — список локаций\n"
        "`!spy_start [минуты]` — начать/перезапустить раунд (по умолчанию максимум — 180 минут)\n"
        "`!spy_end` — завершить раунд и раскрыть роли (лобби остаётся открытым)\n"
        "`!spy_close` — полностью распустить лобби\n\n"
        "**Для админов (право «Управление сервером»):**\n"
        "`!spy_words add <слово>` — добавить слово в пул\n"
        "`!spy_words addmany сл1, сл2, ...` — добавить сразу несколько слов\n"
        "`!spy_words remove <слово>` — удалить слово\n"
        "`!spy_words list` — показать пул слов\n"
        "`!spy_words clear` — очистить пул (вернуться к встроенным локациям)"
    )


@bot.command(name="spy_create")
async def spy_create(ctx: commands.Context):
    if get_game(ctx.channel.id):
        await ctx.send("В этом канале уже есть активное лобби. Используй `!spy_join`.")
        return
    games[ctx.channel.id] = SpyGame(ctx.channel.id, ctx.author)
    await ctx.send(
        f"🎮 Лобби создано игроком {ctx.author.mention}!\n"
        f"Присоединяйтесь командой `!spy_join` (минимум {MIN_PLAYERS} игрока)."
    )


@bot.command(name="spy_join")
async def spy_join(ctx: commands.Context):
    game = get_game(ctx.channel.id)
    if not game:
        await ctx.send("Сначала создайте лобби командой `!spy_create`.")
        return
    if game.started:
        await ctx.send("Игра уже началась, дождитесь следующего раунда.")
        return
    if game.add_player(ctx.author):
        await ctx.send(f"{ctx.author.mention} присоединился! Игроков: {len(game.players)}.")
    else:
        await ctx.send("Ты уже в лобби.")


@bot.command(name="spy_leave")
async def spy_leave(ctx: commands.Context):
    game = get_game(ctx.channel.id)
    if not game:
        await ctx.send("Активного лобби нет.")
        return
    if game.remove_player(ctx.author):
        await ctx.send(f"{ctx.author.mention} покинул лобби. Игроков: {len(game.players)}.")
        if not game.players:
            games.pop(ctx.channel.id, None)
            await ctx.send("Лобби распущено — не осталось игроков.")
    else:
        await ctx.send("Тебя не было в лобби.")


@bot.command(name="spy_players")
async def spy_players(ctx: commands.Context):
    game = get_game(ctx.channel.id)
    if not game or not game.players:
        await ctx.send("В лобби пока никого нет.")
        return
    names = "\n".join(f"• {p.display_name}" for p in game.players)
    await ctx.send(f"**Игроки ({len(game.players)}):**\n{names}")


@bot.command(name="spy_locations")
async def spy_locations(ctx: commands.Context):
    names = ", ".join(sorted(LOCATIONS.keys()))
    await ctx.send(f"**Возможные локации ({len(LOCATIONS)}):**\n{names}")


@bot.group(name="spy_words", invoke_without_command=True)
async def spy_words(ctx: commands.Context):
    await ctx.send(
        "Используй подкоманды: `!spy_words add <слово>`, "
        "`!spy_words addmany сл1, сл2, ...`, `!spy_words remove <слово>`, "
        "`!spy_words list`, `!spy_words clear`."
    )


@spy_words.command(name="add")
@commands.has_permissions(manage_guild=True)
async def spy_words_add(ctx: commands.Context, *, word: str):
    if not ctx.guild:
        await ctx.send("Эта команда работает только на сервере.")
        return
    word = word.strip()
    if not word:
        await ctx.send("Слово не может быть пустым.")
        return
    pool = custom_word_pools.setdefault(ctx.guild.id, [])
    if word.lower() in (w.lower() for w in pool):
        await ctx.send(f"Слово «{word}» уже есть в пуле.")
        return
    pool.append(word)
    await ctx.send(f"✅ Добавлено слово «{word}». Сейчас в пуле: {len(pool)}.")


@spy_words.command(name="addmany")
@commands.has_permissions(manage_guild=True)
async def spy_words_addmany(ctx: commands.Context, *, words: str):
    if not ctx.guild:
        await ctx.send("Эта команда работает только на сервере.")
        return
    pool = custom_word_pools.setdefault(ctx.guild.id, [])
    existing_lower = {w.lower() for w in pool}
    added = []
    skipped = []
    for raw in words.split(","):
        word = raw.strip()
        if not word:
            continue
        if word.lower() in existing_lower:
            skipped.append(word)
            continue
        pool.append(word)
        existing_lower.add(word.lower())
        added.append(word)

    parts = []
    if added:
        parts.append(f"✅ Добавлено: {', '.join(added)}")
    if skipped:
        parts.append(f"⏭️ Пропущено (уже были в пуле): {', '.join(skipped)}")
    if not parts:
        parts.append("Не найдено ни одного слова для добавления.")
    parts.append(f"Всего слов в пуле: {len(pool)}.")
    await ctx.send("\n".join(parts))


@spy_words.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def spy_words_remove(ctx: commands.Context, *, word: str):
    if not ctx.guild:
        await ctx.send("Эта команда работает только на сервере.")
        return
    pool = custom_word_pools.get(ctx.guild.id, [])
    word = word.strip()
    for existing in list(pool):
        if existing.lower() == word.lower():
            pool.remove(existing)
            await ctx.send(f"🗑️ Слово «{existing}» удалено. Осталось: {len(pool)}.")
            return
    await ctx.send(f"Слово «{word}» не найдено в пуле.")


@spy_words.command(name="list")
async def spy_words_list(ctx: commands.Context):
    if not ctx.guild:
        await ctx.send("Эта команда работает только на сервере.")
        return
    pool = custom_word_pools.get(ctx.guild.id, [])
    if not pool:
        await ctx.send(
            "Пул слов пуст. Игра будет использовать встроенные локации "
            "(см. `!spy_locations`). Добавить слова: `!spy_words add <слово>`."
        )
        return
    numbered = "\n".join(f"{i}. {w}" for i, w in enumerate(pool, start=1))
    await ctx.send(f"**Пул слов ({len(pool)}):**\n{numbered}")


@spy_words.command(name="clear")
@commands.has_permissions(manage_guild=True)
async def spy_words_clear(ctx: commands.Context):
    if not ctx.guild:
        await ctx.send("Эта команда работает только на сервере.")
        return
    custom_word_pools.pop(ctx.guild.id, None)
    await ctx.send("🧹 Пул слов очищен. Игра снова будет использовать встроенные локации.")


@spy_words_add.error
@spy_words_addmany.error
@spy_words_remove.error
@spy_words_clear.error
async def spy_words_admin_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ Управлять пулом слов могут только участники с правом «Управление сервером».")
    else:
        raise error


@bot.command(name="spy_start")
async def spy_start(ctx: commands.Context, minutes: int = MAX_ROUND_MINUTES):
    game = get_game(ctx.channel.id)
    if not game:
        await ctx.send("Сначала создайте лобби командой `!spy_create`.")
        return
    if game.started:
        await ctx.send("Игра уже идёт.")
        return
    if len(game.players) < MIN_PLAYERS:
        await ctx.send(f"Нужно минимум {MIN_PLAYERS} игрока, сейчас: {len(game.players)}.")
        return
    if minutes <= 0 or minutes > MAX_ROUND_MINUTES:
        await ctx.send(f"Укажи разумную длительность раунда: от 1 до {MAX_ROUND_MINUTES} минут.")
        return

    guild_words = custom_word_pools.get(ctx.guild.id) if ctx.guild else None

    # Сбрасываем данные предыдущего раунда (если он был) перед новым запуском.
    game.location = None
    game.word = None

    game.started = True
    if guild_words:
        game.word = random.choice(guild_words)
    else:
        game.location = random.choice(list(LOCATIONS.keys()))
    game.spy = random.choice(game.players)

    failed_dm = []
    for player in game.players:
        try:
            await send_role_dm(player, game)
        except discord.Forbidden:
            failed_dm.append(player)

    await ctx.send(
        f"🚨 Игра началась! Роли разосланы в личные сообщения.\n"
        f"⏱️ Таймер раунда: {minutes} мин. Когда закончите — используйте `!spy_end`."
    )
    if failed_dm:
        mentions = ", ".join(p.mention for p in failed_dm)
        await ctx.send(
            f"⚠️ Не удалось отправить ЛС этим игрокам (у них закрыты личные сообщения): {mentions}. "
            "Попросите их открыть ЛС и создать лобби заново, либо сообщите роль вручную."
        )

    async def round_timer():
        await asyncio.sleep(minutes * 60)
        if games.get(ctx.channel.id) is game and game.started:
            await ctx.send("⏰ Время раунда вышло! Используйте `!spy_end`, чтобы раскрыть роли.")

    game.timer_task = asyncio.create_task(round_timer())


@bot.command(name="spy_end")
async def spy_end(ctx: commands.Context):
    game = get_game(ctx.channel.id)
    if not game or not game.started:
        await ctx.send("Сейчас нет активной игры.")
        return

    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()

    if game.word_mode:
        reveal_line = f"🔑 Слово было: **{game.word}**"
    else:
        reveal_line = f"📍 Локация была: **{game.location}**"

    await ctx.send(
        f"🏁 **Раунд окончен!**\n"
        f"{reveal_line}\n"
        f"🕵️ Шпионом был: **{game.spy.display_name}**\n\n"
        f"Лобби осталось открытым ({len(game.players)} игрока(ов)). "
        "Новый раунд с теми же игроками — `!spy_start`. "
        "Полностью распустить лобби — `!spy_close`."
    )

    # Раунд завершён, но лобби и список игроков сохраняются для следующего !spy_start.
    game.started = False
    game.location = None
    game.word = None
    game.spy = None
    game.timer_task = None


@bot.command(name="spy_close")
async def spy_close(ctx: commands.Context):
    game = get_game(ctx.channel.id)
    if not game:
        await ctx.send("В этом канале нет активного лобби.")
        return
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
    games.pop(ctx.channel.id, None)
    await ctx.send("🧹 Лобби распущено. Чтобы начать заново — `!spy_create`.")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        raise SystemExit(
            "Не найден токен бота. Установи переменную окружения DISCORD_BOT_TOKEN "
            "или впиши токен напрямую в код (не рекомендуется для публичных репозиториев)."
        )
    bot.run(TOKEN)
