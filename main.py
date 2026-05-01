import asyncio
import logging
import os
import json
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
import redis.asyncio as redis

# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
REDIS_URL = os.getenv("REDIS_URL")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

FREE_LIMIT = 20
SYSTEM_PROMPT = "Ты — Луна. Тёплая, мягкая, коротко отвечаешь ✨"

MAX_HISTORY = 12
ANTI_FLOOD_SECONDS = 2

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None
openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)

# ================= STATE =================
user_locks = {}
last_request_time = {}

# ================= REDIS SAFE =================
async def rget(key, default=None):
    try:
        v = await redis_client.get(key)
        return json.loads(v) if v else default
    except:
        return default

async def rset(key, value, ex=86400):
    try:
        await redis_client.set(key, json.dumps(value), ex=ex)
    except:
        pass

# ================= SAFETY =================
def anti_flood(uid):
    now = time.time()
    if now - last_request_time.get(uid, 0) < ANTI_FLOOD_SECONDS:
        return False
    last_request_time[uid] = now
    return True

def get_lock(uid):
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]

# ================= HISTORY =================
async def get_history(uid):
    return await rget(f"history:{uid}", [
        {"role": "system", "content": SYSTEM_PROMPT}
    ])

async def save_history(uid, history):
    await rset(f"history:{uid}", history[-MAX_HISTORY:])

# ================= LIMITS =================
async def is_premium(uid):
    try:
        return await redis_client.get(f"premium:{uid}") == "1"
    except:
        return False

async def incr_usage(uid):
    try:
        v = await redis_client.incr(f"usage:{uid}")
        await redis_client.expire(f"usage:{uid}", 86400)
        return v
    except:
        return 0

# ================= OPENROUTER SAFE CALL =================
async def ask_llm(history):
    models = [
        "google/gemini-2.0-flash-exp:free",
        "qwen/qwen-2.5-7b-instruct:free",
        "microsoft/phi-3-mini-128k-instruct:free"
    ]

    for model in models:
        for attempt in range(3):
            try:
                resp = await openai_client.chat.completions.create(
                    model=model,
                    messages=history,
                    timeout=25
                )
                return resp.choices[0].message.content
            except Exception as e:
                wait = 2 ** attempt
                logging.warning(f"{model} fail ({attempt}): {e}")
                await asyncio.sleep(wait)

    return None

# ================= HANDLERS =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Я Луна ✨")

@dp.message(Command("reset"))
async def reset(m: types.Message):
    await redis_client.delete(f"history:{m.from_user.id}")
    await m.answer("Я всё забыла 🌙")

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    uid = m.from_user.id

    if not anti_flood(uid):
        return await m.answer("Слишком быстро ✨")

    if not await is_premium(uid):
        usage = await incr_usage(uid)
        if usage > FREE_LIMIT:
            return await m.answer("Лимит исчерпан /buy ✨")

    lock = get_lock(uid)

    async with lock:
        history = await get_history(uid)
        history.append({"role": "user", "content": m.text})
        history = history[-MAX_HISTORY:]

        wait_msg = await m.answer("🌙 думаю...")

        text = await ask_llm(history)

        if not text:
            await wait_msg.edit_text("Сейчас перегрузка. Попробуй позже ✨")
            return

        await wait_msg.edit_text(text)

        history.append({"role": "assistant", "content": text})
        await save_history(uid, history)

# ================= PAYMENT =================
@dp.message(Command("buy"))
async def buy(m: types.Message):
    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Луна",
        description="Безлимит",
        payload="premium",
        provider_token=PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=100)]
    )

@dp.pre_checkout_query()
async def checkout(q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def success(m: types.Message):
    await redis_client.set(f"premium:{m.from_user.id}", "1")
    await m.answer("Теперь я всегда рядом ✨")

# ================= LIFECYCLE =================
async def on_startup(app):
    global redis_client

    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info(f"Webhook OK: {WEBHOOK_URL}")

async def on_shutdown(app):
    try:
        await bot.delete_webhook()
    except:
        pass

    try:
        await redis_client.aclose()
    except:
        pass

# ================= APP =================
def create_app():
    app = web.Application()

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    web.run_app(create_app(), host="0.0.0.0", port=port)
