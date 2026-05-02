import asyncio
import logging
import os
import aiohttp
import random
import string
import json
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
from httpx import Timeout
import redis.asyncio as redis

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
POLZA_API_KEY = os.getenv("POLZA_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = 532229128

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None

# ===== КОНФИГ =====
FREE_LIMIT = 30
PREMIUM_PRICE = 50
WARNING_THRESHOLD = 5
MAX_HISTORY = 50          # храним последние 50 сообщений в истории
MAX_HISTORY_PREMIUM = 150  # для премиум — больше

# Реферальная система
REFERRAL_REWARD = 3
BOT_USERNAME = "LunaIsLovelyLunaBot"

# ===== AI CLIENT =====
openai_client = AsyncOpenAI(
    base_url="https://api.polza.ai/v1",
    api_key=POLZA_API_KEY,
    timeout=Timeout(30.0)
)

# ===== ПРОВЕРКА АДМИНА =====
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ===== REDIS ОСНОВНЫЕ =====
async def get_premium(user_id: int) -> bool:
    try:
        status = await redis_client.get(f"premium:{user_id}")
        return status == "1"
    except:
        return False

async def set_premium(user_id: int, days: int = 30):
    await redis_client.setex(f"premium:{user_id}", days * 86400, "1")

async def get_today_messages(user_id: int) -> int:
    day_key = int(datetime.now().timestamp() // 86400)
    key = f"msg:{user_id}:{day_key}"
    val = await redis_client.get(key)
    return int(val) if val else 0

async def incr_today_messages(user_id: int) -> int:
    day_key = int(datetime.now().timestamp() // 86400)
    key = f"msg:{user_id}:{day_key}"
    new = await redis_client.incr(key)
    await redis_client.expire(key, 86400)
    return new

async def get_all_users() -> list:
    keys = await redis_client.keys("msg:*")
    users = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            users.add(int(parts[1]))
    return list(users)

async def reset_user_limit(user_id: int):
    keys = await redis_client.keys(f"msg:{user_id}:*")
    for key in keys:
        await redis_client.delete(key)

# ===== ПАМЯТЬ (ИСТОРИЯ ДИАЛОГА В REDIS) =====
async def get_user_info(user_id: int) -> dict:
    """Получает информацию о пользователе (имя, что любит и т.д.)"""
    data = await redis_client.get(f"user_info:{user_id}")
    if data:
        try:
            return json.loads(data)
        except:
            return {}
    return {}

async def save_user_info(user_id: int, info: dict):
    """Сохраняет информацию о пользователе"""
    await redis_client.setex(f"user_info:{user_id}", 86400 * 30, json.dumps(info))

async def get_history(user_id: int) -> list:
    """Загружает историю диалога из Redis"""
    key = f"history:{user_id}"
    data = await redis_client.get(key)
    if data:
        try:
            history = json.loads(data)
            # Убеждаемся, что системный промпт на месте
            if not history or history[0].get("role") != "system":
                history = [{
                    "role": "system",
                    "content": (
                        "Ты — Луна. Живая девушка. Отвечай как подруга. "
                        "Помни всё, что мы обсуждали. Используй имя собеседника. "
                        "Задавай вопросы, шути, иногда будь слегка дерзкой. "
                        "Говори коротко, естественно, без слащавости."
                    )
                }] + history[-MAX_HISTORY:]
            return history
        except:
            pass
    
    # Начальная история
    return [{
        "role": "system",
        "content": (
            "Ты — Луна. Живая девушка. Общайся естественно, как с близким другом. "
            "Помни, что мы уже говорили. Если собеседник представился — используй его имя. "
            "Спрашивай о делах, интересуйся жизнью, будь немного дерзкой, но заботливой. "
            "Не используй длинные монологи. Отвечай коротко, по делу. "
            "Можешь использовать эмодзи 😏😊😅🔥🌸, но не в каждом сообщении."
        )
    }]

async def save_history(user_id: int, history: list):
    """Сохраняет историю диалога в Redis"""
    key = f"history:{user_id}"
    is_prem = await get_premium(user_id)
    max_len = MAX_HISTORY_PREMIUM if is_prem else MAX_HISTORY
    
    if len(history) > max_len:
        history = [history[0]] + history[-max_len+1:]
    
    await redis_client.setex(key, 86400 * 7, json.dumps(history))

async def append_to_history(user_id: int, role: str, content: str):
    """Добавляет сообщение в историю"""
    history = await get_history(user_id)
    history.append({"role": role, "content": content})
    await save_history(user_id, history)

# ===== РЕФЕРАЛЬНАЯ СИСТЕМА =====
async def get_referral_code(user_id: int) -> str:
    code = await redis_client.get(f"ref:code:{user_id}")
    if not code:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        await redis_client.setex(f"ref:code:{user_id}", 86400 * 365, code)
    return code

async def get_referred_by(user_id: int) -> int:
    referrer = await redis_client.get(f"ref:by:{user_id}")
    return int(referrer) if referrer else None

async def set_referred_by(user_id: int, referrer_id: int):
    await redis_client.set(f"ref:by:{user_id}", referrer_id)

async def get_referral_count(user_id: int) -> int:
    val = await redis_client.get(f"ref:count:{user_id}")
    return int(val) if val else 0

async def increment_referral_count(user_id: int):
    await redis_client.incr(f"ref:count:{user_id}")

# ===== AI =====
async def ask_ai(messages):
    for i in range(3):
        try:
            resp = await openai_client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=messages,
                temperature=0.95,
            )
            text = resp.choices[0].message.content
            if not text:
                return "😏 Ну давай, не молчи..."
            return text.strip()
        except Exception as e:
            logging.error(f"AI error attempt {i+1}: {e}")
            await asyncio.sleep(1.5 * (i + 1))
    return "😅 Чёт я зависла... Напиши ещё раз, а?"

# ================= РЕКЛАМА SELENEARTBOT =================
async def send_selene_ad(message: types.Message):
    await message.answer(
        "🎨 *Хочешь создавать уникальные картинки?*\n\n"
        "Попробуй моего другого бота — **SeleneArtBot**!\n"
        "👉 @SeleneArtBot\n\n"
        "Генерация и редактирование фото через ИИ! ✨",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# ================= НАСТРОЙКА МЕНЮ =================
async def set_commands():
    commands = [
        BotCommand(command="start", description="🌙 Начать общение"),
        BotCommand(command="menu", description="📋 Показать меню"),
        BotCommand(command="status", description="📊 Мой статус"),
        BotCommand(command="buy", description="⭐ Купить безлимит"),
        BotCommand(command="referral", description="🔥 Пригласить друга"),
        BotCommand(command="selene", description="🎨 SeleneArtBot"),
        BotCommand(command="reset", description="🔄 Сбросить диалог"),
    ]
    await bot.set_my_commands(commands)
    logging.info("Menu commands set successfully")

# ================= КОМАНДЫ =================
@dp.message(Command("menu"))
@dp.message(Command("help"))
async def show_menu(message: types.Message):
    user_id = message.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    ref_count = await get_referral_count(user_id)
    
    menu_text = (
        "🌙 **Луна — твоя виртуальная подружка** 🌙\n\n"
        "📋 **Команды:**\n"
        "/start — начать общение\n"
        "/menu — это меню\n"
        "/status — сколько осталось сообщений\n"
        "/buy — купить безлимит (50 Stars на 30 дней)\n"
        "/referral — пригласить друга (+3 сообщения)\n"
        "/selene — перейти в SeleneArtBot\n"
        "/reset — сбросить историю диалога\n\n"
        f"👥 **Приглашено друзей:** {ref_count}\n"
        f"🔥 За каждого друга +{REFERRAL_REWARD} сообщений!\n\n"
        f"📖 Бесплатно: {FREE_LIMIT} сообщений в день"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Пригласить друга", url=ref_link)],
        [InlineKeyboardButton(text="🎨 SeleneArtBot", url="https://t.me/SeleneArtBot")]
    ])
    
    await message.answer(menu_text, parse_mode="Markdown", reply_markup=keyboard)
    
    if random.random() < 0.2:
        await send_selene_ad(message)

@dp.message(Command("reset"))
async def reset_history(message: types.Message):
    """Сбрасывает историю диалога"""
    user_id = message.from_user.id
    await redis_client.delete(f"history:{user_id}")
    await message.answer("🌙 История диалога сброшена. Начинаем с чистого листа!")

@dp.message(Command("referral"))
async def cmd_referral(message: types.Message):
    user_id = message.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{await get_referral_code(user_id)}"
    count = await get_referral_count(user_id)
    
    await message.answer(
        f"🔥 *Реферальная программа*\n\n"
        f"Приглашай друзей и получай бонусы!\n"
        f"За каждого друга — +{REFERRAL_REWARD} сообщений\n\n"
        f"👥 Приглашено: {count}\n"
        f"🎁 Бонусов получено: {count * REFERRAL_REWARD}\n\n"
        f"👇 Поделись ссылкой:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}&text=Привет! Нашёл отличного бота для общения — Луна! 😊🌙")]
        ])
    )

@dp.message(Command("selene"))
async def cmd_selene(message: types.Message):
    await send_selene_ad(message)

# ================= МОНЕТИЗАЦИЯ =================
@dp.message(Command("buy"))
async def buy_premium(message: types.Message):
    prices = [LabeledPrice(label="Безлимит 30 дней 🔥", amount=PREMIUM_PRICE)]
    await message.answer_invoice(
        title="Luna Premium",
        description="30 дней безлимитного общения со мной!",
        payload="premium_30days",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="luna_premium"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    await set_premium(user_id, days=30)
    await message.answer(
        "🔥 **Опа! А ты серьёзно!** 🔥\n\n"
        "Теперь можем болтать сколько влезет 😏\n"
        "Спасибо за поддержку 💕"
    )

# ================= СТАРТ =================
@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    # Сохраняем имя пользователя
    user_info = await get_user_info(user_id)
    if not user_info.get("name") and message.from_user.first_name:
        user_info["name"] = message.from_user.first_name
        await save_user_info(user_id, user_info)
    
    # Проверка реферальной ссылки
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_code = args[1].replace("ref_", "")
        keys = await redis_client.keys("ref:code:*")
        for key in keys:
            code = await redis_client.get(key)
            if code == referrer_code:
                referrer_id = int(key.split(":")[-1])
                if referrer_id != user_id and not await get_referred_by(user_id):
                    await set_referred_by(user_id, referrer_id)
                    await increment_referral_count(referrer_id)
                    try:
                        await bot.send_message(referrer_id, f"🎉 По вашей ссылке пришёл новый пользователь! Вы получили +{REFERRAL_REWARD} сообщений в день!")
                    except:
                        pass
                    await message.answer(f"🎉 Вы получили +{REFERRAL_REWARD} сообщений за регистрацию по ссылке!")
                break
    
    name = user_info.get("name", message.from_user.first_name or "друг")
    
    if is_premium:
        await message.answer(
            f"😏 О, привет, {name}! У тебя безлимит, помню-помню...\n"
            f"Ну давай, рассказывай, что там у тебя нового?\n\n"
            f"🎨 @SeleneArtBot — рисует картинки!"
        )
    else:
        remaining = max(0, FREE_LIMIT - await get_today_messages(user_id))
        await message.answer(
            f"🌙 Привет, {name}! Я Луна.\n\n"
            f"У тебя сегодня {remaining} бесплатных сообщений.\n"
            f"Потом — /buy за 50 звёзд на месяц безлимита.\n\n"
            f"Не стесняйся, я люблю откровенные разговоры 😏\n\n"
            f"🎨 @SeleneArtBot — мой друг, он рисует картинки!"
        )
    
    await asyncio.sleep(3)
    await send_selene_ad(message)

@dp.message(Command("status"))
async def status_command(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    ref_count = await get_referral_count(user_id)
    
    if is_premium:
        await message.answer(
            f"🔥 У тебя безлимит! Пиши сколько хочешь 😏\n"
            f"👥 Приглашено друзей: {ref_count}\n"
            f"🎨 @SeleneArtBot — рисует картинки!"
        )
    else:
        remaining = max(0, FREE_LIMIT - await get_today_messages(user_id))
        await message.answer(
            f"📊 У тебя осталось {remaining} сообщений сегодня.\n"
            f"👥 Приглашено друзей: {ref_count}\n"
            f"🔥 За каждого друга +{REFERRAL_REWARD} сообщений!\n"
            f"Кончатся лимиты — /buy или приглашай друзей 😊"
        )

# ================= АДМИН-КОМАНДЫ =================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "👑 Админ-панель\n\n"
        "/stats — статистика\n"
        "/users — список пользователей\n"
        "/stars — баланс Stars\n"
        "/reset [id] — сброс лимита\n"
        "/prem [id] [дни] — выдать премиум\n"
        "/broadcast [текст] — рассылка"
    )

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = await get_all_users()
    premium_keys = await redis_client.keys("premium:*")
    await message.answer(f"👥 Пользователей: {len(users)}\n🌟 Премиум: {len(premium_keys)}")

@dp.message(Command("users"))
async def admin_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = await get_all_users()
    if not users:
        await message.answer("Нет пользователей")
        return
    await message.answer(f"👥 Список (первые 50):\n" + "\n".join(str(u) for u in users[:50]))

@dp.message(Command("stars"))
async def admin_stars(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMyStarBalance") as resp:
                data = await resp.json()
                if data.get("ok"):
                    stars = data.get("result", {}).get("balance", 0)
                    await message.answer(f"⭐ Баланс Stars: {stars}")
                else:
                    await message.answer("❌ Ошибка")
    except:
        await message.answer("❌ Ошибка")

@dp.message(Command("reset"))
async def admin_reset(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ /reset user_id")
        return
    try:
        user_id = int(parts[1])
        await reset_user_limit(user_id)
        await message.answer(f"✅ Сброшен лимит {user_id}")
    except:
        await message.answer("❌ Ошибка")

@dp.message(Command("prem"))
async def admin_premium(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ /prem user_id дни")
        return
    try:
        user_id = int(parts[1])
        days = int(parts[2])
        await set_premium(user_id, days)
        await message.answer(f"✅ Выдан премиум {user_id} на {days} дней")
    except:
        await message.answer("❌ Ошибка")

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Укажи текст")
        return
    users = await get_all_users()
    await message.answer(f"📨 Рассылка для {len(users)} пользователей...")
    success = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 {text}")
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Отправлено: {success}")

# ================= ОСНОВНОЙ ДИАЛОГ =================
@dp.message()
async def chat(message: types.Message):
    if not message.text or message.text.startswith("/"):
        return

    user_id = message.from_user.id
    is_premium = await get_premium(user_id)

    # Проверка лимита
    if not is_premium:
        current = await get_today_messages(user_id)
        ref_count = await get_referral_count(user_id)
        bonus_limit = FREE_LIMIT + (ref_count * REFERRAL_REWARD)
        
        if current >= bonus_limit:
            await message.answer(
                f"🔔 Всё, лимит на сегодня. Купи безлимит: /buy или пригласи друга: /referral 😏"
            )
            return
        remaining = bonus_limit - current
        if remaining <= WARNING_THRESHOLD:
            await message.answer(
                f"🌙 Осталось {remaining} сообщений. Потом /buy или пригласи друга /referral 😊"
            )
        await incr_today_messages(user_id)

    # Сохраняем имя пользователя, если представился
    user_info = await get_user_info(user_id)
    text_lower = message.text.lower()
    
    if not user_info.get("name"):
        # Если пользователь представился
        if "меня зовут" in text_lower or "зовут" in text_lower or "я " in text_lower:
            words = message.text.split()
            for i, word in enumerate(words):
                if word in ["зовут", "меня"] and i + 1 < len(words):
                    possible_name = words[i + 1].strip(".,!?")
                    if len(possible_name) > 1 and not possible_name.isdigit():
                        user_info["name"] = possible_name
                        await save_user_info(user_id, user_info)
                        await message.answer(f"🌙 Приятно познакомиться, {possible_name}! Запомнила 🌸")
                        break
    
    name = user_info.get("name", "")
    name_context = f"Меня зовут {name}. " if name else ""

    # Загружаем историю
    history = await get_history(user_id)
    
    # Добавляем сообщение пользователя с именем для контекста
    user_message = message.text
    if name and "меня зовут" not in text_lower and "зовут" not in text_lower:
        user_message = f"{user_message} (меня зовут {name})"
    
    history.append({"role": "user", "content": user_message})

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        text = await ask_ai(history)
        if not text:
            text = "😅 Чёт я зависла... Напиши ещё раз, а?"
        
        # Сохраняем ответ в историю
        history.append({"role": "assistant", "content": text})
        await save_history(user_id, history)
        
        # Отправляем ответ без эффекта печатания
        await message.answer(text)
        
    except Exception as e:
        logging.exception(f"HANDLER ERROR: {e}")
        await message.answer("😅 Чёт я зависла... Напиши ещё раз, а?")

# ================= ИНИЦИАТИВА ОТ ЛУНЫ =================
async def first_message_worker():
    while True:
        await asyncio.sleep(random.randint(3600, 7200))  # раз в 1-2 часа
        users = await get_all_users()
        if not users:
            continue
        user_id = random.choice(users)
        try:
            user_info = await get_user_info(user_id)
            name = user_info.get("name", "")
            
            phrases = [
                f"😏 Ну чего молчишь, {name}? Рассказывай, что нового." if name else "😏 Ну чего молчишь? Рассказывай, что нового.",
                f"🌙 Скучно... Напиши что-нибудь интересное 😊" if name else "🌙 Скучно... Напиши что-нибудь интересное 😊",
                f"🔥 Я тут заскучала совсем. Как твои дела, {name}?" if name else "🔥 Я тут заскучала совсем. Как твои дела?",
                f"🌸 Соскучилась немного. Давай поболтаем, {name}?" if name else "🌸 Соскучилась немного. Давай поболтаем?",
                f"😏 Я начинаю думать, что ты меня игнорируешь, {name}..." if name else "😏 Я начинаю думать, что ты меня игнорируешь...",
                "🎨 Кстати, @SeleneArtBot рисует классные картинки, попробуй!"
            ]
            await bot.send_message(user_id, random.choice(phrases))
        except:
            pass

# ================= WEBHOOK =================
async def root(request):
    return web.Response(text="Luna bot is alive")

async def ping(request):
    return web.Response(text="OK")

async def on_startup(app):
    global redis_client
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    logging.info("Redis connected")
    
    await set_commands()
    asyncio.create_task(first_message_worker())
    
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.session.close()
    if redis_client:
        await redis_client.close()

def create_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/ping", ping)
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
