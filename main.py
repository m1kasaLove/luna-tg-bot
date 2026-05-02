import asyncio
import logging
import os
import aiohttp

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
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None

# ===== КОНФИГ =====
FREE_LIMIT = 25
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

# ===== REDIS HELPER =====
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

# ===== AI =====
async def ask_ai(messages):
    for i in range(3):
        try:
            resp = await openai_client.chat.completions.create(
                model="deepseek/deepseek-chat-v3-0324",
                messages=messages,
            )
            text = resp.choices[0].message.content
            if not text:
                return "..."
            return text.strip()
        except Exception as e:
            logging.error(f"AI error attempt {i+1}: {e}")
            await asyncio.sleep(1.5 * (i + 1))
    return "Мда... Что‑то я зависла. Напиши ещё раз, ок?"

# ===== TYPING EFFECT =====
async def type_message(message: types.Message, text: str):
    if len(text) < 20:
        await message.answer(text)
        return
    sent = await message.answer("...")
    step = max(1, len(text) // 40)
    for i in range(0, len(text), step):
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sent.message_id,
                text=text[:i] + "▌"
            )
        except:
            pass
        await asyncio.sleep(0.03)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=text
    )

# ================= МЕНЮ (ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ) =================
@dp.message(Command("menu"))
@dp.message(Command("help"))
async def show_menu(message: types.Message):
    menu_text = (
        "🌙 **Меню команд Луны** 🌙\n\n"
        "📋 **Основные команды:**\n"
        "/start — начать диалог со мной\n"
        "/menu или /help — показать это меню\n"
        "/status — узнать остаток бесплатных сообщений\n"
        "/buy — купить безлимит за 50 Stars (30 дней)\n\n"
        "💬 **Как общаться:**\n"
        "Просто пиши мне сообщения — я отвечаю как живой человек 😊\n\n"
        "✨ У меня есть характер: могу быть слегка дерзкой, кокетливой,\n"
        "задавать вопросы и поддерживать любой разговор.\n\n"
        f"📖 Бесплатно: {FREE_LIMIT} сообщений в день\n"
        f"⭐ Безлимит: {PREMIUM_PRICE} Stars на 30 дней\n\n"
        "💕 Спрашивай что хочешь, я всегда здесь!"
    )
    await message.answer(menu_text)

# ================= МОНЕТИЗАЦИЯ =================
@dp.message(Command("buy"))
async def buy_premium(message: types.Message):
    prices = [LabeledPrice(label="Безлимит на 30 дней ✨", amount=PREMIUM_PRICE)]
    await message.answer_invoice(
        title="Luna Premium",
        description="Безлимитное общение с Луной на 30 дней!\n⭐ 50 Telegram Stars",
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
        "✨ **Премиум активирован!** ✨\n\n"
        "Теперь ты можешь общаться со мной без ограничений 🌙\n"
        "Спасибо за поддержку! 💕\n\n"
        "Пиши что хочешь 😊"
    )

# ================= КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ =================
@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    if is_premium:
        await message.answer(
            "✨ Привет! У тебя активен безлимит, так что можем болтать сколько влезет 😊\n"
            "Напиши что-нибудь или отправь /menu"
        )
    else:
        remaining = max(0, FREE_LIMIT - await get_today_messages(user_id))
        await message.answer(
            f"🌙 Привет! Я Луна, приятно познакомиться 😊\n\n"
            f"У тебя сегодня ещё {remaining} бесплатных сообщений.\n"
            f"Потом можно купить безлимит: /buy\n\n"
            f"Отправь /menu, чтобы увидеть все команды."
        )

@dp.message(Command("status"))
async def status_command(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    if is_premium:
        await message.answer("✨ У тебя безлимит! Общайся сколько хочешь 😊")
    else:
        remaining = max(0, FREE_LIMIT - await get_today_messages(user_id))
        await message.answer(
            f"📊 Твой статус: бесплатный тариф\n"
            f"💬 Осталось сообщений сегодня: {remaining}/{FREE_LIMIT}\n\n"
            f"🌟 Купить безлимит: /buy"
        )

# ================= АДМИН-КОМАНДЫ =================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для админа")
        return
    
    admin_text = (
        "👑 **Админ-панель Луны** 👑\n\n"
        "📊 **Статистика:**\n"
        "/stats — общая статистика\n"
        "/users — список пользователей\n"
        "/stars — баланс Stars бота\n\n"
        "🔧 **Управление:**\n"
        "/reset [user_id] — сбросить лимит пользователю\n"
        "/prem [user_id] [дни] — выдать премиум\n"
        "/broadcast [текст] — рассылка всем\n\n"
        "💎 **Примеры:**\n"
        "/reset 123456789\n"
        "/prem 123456789 30\n"
        "/broadcast Привет всем!"
    )
    await message.answer(admin_text)

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        users = await get_all_users()
        premium_keys = await redis_client.keys("premium:*")
        
        stats_text = (
            f"📊 **Статистика бота**\n\n"
            f"👤 Уникальных пользователей: {len(users)}\n"
            f"🌟 Активных премиумов: {len(premium_keys)}\n"
            f"📅 Сегодня: {asyncio.get_event_loop().time() // 86400}"
        )
        await message.answer(stats_text)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("users"))
async def admin_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    users = await get_all_users()
    if not users:
        await message.answer("📭 Нет пользователей")
        return
    
    users_list = "\n".join(str(uid) for uid in users[:50])
    await message.answer(f"👥 **Пользователи бота**\nВсего: {len(users)}\n\n{users_list}")

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
                    await message.answer(f"⭐ **Баланс Stars бота:** {stars}")
                else:
                    await message.answer("❌ Не удалось получить баланс")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("reset"))
async def admin_reset(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /reset TelegramID")
        return
    
    try:
        user_id = int(parts[1])
        await reset_user_limit(user_id)
        await message.answer(f"✅ Сброшен лимит пользователю {user_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("prem"))
async def admin_premium(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Использование: /prem TelegramID дни")
        return
    
    try:
        user_id = int(parts[1])
        days = int(parts[2])
        await set_premium(user_id, days)
        await message.answer(f"✅ Выдан премиум на {days} дней пользователю {user_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Укажи текст рассылки после /broadcast")
        return
    
    users = await get_all_users()
    if not users:
        await message.answer("📭 Нет пользователей для рассылки")
        return
    
    success = 0
    fail = 0
    
    await message.answer(f"📨 Начинаю рассылку для {len(users)} пользователей...")
    
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 **Сообщение от Луны:**\n\n{text}")
            success += 1
            await asyncio.sleep(0.05)
        except:
            fail += 1
    
    await message.answer(f"✅ Рассылка завершена\n📨 Отправлено: {success}\n❌ Ошибок: {fail}")

# ================= ОСНОВНОЙ ДИАЛОГ =================
@dp.message()
async def chat(message: types.Message):
    if not message.text:
        return
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    is_premium = await get_premium(user_id)

    if not is_premium:
        current = await get_today_messages(user_id)
        if current >= FREE_LIMIT:
            await message.answer(
                f"🔔 Всё, лимит на сегодня {FREE_LIMIT} сообщений.\n"
                f"Хочешь ещё? /buy — и болтаем сколько влезет ✨"
            )
            return
        remaining = FREE_LIMIT - current
        if remaining <= WARNING_THRESHOLD:
            await message.answer(
                f"🌙 Осторожно: у тебя осталось {remaining} сообщений на сегодня.\n"
                f"Потом — /buy ✨"
            )
        await incr_today_messages(user_id)

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        text = await ask_ai([
            {
                "role": "system",
                "content": (
                    "Ты — Луна. Живая, молодая девушка. Общаешься просто и естественно, как с другом. "
                    "Можешь быть слегка дерзкой, иногда кокетливой, задавать встречные вопросы. "
                    "Не переигрывай — никаких ✨ и сказочных принцесс. "
                    "Иногда можешь спросить: «А у тебя как?», «Ну интересно же», «Расскажи подробнее». "
                    "Используй смайлики 😊😏🌸 но без фанатизма. "
                    "Отвечай тепло, но без слащавости."
                )
            },
            {"role": "user", "content": message.text}
        ])
        await type_message(message, text)
    except Exception as e:
        logging.exception(f"HANDLER ERROR: {e}")
        await message.answer("Блин, что‑то я туплю. Напиши ещё раз, а?")

# ================= WEBHOOK =================
async def root(request):
    return web.Response(text="Luna bot is alive")

async def ping(request):
    return web.Response(text="OK")

async def on_startup(app):
    global redis_client
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    logging.info("Redis connected")
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
