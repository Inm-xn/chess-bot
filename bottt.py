import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup


# =========================
# Настройки (вставьте свои значения)
# =========================
TOKEN = "8758955979:AAGrNafupgnJ7_9JveAzYQ28n3nyKJbJYmU"          # <-- сюда вставьте TOKEN бота
ADMIN_ID = 6626734308                   # <-- сюда вставьте ADMIN_ID (ваш Telegram user id)
CHANNEL_ID = -1003585416242           # <-- сюда вставьте CHANNEL_ID (id вашего канала)

# Куда сохранять “последнюю обработанную новость” (чтобы не дублировать)
STATE_FILE = Path("last_seen_news.json")

# RSS источник(и). Для расширения просто добавьте ещё элементы в список.
# Пример: {"name": "Other Source", "url": "https://example.com/rss"}
NEWS_SOURCES = [
    {"name": "Chess.com News", "url": "https://www.chess.com/rss/news"},
    {"name": "FIDE", "url": "https://www.fide.com/feed/"},
]


# =========================
# Логирование
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("news-bot")


# =========================
# Модели
# =========================
@dataclass(frozen=True)
class NewsItem:
    key: str         # короткий ключ (hash) для callback_data (до 64 байт)
    title: str
    link: str
    source_name: str


# Хранилище “ожидающих решения” постов (в памяти).
# Для простоты: если бот перезапустился, админские кнопки для уже отправленных сообщений могут “устареть”.
PENDING: Dict[str, NewsItem] = {}
PENDING_LOCK = asyncio.Lock()


# =========================
# Вспомогательные функции (state / ids / форматирование)
# =========================
def _make_key(raw: str) -> str:
    """
    Делает короткий id для callback_data (и для state), чтобы не упираться в лимит Telegram.
    md5 (32 hex chars) + префикс publish:/skip: остаются в лимите.
    """
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def format_post(item: NewsItem) -> str:
    # Требуемое оформление:
    # "♟ {title}\n\nИсточник: {link}"
    return (
    f"♟ {item.title}\n\n"
    f"🌐 {item.source_name}\n"
    f"👉 {item.link}"
)


def load_state() -> Dict[str, Optional[str]]:
    """
    State хранит last_seen key для каждого источника.
    Формат файла: {"Chess.com News": "abc123...", "Other Source": "..."}
    """
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        logger.exception("Не удалось прочитать state-файл, используем пустое состояние.")
    return {}


def save_state(state: Dict[str, Optional[str]]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_entry_field(entry: Any, *keys: str) -> Optional[str]:
    for k in keys:
        v = entry.get(k) if isinstance(entry, dict) else None
        if v:
            return str(v).strip()
    return None


def entry_to_item(entry: Any, source_name: str) -> Optional[NewsItem]:
    title = get_entry_field(entry, "title")
    link = get_entry_field(entry, "link", "links", "id")

    # feedparser иногда кладет link в entry.link, а title может быть странный/пустой
    if not title:
        return None
    if not link:
        # если даже link нет — пропускаем (иначе нечего публиковать)
        return None

    raw_id = f"{source_name}|{title}|{link}"
    key = _make_key(raw_id)

    return NewsItem(
        key=key,
        title=title,
        link=link,
        source_name=source_name,
    )


async def fetch_feed(url: str) -> Any:
    # feedparser работает синхронно и может блокировать event loop, поэтому выносим в поток.
    return await asyncio.to_thread(feedparser.parse, url)


def extract_unseen_items(
    items_newest_first: List[NewsItem],
    last_seen_key: Optional[str],
) -> List[NewsItem]:
    """
    Определяем новые элементы относительно last_seen_key.
    Предполагаем, что feed обычно идет от новых к старым.
    Возвращаем unseen items в порядке "сначала старее, потом новее" для удобства.
    """
    if not items_newest_first:
        return []

    current_keys = [it.key for it in items_newest_first]
    newest_key = current_keys[0]

    if not last_seen_key:
        # Первый запуск: не спамим админа текущими старыми новостями
        return []

    if last_seen_key not in current_keys:
        # Если последний ключ “пропал” (например, структура RSS изменилась),
        # безопаснее не рассылать всё подряд: просто обновим last_seen_key на newest.
        return []

    idx = current_keys.index(last_seen_key)
    unseen_newest = items_newest_first[:idx]  # все, что новее последнего обработанного
    return list(reversed(unseen_newest))     # отправляем в хронологии


# =========================
# Handlers callback-кнопок
# =========================
async def safe_clear_keyboard(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        # Например, если сообщение уже редактировали или истекла возможность редактирования
        pass


async def handle_publish(callback: CallbackQuery, bot: Bot) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)

    async with PENDING_LOCK:
        item = PENDING.get(key)

    if not item:
        await callback.answer("Не найдено (возможно, бот перезапускался).", show_alert=True)
        return

    await bot.send_message(CHANNEL_ID, format_post(item))
    await callback.answer("Опубликовано")
    await safe_clear_keyboard(callback)


async def handle_skip(callback: CallbackQuery) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)

    # Ничего не делаем кроме “ответа” и очистки клавиатуры
    async with PENDING_LOCK:
        PENDING.pop(key, None)

    await callback.answer("Пропущено")
    await safe_clear_keyboard(callback)


# =========================
# Основной polling-цикл RSS
# =========================
async def poll_loop(bot: Bot) -> None:
    """
    Каждые 20 секунд:
    - проверяет RSS
    - если есть новые новости: отправляет администратору с кнопками
    - запоминает last_seen, чтобы не дублировать
    """
    state = load_state()
    logger.info("RSS polling стартовал. State источников: %s", list(state.keys()))

    while True:
        try:
            await poll_once(bot, state)
        except Exception:
            logger.exception("Ошибка во время проверки RSS.")

        await asyncio.sleep(20)


async def poll_once(bot: Bot, state: Dict[str, Optional[str]]) -> None:
    for source in NEWS_SOURCES:
        source_name = source["name"]
        url = source["url"]

        logger.info("Проверка RSS: %s (%s)", source_name, url)

        feed = await fetch_feed(url)
        entries = list(getattr(feed, "entries", []) or [])

        items: List[NewsItem] = []
        for entry in entries:
            item = entry_to_item(entry, source_name)
            if item:
                items.append(item)

        # feedparser обычно дает newest first — используем как есть
        items_newest_first = items

        last_seen_key = state.get(source_name)

        # Если ключа нет (первый запуск) — запомним newest и не будем присылать админу.
        if not last_seen_key and items_newest_first:
            state[source_name] = items_newest_first[0].key
            save_state(state)
            logger.info("Первый запуск для '%s': last_seen зафиксирован на newest.", source_name)
            continue

        unseen_items = extract_unseen_items(items_newest_first, last_seen_key)
        if not unseen_items:
            logger.info("Новых новостей для '%s' нет.", source_name)
            continue

        for item in unseen_items:
            # Сохраняем в pending до решения админа
            async with PENDING_LOCK:
                PENDING[item.key] = item

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Опубликовать",
                        callback_data=f"publish:{item.key}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Пропустить",
                        callback_data=f"skip:{item.key}",
                    )
                ],
            ])

            text = format_post(item)
            await bot.send_message(ADMIN_ID, text, reply_markup=keyboard)

            # Важно: запоминаем “последнюю отправленную” новость на этапе отправки админу,
            # чтобы не дублировать при “Пропустить”.
            state[source_name] = item.key
            save_state(state)

            logger.info("Отправлено администратору: %s", item.title)


# =========================
# Запуск
# =========================
async def main() -> None:
    bot = Bot(token=TOKEN)
    
    # Удаляем webhook перед запуском polling
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook удален")
    
    dp = Dispatcher()

    # publish
    @dp.callback_query(F.data.startswith("publish:"))
    async def _publish_cb(callback: CallbackQuery) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор может публиковать.", show_alert=True)
            return
        await handle_publish(callback, bot)

    # skip
    @dp.callback_query(F.data.startswith("skip:"))
    async def _skip_cb(callback: CallbackQuery) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор может пропускать.", show_alert=True)
            return
        await handle_skip(callback)

    # Фоновый polling
    rss_task = asyncio.create_task(poll_loop(bot))

    try:
        logger.info("Запуск long-polling Telegram.")
        await dp.start_polling(bot)
    finally:
        rss_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())