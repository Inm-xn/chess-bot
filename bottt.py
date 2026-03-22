import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
 
import feedparser
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, PhotoSize
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
 
 
# =========================
# Настройки
# =========================
TOKEN = "8758955979:AAGrNafupgnJ7_9JveAzYQ28n3nyKJbJYmU"
ADMIN_ID = 6626734308
CHANNEL_ID = -1003585416242
DEEPL_API_KEY = "В9c699384-f4bb-43be-b0e7-39d58d6748c6:fx"
 
STATE_FILE = Path("last_seen_news.json")
 
NEWS_SOURCES = [
    {"name": "Chess.com News", "url": "https://www.chess.com/rss/news"},
    {"name": "FIDE", "url": "https://www.fide.com/feed/"},
    {"name": "Chessbase", "url": "https://en.chessbase.com/feed"},
    {"name": "Chessdom", "url": "https://chessdom.com/feed"},
    {"name": "The Week in Chess", "url": "https://theweekinchess.com/twic-rss-feed"},
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
# FSM состояния
# =========================
class EditStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_comment = State()
 
 
# =========================
# Модели
# =========================
@dataclass
class NewsItem:
    key: str
    title: str
    link: str
    source_name: str
    image_url: Optional[str]
    summary: Optional[str]
    custom_title: Optional[str] = None
    custom_comment: Optional[str] = None
    custom_photo: Optional[str] = None
 
 
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
                headers={
                    "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "text": [text],
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
 
 
def clean_html(text: str) -> str:
    clean = re.sub(r'<[^>]+>', '', text)
    clean = clean.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    return clean.strip()
 
 
def get_hashtags(source_name: str) -> str:
    tags = "#шахматы #chess #новости"
    if "Chess.com" in source_name:
        tags += " #chessdotcom"
    elif "FIDE" in source_name:
        tags += " #FIDE"
    elif "Chessbase" in source_name:
        tags += " #chessbase"
    return tags
 
 
def format_post(item: NewsItem, translated_title: str, translated_summary: Optional[str]) -> str:
    from datetime import datetime
    date = datetime.now().strftime("%d.%m.%Y")
    hashtags = get_hashtags(item.source_name)
 
    title = item.custom_title or translated_title
    text = f"♟ *{title}*\n\n"
 
    if item.custom_comment:
        text += f"💬 _{item.custom_comment}_\n\n"
 
    if translated_summary:
        text += f"{translated_summary}\n\n"
 
    text += f"📅 {date}\n"
    text += f"🌐 {item.source_name}\n"
    text += f"👉 [Читать полностью]({item.link})\n\n"
    text += hashtags
 
    return text
 
 
def build_admin_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{key}")],
        [InlineKeyboardButton(text="📷 Добавить фото", callback_data=f"photo:{key}")],
        [InlineKeyboardButton(text="✏️ Изменить заголовок", callback_data=f"edittitle:{key}")],
        [InlineKeyboardButton(text="💬 Добавить комментарий", callback_data=f"comment:{key}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{key}")],
    ])
 
 
def extract_image_url(entry: Any) -> Optional[str]:
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        for m in media:
            url = m.get("url", "")
            if url and any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                return url
 
    thumb = getattr(entry, "media_thumbnail", None)
    if thumb and isinstance(thumb, list) and thumb[0].get("url"):
        return thumb[0]["url"]
 
    enclosures = getattr(entry, "enclosures", None)
    if enclosures:
        for enc in enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href") or enc.get("url")
 
    links = getattr(entry, "links", [])
    for link in links:
        if "image" in link.get("type", ""):
            return link.get("href")
 
    return None
 
 
def extract_summary(entry: Any) -> Optional[str]:
    summary = getattr(entry, "summary", None) or getattr(entry, "description", None)
    if summary:
        cleaned = clean_html(summary)
        if len(cleaned) > 300:
            cleaned = cleaned[:300].rsplit(' ', 1)[0] + "..."
        return cleaned if cleaned else None
    return None
 
 
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
    image_url = extract_image_url(entry)
    summary = extract_summary(entry)
    raw_id = f"{source_name}|{title}|{link}"
    key = _make_key(raw_id)
    return NewsItem(key=key, title=title, link=link, source_name=source_name, image_url=image_url, summary=summary)
 
 
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
# Публикация
# =========================
async def publish_item(item: NewsItem, bot: Bot) -> None:
    translated_title = await translate_to_russian(item.title)
    translated_summary = None
    if item.summary:
        translated_summary = await translate_to_russian(item.summary)
 
    caption = format_post(item, translated_title, translated_summary)
    photo = item.custom_photo or item.image_url
 
    if photo:
        try:
            await bot.send_photo(CHANNEL_ID, photo=photo, caption=caption, parse_mode="Markdown")
            return
        except Exception:
            logger.exception("Не удалось отправить фото, отправляем текст.")
 
    await bot.send_message(CHANNEL_ID, caption, parse_mode="Markdown")
 
 
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
    await publish_item(item, bot)
    async with PENDING_LOCK:
        PENDING.pop(key, None)
    await callback.answer("Опубликовано ✅")
    await safe_clear_keyboard(callback)
 
 
async def handle_skip(callback: CallbackQuery) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)
    async with PENDING_LOCK:
        PENDING.pop(key, None)
    await callback.answer("Пропущено")
    await safe_clear_keyboard(callback)
 
 
async def handle_photo_request(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)
    await state.update_data(pending_key=key)
    await state.set_state(EditStates.waiting_for_photo)
    await callback.message.answer("📷 Отправь фото для этой новости:")
    await callback.answer()
 
 
async def handle_photo_received(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    key = data.get("pending_key")
    if not key or not message.photo:
        await state.clear()
        return
    photo_id = message.photo[-1].file_id
    async with PENDING_LOCK:
        item = PENDING.get(key)
        if item:
            PENDING[key] = NewsItem(
                key=item.key, title=item.title, link=item.link,
                source_name=item.source_name, image_url=item.image_url,
                summary=item.summary, custom_title=item.custom_title,
                custom_comment=item.custom_comment, custom_photo=photo_id
            )
    await state.clear()
    await message.answer("✅ Фото добавлено! Теперь нажми 'Опубликовать'.")
 
 
async def handle_edittitle_request(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)
    await state.update_data(pending_key=key)
    await state.set_state(EditStates.waiting_for_title)
    await callback.message.answer("✏️ Напиши новый заголовок:")
    await callback.answer()
 
 
async def handle_title_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("pending_key")
    if not key or not message.text:
        await state.clear()
        return
    async with PENDING_LOCK:
        item = PENDING.get(key)
        if item:
            PENDING[key] = NewsItem(
                key=item.key, title=item.title, link=item.link,
                source_name=item.source_name, image_url=item.image_url,
                summary=item.summary, custom_title=message.text,
                custom_comment=item.custom_comment, custom_photo=item.custom_photo
            )
    await state.clear()
    await message.answer("✅ Заголовок изменён! Теперь нажми 'Опубликовать'.")
 
 
async def handle_comment_request(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    _, key = data.split(":", 1)
    await state.update_data(pending_key=key)
    await state.set_state(EditStates.waiting_for_comment)
    await callback.message.answer("💬 Напиши свой комментарий к новости:")
    await callback.answer()
 
 
async def handle_comment_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("pending_key")
    if not key or not message.text:
        await state.clear()
        return
    async with PENDING_LOCK:
        item = PENDING.get(key)
        if item:
            PENDING[key] = NewsItem(
                key=item.key, title=item.title, link=item.link,
                source_name=item.source_name, image_url=item.image_url,
                summary=item.summary, custom_title=item.custom_title,
                custom_comment=message.text, custom_photo=item.custom_photo
            )
    await state.clear()
    await message.answer("✅ Комментарий добавлен! Теперь нажми 'Опубликовать'.")
 
 
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
            translated_title = await translate_to_russian(item.title)
            image_info = "🖼 Есть картинка" if item.image_url else "📄 Без картинки"
            summary_info = f"\n\n📝 {item.summary[:150]}..." if item.summary else ""
            preview_text = (
                f"📰 Новая новость! {image_info}\n\n"
                f"🇬🇧 {item.title}\n"
                f"🇷🇺 {translated_title}"
                f"{summary_info}\n\n"
                f"🌐 {source_name}\n"
                f"👉 {item.link}"
            )
            await bot.send_message(ADMIN_ID, preview_text, reply_markup=build_admin_keyboard(item.key))
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
 
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
 
    # Publish
    @dp.callback_query(F.data.startswith("publish:"))
    async def _publish_cb(callback: CallbackQuery) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор.", show_alert=True)
            return
        await handle_publish(callback, bot)
 
    # Skip
    @dp.callback_query(F.data.startswith("skip:"))
    async def _skip_cb(callback: CallbackQuery) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор.", show_alert=True)
            return
        await handle_skip(callback)
 
    # Photo
    @dp.callback_query(F.data.startswith("photo:"))
    async def _photo_cb(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор.", show_alert=True)
            return
        await handle_photo_request(callback, state)
 
    @dp.message(EditStates.waiting_for_photo, F.photo)
    async def _photo_received(message: Message, state: FSMContext) -> None:
        await handle_photo_received(message, state, bot)
 
    # Edit title
    @dp.callback_query(F.data.startswith("edittitle:"))
    async def _edittitle_cb(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор.", show_alert=True)
            return
        await handle_edittitle_request(callback, state)
 
    @dp.message(EditStates.waiting_for_title, F.text)
    async def _title_received(message: Message, state: FSMContext) -> None:
        await handle_title_received(message, state)
 
    # Comment
    @dp.callback_query(F.data.startswith("comment:"))
    async def _comment_cb(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user and callback.from_user.id != ADMIN_ID:
            await callback.answer("Только администратор.", show_alert=True)
            return
        await handle_comment_request(callback, state)
 
    @dp.message(EditStates.waiting_for_comment, F.text)
    async def _comment_received(message: Message, state: FSMContext) -> None:
        await handle_comment_received(message, state)
 
    rss_task = asyncio.create_task(poll_loop(bot))
    try:
        logger.info("Запуск long-polling Telegram.")
        await dp.start_polling(bot)
    finally:
        rss_task.cancel()
 
 
if __name__ == "__main__":
    asyncio.run(main())
