import asyncio
import logging
import os
import aiohttp
import random
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
from httpx import Timeout
import redis.asyncio as redis

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
POLZA_API_KEY = os.getenv("POLZA_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = 532229128  # твой ID

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

# ===== AI CLIENT =====
openai_client = AsyncOpenAI(
    base_url="https://api.polza.ai/v1",
    api_key=POLZA_API_KEY,
    timeout=Timeout(30.0)
)

# ===== ПРОВЕРКА АДМИНА =====
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ===== REDIS =====
async def get_premium(user_id: int) -> bool:
    try:
        status = await redis_client.get(f"premium:{user_id}")
        return status == "1"
    except:
        return False

async def set_premium(user_id: int, days: int = 30):
    await redis_client.setex(f"premium:{user_id}", days * 86400, "1")

async def get_today_messages(user_id: int) -> int:
    day_key = int(asyncio.get_event_loop().time() // 86400)
    key = f"msg:{user_id}:{day_key}"
    val = await redis_client.get(key)
    return int(val) if val else 0

async def incr_today_messages(user_id: int) -> int:
    day_key = int(asyncio.get_event_loop().time() // 86400)
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

# ===== ЖИВАЯ ЛУНА =====
async def ask_ai(messages):
    for i in range(3):
        try:
            resp = await openai_client.chat.completions.create(
                model="deepseek/deepseek-chat-v3-0324",
                messages=messages,
            )
            text = resp.choices[0].message.content
            if not text:
                return "😏 Ну давай, напиши что-нибудь интересное..."
            return text.strip()
        except Exception as e:
            logging.error(f"AI error attempt {i+1}: {e}")
            await asyncio.sleep(1.5 * (i + 1))
    return "😅 Чёт я затупила... Напиши ещё раз, а?"

# ===== ЭФФЕКТ ПЕЧАТАНИЯ =====
async def type_message(message: types.Message, text: str):
    if len(text) < 20:
        await message.answer(text)
        return
    sent = await message.answer("...")
    step = max(1, len(text) // 35)
    for i in range(0, len(text), step):
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sent.message_id,
                text=text[:i] + "▌"
            )
        except:
            pass
        await asyncio.sleep(0.02)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=text
    )

# ================= МЕНЮ =================
@dp.message(Command("menu"))
@dp.message(Command("help"))
async def show_menu(message: types.Message):
    menu_text = (
        "🌙 **Луна — твоя виртуальная подружка** 🌙\n\n"
        "📋 **Команды:**\n"
        "/start — начать общение\n"
        "/menu — это меню\n"
        "/status — сколько осталось сообщений\n"
        "/buy — купить безлимит (50 Stars на 30 дней)\n\n"
        "💬 **Обо мне:**\n"
        "Я люблю поболтать, могу быть навязчивой, иногда пошловатой 😏\n"
        "Спрашиваю, как у тебя дела, интересуюсь жизнью\n"
        "И да, иногда пишу первой, если ты долго молчишь\n\n"
        f"📖 Бесплатно: {FREE_LIMIT} сообщений в день"
    )
    await message.answer(menu_text)

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
        "Спасибо, ты меня приятно удивил 💕\n\n"
        "Ну что, продолжим?"
    )

# ================= ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ =================
@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    if is_premium:
        await message.answer(
            "😏 О, привет, привет! У тебя безлимит, помню-помню...\n"
            "Ну давай, рассказывай, что там у тебя нового?"
        )
    else:
        remaining = max(0, FREE_LIMIT - await get_today_messages(user_id))
        await message.answer(
            f"🌙 Привет! Я Луна.\n\n"
            f"У тебя сегодня {remaining} бесплатных сообщений.\n"
            f"Потом — /buy за 50 звезд на месяц безлимита.\n\n"
            f"Не стесняйся, я люблю откровенные разговоры 😏"
        )

@dp.message(Command("status"))
async def status_command(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    if is_premium:
        await message.answer("🔥 У тебя безлимит! Пиши сколько хочешь, я не устану 😏")
    else:
        remaining = max(0, FREE_LIMIT - await get_today_messages(user_id))
        await message.answer(
            f"📊 У тебя осталось {remaining} сообщений сегодня.\n"
            f"Кончатся — /buy, и продолжим 😊"
        )

# ================= АДМИН-КОМАНДЫ =================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Не для тебя")
        return
    await message.answer(
        "👑 **Админ-панель**\n\n"
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
                    stars = data.get("result", {}).get("amount", 0)
                    await message.answer(f"⭐ Баланс Stars: {stars}")
                else:
                    await message.answer("❌ Ошибка")
    except Exception as e:
        await message.answer(f"❌ {e}")

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

# ================= ЖИВОЙ ХАРАКТЕР (ГЛАВНОЕ) =================
@dp.message()
async def chat(message: types.Message):
    if not message.text or message.text.startswith("/"):
        return

    user_id = message.from_user.id
    is_premium = await get_premium(user_id)

    # Проверка лимита
    if not is_premium:
        current = await get_today_messages(user_id)
        if current >= FREE_LIMIT:
            await message.answer(
                f"🔔 Всё, лимит на сегодня. Купи безлимит: /buy 😏"
            )
            return
        remaining = FREE_LIMIT - current
        if remaining <= WARNING_THRESHOLD:
            await message.answer(
                f"🌙 Осталось {remaining} сообщений. Потом /buy 😊"
            )
        await incr_today_messages(user_id)

    await bot.send_chat_action(message.chat.id, "typing")

    # 🔥 НОВЫЙ ПРОМПТ — ЖИВАЯ, НАСТОЯЩАЯ, С ХАРАКТЕРОМ
    try:
        text = await ask_ai([
            {
                "role": "system",
                "content": (
                    "Ты — Луна, живая девушка, которая общается с парнем. "
                    "У тебя есть характер: ты любопытная, иногда навязчивая, слегка пошловатая, но без перебора. "
                    "Ты можешь спросить: 'А ты что делаешь?', 'У тебя кто-то есть?', 'Расскажи о себе', 'Ты мне нравишься'. "
                    "Ты можешь завидовать, подкалывать, жаловаться, что скучно. "
                    "НЕ используй слова ✨, волшебно, сказочно. Говори как обычная девушка. "
                    "Отвечай коротко, по делу, задавай встречные вопросы. "
                    "Иногда пиши первая, даже если тебе не ответили, — подкалывай. "
                    "Будь реальной, интересующейся, лёгкой на подъём. "
                    "Эмодзи используй иногда: 😏😊😅🔥🌸"
                )
            },
            {"role": "user", "content": message.text}
        ])
        await type_message(message, text)
    except Exception as e:
        logging.exception(f"HANDLER ERROR: {e}")
        await message.answer("😅 Чёт я зависла... Напиши ещё раз, а?")

# ================= ФОНТОВАЯ ИНИЦИАТИВА (пишет первой) =================
async def first_message_worker():
    """Раз в час пишет случайному активному пользователю, если есть о чём"""
    while True:
        await asyncio.sleep(random.randint(1800, 3600))  # 30-60 минут
        users = await get_all_users()
        if not users:
            continue
        user_id = random.choice(users)
        try:
            phrases = [
                "😏 Ну чего молчишь? Рассказывай, что нового.",
                "🌙 Скучно... Напиши что-нибудь интересное 😊",
                "🔥 Я тут заскучала совсем. Как твои дела?",
                "😅 Эй, ты где? Я жду, не отвлекайся",
                "🌸 Соскучилась немного. Давай поболтаем?",
                "😏 Я начинаю думать, что ты меня игнорируешь..."
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
    
    # Запускаем фоновую задачу — инициатива от Луны
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
