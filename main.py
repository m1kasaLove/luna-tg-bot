import asyncio
import logging
import os

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

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None

# ===== AI CLIENT =====
openai_client = AsyncOpenAI(
    base_url="https://api.polza.ai/v1",
    api_key=POLZA_API_KEY,
    timeout=Timeout(30.0)
)

# ===== REDIS HELPERS =====
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
    key = f"msgs:{user_id}:{day_key}"
    val = await redis_client.get(key)
    return int(val) if val else 0

async def incr_today_messages(user_id: int) -> int:
    day_key = int(asyncio.get_event_loop().time() // 86400)
    key = f"msgs:{user_id}:{day_key}"
    new = await redis_client.incr(key)
    await redis_client.expire(key, 86400)
    return new

async def get_remaining_messages(user_id: int) -> int:
    today = await get_today_messages(user_id)
    return max(0, FREE_LIMIT - today)

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
    buffer = ""
    step = max(1, len(text) // 40)

    for i in range(0, len(text), step):
        buffer = text[:i]
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sent.message_id,
                text=buffer + "▌"
            )
        except:
            pass
        await asyncio.sleep(0.03)

    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=text
    )

# ===== МОНЕТИЗАЦИЯ =====
FREE_LIMIT = 25
PREMIUM_PRICE = 50  # Stars

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
        "✨ Опа! А ты серьёзно! ✨\n"
        "Теперь можем болтать сколько влезет — безлимит на 30 дней.\n"
        "Спасибо, ты меня приятно удивил 💕\n"
        "Продолжим?"
    )
    
    logging.info(f"Premium purchased by user {user_id}")

# ===== КОМАНДЫ =====
@dp.message(Command("start"))
async def start(m: types.Message):
    user_id = m.from_user.id
    premium_status = await get_premium(user_id)
    
    if premium_status:
        await m.answer("О, привет again ✨ У тебя же безлимит. Ну давай, пиши, я тут.")
    else:
        remaining = await get_remaining_messages(user_id)
        await m.answer(
            f"Привет! Я Луна 🌙\n\n"
            f"У тебя сегодня ещё {remaining} бесплатных сообщений.\n"
            f"Потом — /buy за 50 звезд на месяц безлимита.\n\n"
            f"Не стесняйся, спрашивай что хочешь 😊"
        )

@dp.message(Command("status"))
async def status_cmd(m: types.Message):
    user_id = m.from_user.id
    premium_status = await get_premium(user_id)
    
    if premium_status:
        await m.answer("У тебя безлимит, дружище. Пиши сколько влезет 😊")
    else:
        remaining = await get_remaining_messages(user_id)
        await m.answer(
            f"Сегодня осталось сообщений: {remaining}\n"
            f"Когда закончатся — нужен /buy"
        )

# ===== ОСНОВНОЙ ДИАЛОГ =====
FREE_LIMIT = 25
WARNING_THRESHOLD = 5  # за 5 сообщений до конца предупредим

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    user_id = m.from_user.id
    is_premium = await get_premium(user_id)
    
    # Проверка лимита
    if not is_premium:
        current = await get_today_messages(user_id)
        
        # Если лимит исчерпан
        if current >= FREE_LIMIT:
            await m.answer(
                f"🔔 Всё, лимит на сегодня {FREE_LIMIT} сообщений.\n"
                f"Хочешь ещё? /buy — и болтаем сколько влезет ✨"
            )
            return
        
        # Если осталось мало — предупреждаем в самом сообщении
        remaining = FREE_LIMIT - current
        if remaining <= WARNING_THRESHOLD:
            # Отправляем предупреждение отдельно, но не блокируем диалог
            await m.answer(
                f"🌙 Осторожно: у тебя осталось {remaining} сообщений на сегодня.\n"
                f"Потом — /buy ✨"
            )
        
        # Увеличиваем счётчик после предупреждения
        await incr_today_messages(user_id)

    # Эффект "печатает"
    await bot.send_chat_action(m.chat.id, "typing")

    try:
        # 🔥 НОВЫЙ ПРОМПТ — живой, с характером, иногда с вопросами
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
            {"role": "user", "content": m.text}
        ])

        await type_message(m, text)

    except Exception as e:
        logging.exception(f"HANDLER ERROR: {e}")
        await m.answer("Блин, что‑то я туплю. Напиши ещё раз, а?")

# ===== WEBHOOK =====
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

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app

# ===== RUN =====
if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
