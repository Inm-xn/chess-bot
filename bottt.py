import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
 
import feedparser
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
 
 
# =========================
# Настройки (вставьте свои значения)
# =========================
TOKEN = "8758955979:AAGrNafupgnJ7_9JveAzYQ28n3nyKJbJYmU"
ADMIN_ID = 6626734308
CHANNEL_ID = -1003585416242
DEEPL_API_KEY = "8fd00432-7185-496b-bc0f-ba128a4ef8a8:fx"  # <-- сюда вставьте API ключ DeepL
 
STATE_FILE = Path("last_seen_news.json")
 
NEWS_SOURCES = [
    {"name": "Chess.com News", "url": "https://www.chess.com/rss/news"},
    {"name": "FIDE", "url": "https://www.fide.com/feed/"},
    {"name": "Chessbase", "url": "https://en.chessbase.com/feed"},
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
    key: str
    title: str
    link: str
    source_name: str
 
 
PENDING: Dict[str, NewsItem] = {}
PENDING_LOCK = asyncio.Lock()
 
 
# =========================
# Перевод через DeepL
# =========================
async def translate_to_russian(text: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-free.deepl.com/v2/translate",
                data={
                    "auth_key": DEEPL_API_KEY,
                    "text": text,
                    "target_lang": "RU",
                },
                timeout=10,
            )
            result = response.json()
            return result["translations"][0]["text"]
    except Exception:
        logger.exception("Ошибка перевода DeepL, возвращаем оригинал.")
        return text
 
 
# =========================
# Вспомогательные функции
# =========================
def _make_key(raw: str) -> str:
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
 
 
def format_post(item: NewsItem, translated_title: str) -> str:
    return (
        f"♟ {translated_title}\n\n"
        f"🌐 {item.source_name}\n"
        f"👉 {item.link}"
    )
 
 
def load_state() -> Dict[str, Optional[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        logger.exception("Не удалось прочитать state-файл.")
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
    if not title or not link:
        return None
    raw_id = f"{source_name}|{title}|{link}"
    key = _make_key(raw_id)
    return NewsItem(key=key, title=title, link=link, source_name=source_name)
 
 
async def fetch_feed(url: str) -> Any:
    return await asyncio.to_thread(feedparser.parse, url)
 
 
def extract_unseen_items(
    items_newest_first: List[NewsItem],
    last_seen_key: Optional[str],
) -> List[NewsItem]:
    if not items_newest_first:
        return []
    current_keys = [it.key for it in items_newest_first]
    if not last_seen_key:
        return []
    if last_seen_key not in current_keys:
        return []
    idx = current_keys.index(last_seen_key)
    unseen_newest = items_newest_first[:idx]
    return list(reversed(unseen_newest))
 
 
# =========================
# Handlers
# =========================
async def safe_clear_keyboard(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
 
 
async def handle_publish(callback: CallbackQuery, bot: Bot) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)
    async with PENDING_LOCK:
        item = PENDING.get(key)
    if not item:
        await callback.answer("Не найдено (возможно, бот перезапускался).", show_alert=True)
        return
    translated_title = await translate_to_russian(item.title)
    await bot.send_message(CHANNEL_ID, format_post(item, translated_title))
    await callback.answer("Опубликовано")
    await safe_clear_keyboard(callback)
 
 
async def handle_skip(callback: CallbackQuery) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)
    async with PENDING_LOCK:
        PENDING.pop(key, None)
    await callback.answer("Пропущено")
    await safe_clear_keyboard(callback)
 
 
# =========================
# RSS polling
# =========================
async def poll_loop(bot: Bot) -> None:
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
        items_newest_first = items
        last_seen_key = state.get(source_name)
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
            async with PENDING_LOCK:
                PENDING[item.key] = item
            # Показываем админу оригинал + перевод для предпросмотра
            translated_title = await translate_to_russian(item.title)
            preview_text = (
                f"📰 Новая новость!\n\n"
                f"🇬🇧 {item.title}\n"
                f"🇷🇺 {translated_title}\n\n"
                f"🌐 {source_name}\n"
                f"👉 {item.link}"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{item.key}")],
                [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{item.key}")],
            ])
            await bot.send_message(ADMIN_ID, preview_text, reply_markup=keyboard)
            state[source_name] = item.key
            save_state(state)
            logger.info("Отправлено администратору: %s", item.title)
 
 
# =========================
# Запуск
# =========================
async def main() -> None:
    bot = Bot(token=TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook удален")
    dp = Dispatcher()
 
    @dp.callback_query(F.data.startswith("publish:"))
    async def _publish_cb(callback: CallbackQuery) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор может публиковать.", show_alert=True)
            return
        await handle_publish(callback, bot)
 
    @dp.callback_query(F.data.startswith("skip:"))
    async def _skip_cb(callback: CallbackQuery) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор может пропускать.", show_alert=True)
            return
        await handle_skip(callback)
 
    rss_task = asyncio.create_task(poll_loop(bot))
    try:
        logger.info("Запуск long-polling Telegram.")
        await dp.start_polling(bot)
    finally:
        rss_task.cancel()
 
 
if __name__ == "__main__":
    asyncio.run(main())
