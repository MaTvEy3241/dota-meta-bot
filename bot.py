"""
Dota 2 Meta Bot
================
Телеграм-бот, который показывает актуальную мету героев Dota 2
(винрейт / пикрейт / банрейт) и присылает картинку с иконками предметов
на закупку по фазам игры (старт / ранняя игра / мид / лейт).

Данные — с OpenDota API (бесплатный, официальный, синхронизирован со
Steam/Dotabuff). Иконки предметов — официальные иконки Dota 2 с CDN OpenDota,
поэтому отдельный стикерпак не нужен и иконки всегда совпадают с реальными
предметами и патчем.

Запуск:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN="твой_токен_от_BotFather"
    python bot.py
"""

import asyncio
import io
import logging
import os
import difflib
import re
from typing import Optional
from bs4 import BeautifulSoup

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    InputMediaPhoto,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dota_meta_bot")

OPENDOTA_BASE = "https://api.opendota.com/api"
# Иконки предметов отдаёт CDN самого Steam/Dota 2, а не OpenDota.
CDN_BASE = "https://cdn.cloudflare.steamstatic.com"
# Некоторые запросы к CDN Steam блокируют запросы без User-Agent.
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/153.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit(
        "Не найден токен бота. Установи переменную окружения TELEGRAM_BOT_TOKEN "
        "(получить токен можно у @BotFather в Telegram)."
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------------------
# Шрифт для рисования текста на картинке (с поддержкой кириллицы).
# Файлы лежат рядом с bot.py — если их нет, используем системный шрифт по умолчанию.
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_BOLD_PATH = os.path.join(BASE_DIR, "DejaVuSans-Bold.ttf")
FONT_REGULAR_PATH = os.path.join(BASE_DIR, "DejaVuSans.ttf")


def load_font(path: str, size: int, fallback_path: str = None) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        if fallback_path:
            try:
                return ImageFont.truetype(fallback_path, size)
            except Exception:
                pass
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Кэш данных из OpenDota (герои, предметы, патчи, иконки).
# Обновляется при старте и раз в 6 часов.
# ---------------------------------------------------------------------------

CACHE = {
    "heroes": {},          # hero_id -> {name, localized_name, primary_attr, roles}
    "name_to_id": {},      # localized_name.lower() -> hero_id
    "items": {},           # item_id -> {"dname": str, "img_url": str}
    "hero_stats": {},      # hero_id -> запись из /heroStats
    "patch_name": "неизвестен",
    "position_meta": {},
}
POSITION_META = {
    "pos5": {"label": "🛡️ Саппорт 5", "short": "Позиция 5", "dotabuff_position": "support-safe"},
    "pos4": {"label": "⚡ Саппорт 4", "short": "Позиция 4", "dotabuff_position": "support-off"},
    "carry": {"label": "🏹 Изи лейн", "short": "Позиция 1 / Carry", "dotabuff_position": "safe"},
    "mid": {"label": "🧠 Мид", "short": "Позиция 2 / Mid", "dotabuff_position": "mid"},
    "offlane": {"label": "💪 Хард лейн", "short": "Позиция 3 / Offlane", "dotabuff_position": "offlane"},
}
DOTABUFF_HERO_URL = "https://www.dotabuff.com/heroes"
DOTABUFF_PROXY = "https://r.jina.ai/http://www.dotabuff.com/heroes"


ICON_CACHE = {}  # item_id -> готовая (обрезанная) иконка (PIL.Image)

# Скобки ММР в OpenDota heroStats: 1=Herald .. 8=Immortal.
# Для "реальной высокоуровневой меты" берём Divine(7) + Immortal(8).
HIGH_SKILL_BRACKETS = [7, 8]
ALL_BRACKETS = list(range(1, 9))

ICON_SIZE = 72
ICON_MARGIN = 10
LABEL_COL_WIDTH = 190


async def fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(
        url, headers=HTTP_HEADERS, timeout=aiohttp.ClientTimeout(total=20)
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def refresh_cache():
    async with aiohttp.ClientSession() as session:
        try:
            heroes = await fetch_json(session, f"{OPENDOTA_BASE}/heroes")
            CACHE["heroes"].clear()
            CACHE["name_to_id"].clear()
            for h in heroes:
                CACHE["heroes"][h["id"]] = h
                CACHE["name_to_id"][h["localized_name"].lower()] = h["id"]
        except Exception as e:
            log.error("Не удалось загрузить героев: %s", e)
        try:
            items = await fetch_json(session, f"{OPENDOTA_BASE}/constants/items")
            CACHE["items"] = {}
            for internal_name, data in items.items():
                if "id" not in data:
                    continue
                img = data.get("img")
                img_url = None if not img else (img if img.startswith("http") else f"{CDN_BASE}{img}")
                CACHE["items"][data["id"]] = {"dname": data.get("dname", internal_name), "img_url": img_url}
        except Exception as e:
            log.error("Не удалось загрузить предметы: %s", e)
        try:
            stats = await fetch_json(session, f"{OPENDOTA_BASE}/heroStats")
            CACHE["hero_stats"] = {x["id"]: x for x in stats}
        except Exception as e:
            log.error("Не удалось загрузить heroStats: %s", e)
        try:
            patches = await fetch_json(session, f"{OPENDOTA_BASE}/constants/patch")
            if patches:
                CACHE["patch_name"] = patches[-1]["name"]
        except Exception as e:
            log.error("Не удалось загрузить список патчей: %s", e)
        await refresh_position_meta(session)


async def fetch_text(session, url: str) -> str:
    # Сначала пробуем сам DOTABUFF. Если сервер бота получает Cloudflare/403,
    # используем Jina Reader как прозрачный текстовый прокси к той же странице.
    try:
        async with session.get(
            url,
            headers=HTTP_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status < 400:
                text = await resp.text()
                if len(text) > 5000:
                    return text
            log.warning("DOTABUFF direct request returned HTTP %s", resp.status)
    except Exception as e:
        log.warning("DOTABUFF direct request failed: %s", e)

    proxy_url = "https://r.jina.ai/http://" + url.removeprefix("https://")
    async with session.get(
        proxy_url,
        headers={"User-Agent": "DotaMetaBot/1.0"},
        timeout=aiohttp.ClientTimeout(total=45),
    ) as resp:
        resp.raise_for_status()
        return await resp.text()


def _percent(value: str) -> Optional[float]:
    try:
        return float(value.replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _number(value: str) -> int:
    try:
        return int(value.replace(",", "").replace(" ", "").strip())
    except (TypeError, ValueError):
        return 0


def parse_dotabuff_hero_table(content: str) -> list:
    # Вариант 1: настоящий HTML DOTABUFF.
    soup = BeautifulSoup(content, "html.parser")
    table = soup.find("table")
    if table:
        headers = [x.get_text(" ", strip=True).lower() for x in table.find_all("th")]
        def idx(*names):
            for name in names:
                for i, h in enumerate(headers):
                    if name in h:
                        return i
            return None
        hero_i = idx("hero")
        matches_i = idx("matches")
        pick_i = idx("pick rate")
        win_i = idx("win rate")
        if hero_i is not None:
            rows = []
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) <= hero_i:
                    continue
                vals = [c.get_text(" ", strip=True) for c in cells]
                name = vals[hero_i]
                if not name:
                    continue
                win = _percent(vals[win_i]) if win_i is not None and win_i < len(vals) else None
                pick = _percent(vals[pick_i]) if pick_i is not None and pick_i < len(vals) else 0.0
                matches = _number(vals[matches_i]) if matches_i is not None and matches_i < len(vals) else 0
                if win is not None:
                    rows.append({"name": name, "matches": matches, "pick": pick or 0.0, "win": win})
            if rows:
                return rows

    # Вариант 2: Jina Reader возвращает Markdown/обычный текст.
    rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or "Hero" in line or "---" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 5:
            continue
        # Jina Reader иногда превращает ячейку Hero в Markdown-ссылку с
        # картинкой. В Telegram нам нужно оставить только название героя.
        raw_name = parts[0]
        raw_name = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", raw_name)
        raw_name = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", raw_name)
        raw_name = re.sub(r"https?://\S+", "", raw_name)
        raw_name = re.sub(r"^Image\s*\d+\s*:\s*", "", raw_name, flags=re.IGNORECASE)
        name = BeautifulSoup(raw_name, "html.parser").get_text(" ", strip=True).strip()
        # Обычно: Hero | Tier | Win rate | Change | Pick rate | Change | Ban rate
        win = _percent(parts[2]) if len(parts) > 2 else None
        pick = _percent(parts[4]) if len(parts) > 4 else 0.0
        if name and win is not None:
            rows.append({"name": name, "matches": 0, "pick": pick or 0.0, "win": win})

    return rows


async def refresh_position_meta(session):
    result = {}
    for key, cfg in POSITION_META.items():
        url = (
            f"{DOTABUFF_HERO_URL}?show=heroes&view=meta&mode=all-pick"
            f"&date=7d&position={cfg['dotabuff_position']}"
        )
        try:
            html = await fetch_text(session, url)
            rows = parse_dotabuff_hero_table(html)
            if rows:
                result[key] = rows[:15]
                log.info("DOTABUFF %s: %d heroes", key, len(rows))
            else:
                log.warning("DOTABUFF %s: empty table", key)
        except Exception as e:
            log.warning("DOTABUFF %s error: %s", key, e)
    if result:
        CACHE["position_meta"] = result


async def periodic_refresh():
    while True:
        await asyncio.sleep(6 * 60 * 60)  # каждые 6 часов
        await refresh_cache()


# ---------------------------------------------------------------------------
# Расчёт винрейта/пикрейта/банрейта
# ---------------------------------------------------------------------------

def compute_rates(stat: dict, brackets: list) -> Optional[dict]:
    total_pick = 0
    total_win = 0
    for b in brackets:
        total_pick += stat.get(f"{b}_pick", 0) or 0
        total_win += stat.get(f"{b}_win", 0) or 0
    if total_pick == 0:
        return None
    return {
        "picks": total_pick,
        "wins": total_win,
        "winrate": 100 * total_win / total_pick,
    }


def compute_pro_rates(stat: dict) -> dict:
    pro_pick = stat.get("pro_pick", 0) or 0
    pro_win = stat.get("pro_win", 0) or 0
    pro_ban = stat.get("pro_ban", 0) or 0
    total_pro_games = max(pro_pick + pro_ban, 1)
    return {
        "pro_pick": pro_pick,
        "pro_win": pro_win,
        "pro_ban": pro_ban,
        "pro_winrate": (100 * pro_win / pro_pick) if pro_pick else 0,
        "pro_pickrate": 100 * pro_pick / total_pro_games,
        "pro_banrate": 100 * pro_ban / total_pro_games,
    }


def find_hero_id(query: str) -> Optional[int]:
    query = query.strip().lower()
    if not query:
        return None
    if query in CACHE["name_to_id"]:
        return CACHE["name_to_id"][query]
    for name, hid in CACHE["name_to_id"].items():
        if query in name:
            return hid
    close = difflib.get_close_matches(query, CACHE["name_to_id"].keys(), n=1, cutoff=0.6)
    if close:
        return CACHE["name_to_id"][close[0]]
    return None


# ---------------------------------------------------------------------------
# Текст меты
# ---------------------------------------------------------------------------

def format_meta_menu() -> str:
    return (
        "🎯 <b>DOTA 2 META ПО ПОЗИЦИЯМ</b>\n\n"
        "Выбери позицию — покажу TOP-15 героев по актуальной статистике DOTABUFF."
    )


def format_position_meta(position: str) -> str:
    cfg = POSITION_META.get(position)
    rows = CACHE["position_meta"].get(position, [])[:15]
    if not cfg:
        return "Неизвестная позиция."
    if not rows:
        return f"{cfg['label']}\n\n❌ Не удалось получить статистику DOTABUFF. Попробуй позже."
    lines = [
        f"{cfg['label']} — <b>TOP {len(rows)}</b>",
        f"<i>{cfg['short']} · DOTABUFF · последние 7 дней · All Pick</i>",
        "",
    ]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. <b>{row['name']}</b> — WR <b>{row['win']:.2f}%</b> · Pick {row['pick']:.2f}%"
        )
    lines.append("")
    lines.append("📊 Данные обновляются автоматически.")
    return "\n".join(lines)


def format_hero_caption(hero_id: int) -> str:
    hero = CACHE["heroes"].get(hero_id)
    stat = CACHE["hero_stats"].get(hero_id)
    if not hero or not stat:
        return "Не удалось найти данные по этому герою."

    high = compute_rates(stat, HIGH_SKILL_BRACKETS)
    overall = compute_rates(stat, ALL_BRACKETS)
    pro = compute_pro_rates(stat)

    lines = [
        f"🦸 <b>{hero['localized_name']}</b>",
        f"Патч: {CACHE['patch_name']}",
        "",
    ]
    if high:
        lines.append(f"Divine/Immortal: {high['winrate']:.1f}% винрейт ({high['picks']} игр)")
    if overall:
        lines.append(f"Все ранги: {overall['winrate']:.1f}% винрейт ({overall['picks']} игр)")
    lines.append(
        f"Про-сцена: {pro['pro_winrate']:.1f}% винрейт, "
        f"пикрейт {pro['pro_pickrate']:.1f}%, банрейт {pro['pro_banrate']:.1f}%"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Иконки предметов и генерация картинки закупки
# ---------------------------------------------------------------------------

async def get_item_icon(session: aiohttp.ClientSession, item_id: int):
    if item_id in ICON_CACHE:
        return ICON_CACHE[item_id]

    meta = CACHE["items"].get(item_id)
    if not meta or not meta.get("img_url"):
        return None

    try:
        async with session.get(
            meta["img_url"], headers=HTTP_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            raw = await resp.read()
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        img = img.resize((ICON_SIZE, ICON_SIZE))
        ICON_CACHE[item_id] = img
        return img
    except Exception as e:
        log.warning("Не удалось скачать иконку предмета %s (%s): %s", item_id, meta.get("img_url"), e)
        return None


async def fetch_item_popularity(hero_id: int) -> dict:
    async with aiohttp.ClientSession() as session:
        try:
            return await fetch_json(session, f"{OPENDOTA_BASE}/heroes/{hero_id}/itemPopularity")
        except Exception as e:
            log.error("itemPopularity error: %s", e)
            return {}


PHASES = [
    ("start_game_items", "СТАРТ"),
    ("early_game_items", "РАННЯЯ ИГРА"),
    ("mid_game_items", "МИДГЕЙМ"),
    ("late_game_items", "ЛЕЙТГЕЙМ"),
]


async def build_purchase_image(hero_id: int, items_per_phase: int = 4):
    hero = CACHE["heroes"].get(hero_id)
    if not hero:
        return None

    raw = await fetch_item_popularity(hero_id)
    if not raw:
        return None

    phase_rows = []  # (label, [(item_id, share_percent), ...])
    for key, label in PHASES:
        phase_items = raw.get(key, {})
        if not phase_items:
            continue
        total = sum(phase_items.values())
        top = sorted(phase_items.items(), key=lambda kv: kv[1], reverse=True)[:items_per_phase]
        row = [(int(item_id_str), 100 * count / total if total else 0) for item_id_str, count in top]
        phase_rows.append((label, row))

    if not phase_rows:
        return None

    max_items = max(len(row) for _, row in phase_rows)
    width = LABEL_COL_WIDTH + max_items * (ICON_SIZE + ICON_MARGIN) + ICON_MARGIN
    header_h = 80
    row_h = ICON_SIZE + ICON_MARGIN
    height = header_h + len(phase_rows) * row_h + ICON_MARGIN

    img = Image.new("RGB", (width, height), (24, 26, 31))
    draw = ImageDraw.Draw(img)

    font_title = load_font(FONT_BOLD_PATH, 24)
    font_sub = load_font(FONT_REGULAR_PATH, 14, FONT_BOLD_PATH)
    font_label = load_font(FONT_BOLD_PATH, 15)
    font_badge = load_font(FONT_BOLD_PATH, 13)

    stat = CACHE["hero_stats"].get(hero_id, {})
    high = compute_rates(stat, HIGH_SKILL_BRACKETS)
    wr_text = f"{high['winrate']:.1f}% винрейт (Divine/Immortal)" if high else "нет данных по винрейту"

    draw.text((ICON_MARGIN, 14), f"{hero['localized_name']} — закупка", font=font_title, fill=(255, 255, 255))
    draw.text((ICON_MARGIN, 46), f"{wr_text} · патч {CACHE['patch_name']}", font=font_sub, fill=(150, 200, 255))

    async with aiohttp.ClientSession() as session:
        y = header_h
        for label, row in phase_rows:
            draw.text((ICON_MARGIN, y + ICON_SIZE // 2 - 10), label, font=font_label, fill=(210, 190, 90))
            x = LABEL_COL_WIDTH
            icons = await asyncio.gather(*[get_item_icon(session, item_id) for item_id, _ in row])
            for (item_id, share), icon in zip(row, icons):
                if icon is not None:
                    img.paste(icon, (x, y), icon)
                else:
                    draw.rounded_rectangle([x, y, x + ICON_SIZE, y + ICON_SIZE], radius=8, fill=(60, 60, 60))
                badge_text = f"{share:.0f}%"
                bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
                bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
                bx = x + ICON_SIZE - bw - 8
                by = y + ICON_SIZE - bh - 8
                draw.rectangle([bx - 4, by - 3, x + ICON_SIZE - 1, y + ICON_SIZE - 1], fill=(0, 0, 0))
                draw.text((bx, by - 3), badge_text, font=font_badge, fill=(255, 255, 255))
                x += ICON_SIZE + ICON_MARGIN
            y += row_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Клавиатуры выбора героя по кнопкам
# ---------------------------------------------------------------------------

def position_meta_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, cfg in POSITION_META.items():
        kb.button(text=cfg["label"], callback_data=f"posmeta:{key}")
    kb.adjust(1)
    return kb.as_markup()


def position_back_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К позициям", callback_data="menu:positions")
    return kb.as_markup()


def hero_card_keyboard(hero_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=f"hero:{hero_id}")
    kb.button(text="⬅️ К позициям", callback_data="menu:positions")
    kb.adjust(2)
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👋 Привет! Я показываю актуальную мету Dota 2 и картинку с закупкой "
        "предметов по фазам игры (данные из OpenDota, синхронизированы с Dotabuff/Steam).\n\n"
        "Команды:\n"
        "/meta — топ героев текущей меты\n"
        "/hero <имя> — мета и закупка по герою, например: <code>/hero Pudge</code>\n"
        "/heroes — выбрать героя кнопками\n\n"
        "Можно просто написать имя героя текстом."
    )
    await message.answer(text)


@dp.message(Command("meta"))
async def cmd_meta(message: Message):
    await message.answer(format_meta_menu(), parse_mode="HTML", reply_markup=position_meta_keyboard())


@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    """Проверка: скачивается ли хоть одна иконка предмета с CDN."""
    sample_item_id = next(iter(CACHE["items"]), None)
    if sample_item_id is None:
        await message.answer("Кэш предметов пуст — данные ещё не загрузились.")
        return
    meta = CACHE["items"][sample_item_id]
    async with aiohttp.ClientSession() as session:
        icon = await get_item_icon(session, sample_item_id)
    status = "✅ скачалась" if icon is not None else "❌ не скачалась (см. лог в консоли бота)"
    await message.answer(
        f"Тестовый предмет: {meta['dname']}\nСсылка: {meta['img_url']}\nИконка: {status}"
    )


@dp.message(Command("heroes"))
async def cmd_heroes(message: Message):
    await message.answer(format_meta_menu(), parse_mode="HTML", reply_markup=position_meta_keyboard())


@dp.message(Command("hero"))
async def cmd_hero(message: Message):
    query = message.text.replace("/hero", "", 1).strip()
    if not query:
        await message.answer("Укажи имя героя, например: <code>/hero Pudge</code>", parse_mode="HTML")
        return
    await handle_hero_query(message, query)


@dp.message(F.text)
async def free_text_search(message: Message):
    await handle_hero_query(message, message.text)


async def handle_hero_query(message: Message, query: str):
    hero_id = find_hero_id(query)
    if hero_id is None:
        await message.answer(
            "Не нашёл такого героя. Проверь написание (на английском) "
            "или используй /heroes для выбора кнопками."
        )
        return
    await send_hero_card(message, hero_id)


async def send_hero_card(message: Message, hero_id: int):
    wait_msg = await message.answer("⏳ Собираю картинку закупки...")
    caption = format_hero_caption(hero_id)
    image_bytes = await build_purchase_image(hero_id)
    await wait_msg.delete()

    if image_bytes is None:
        await message.answer(caption + "\n\nНе удалось собрать картинку закупки — попробуй позже.", parse_mode="HTML")
        return

    photo = BufferedInputFile(image_bytes, filename=f"hero_{hero_id}_build.png")
    await message.answer_photo(
        photo=photo,
        caption=caption,
        parse_mode="HTML",
        reply_markup=hero_card_keyboard(hero_id),
    )


@dp.callback_query(F.data == "menu:positions")
async def cb_menu_positions(callback: CallbackQuery):
    await callback.message.edit_text(
        format_meta_menu(),
        parse_mode="HTML",
        reply_markup=position_meta_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("posmeta:"))
async def cb_position_meta(callback: CallbackQuery):
    position = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        format_position_meta(position),
        parse_mode="HTML",
        reply_markup=position_back_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("hero:"))
async def cb_hero(callback: CallbackQuery):
    hero_id = int(callback.data.split(":", 1)[1])
    await callback.answer("Собираю картинку...")
    caption = format_hero_caption(hero_id)
    image_bytes = await build_purchase_image(hero_id)
    if image_bytes is None:
        await callback.message.answer(caption, parse_mode="HTML")
        return

    photo = BufferedInputFile(image_bytes, filename=f"hero_{hero_id}_build.png")

    # Если жали "Обновить" на уже открытой карточке — пробуем заменить картинку
    # на месте, иначе (первый выбор героя из списка) шлём новое сообщение.
    if callback.message.photo:
        try:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                reply_markup=hero_card_keyboard(hero_id),
            )
            return
        except Exception as e:
            log.warning("Не удалось обновить фото на месте: %s", e)

    await callback.message.answer_photo(
        photo=photo,
        caption=caption,
        parse_mode="HTML",
        reply_markup=hero_card_keyboard(hero_id),
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main():
    log.info("Загружаю данные из OpenDota...")
    await refresh_cache()
    asyncio.create_task(periodic_refresh())
    log.info("Бот запущен, жду сообщений...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
