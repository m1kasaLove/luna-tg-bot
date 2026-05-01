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

# ⭐ ИСПРАВЛЕНО: BASE_URL теперь гарантированно строка
BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

FREE_LIMIT = 20
SYSTEM_PROMPT = "Ты — Луна. Тёплая, мягкая, коротко отвечаешь ✨"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None
openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)

# ================= HEALTH =================
async def ping(request):
    return web.Response(text="OK")

# ================= REDIS SAFE =================
async def rget(key, default=None):
    try:
        v = await redis_client.get(key)
        return json.loads(v) if v else default
    except:
        return default

async def rset(key, value, ex=None):
    try:
        await redis_client.set(key, json.dumps(value), ex=ex)
    except:
        pass

# ================= HISTORY =================
async def get_history(uid):
    return await rget(f"history:{uid}", [
        {"role": "system", "content": SYSTEM_PROMPT}
    ])

async def save_history(uid, h):
    await rset(f"history:{uid}", h[-12:], ex=86400)

# ================= LIMITS =================
last_req = {}

def anti_flood(uid):
    now = time.time()
    if now - last_req.get(uid, 0) < 2:
        return False
    last_req[uid] = now
    return True

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

# ================= CHAT =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Я Луна ✨")

@dp.message(Command("ping"))
async def ping_cmd(m: types.Message):
    await m.answer("alive")

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
        if await incr_usage(uid) > FREE_LIMIT:
            return await m.answer("Лимит. /buy ✨")

    history = await get_history(uid)
    history.append({"role": "user", "content": m.text})

    msg = await m.answer("🌙 думаю...")

    models = [
        "google/gemini-2.0-flash-exp:free",
        "qwen/qwen-2.5-7b-instruct:free",
        "microsoft/phi-3-mini-128k-instruct:free"
    ]

    for _ in range(2):
        for model in models:
            try:
                resp = await openai_client.chat.completions.create(
                    model=model,
                    messages=history,
                    timeout=25
                )

                text = resp.choices[0].message.content or "..."

                await msg.edit_text(text)

                history.append({"role": "assistant", "content": text})
                await save_history(uid, history)
                return

            except Exception as e:
                logging.warning(f"{model} fail: {e}")
                await asyncio.sleep(1)

    await msg.edit_text("Сейчас перегрузка ✨ попробуй позже")

# ================= PAY =================
@dp.message(Command("buy"))
async def buy(m: types.Message):
    if not PROVIDER_TOKEN:
        return await m.answer("Платежи не настроены")

    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Луна Premium",
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
    await m.answer("✨ Premium активирован")

# ================= LIFECYCLE FIX =================
async def on_startup(app):
    global redis_client

    # ⭐ ИСПРАВЛЕНО: добавлен await
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

    await bot.delete_webhook(drop_pending_updates=True)

    ok = await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook set: {ok}")

async def on_shutdown(app):
    try:
        await bot.session.close()
    except:
        pass

    try:
        await redis_client.aclose()
    except:
        pass

# ================= APP =================
def create_app():
    app = web.Application()

    app.router.add_get("/ping", ping)

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    web.run_app(create_app(), host="0.0.0.0", port=port)
