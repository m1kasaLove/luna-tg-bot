import asyncio
import logging
import os
import json
import time
import atexit

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
import redis.asyncio as redis

# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
REDIS_URL = os.getenv("REDIS_URL")

BASE_URL = os.getenv("BASE_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

PORT = int(os.getenv("PORT", 10000))

SYSTEM_PROMPT = "Ты — Луна. Тёплая, короткая, мягкая ✨"
FREE_LIMIT = 20

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None
queue = asyncio.Queue(maxsize=100)

openai = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)

# ================= STATE =================
user_locks = {}
last_request = {}

MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "qwen/qwen-2.5-7b-instruct:free",
    "microsoft/phi-3-mini-128k-instruct:free"
]

# ================= REDIS =================
async def rget(k, default=None):
    try:
        v = await redis_client.get(k)
        return json.loads(v) if v else default
    except:
        return default

async def rset(k, v, ex=86400):
    try:
        await redis_client.set(k, json.dumps(v), ex=ex)
    except:
        pass

# ================= LIMITS =================
def anti_flood(uid):
    now = time.time()
    if now - last_request.get(uid, 0) < 2:
        return False
    last_request[uid] = now
    return True

# ================= USER =================
async def premium(uid):
    return await redis_client.get(f"premium:{uid}") == "1"

async def usage(uid):
    v = await redis_client.incr(f"u:{uid}")
    await redis_client.expire(f"u:{uid}", 86400)
    return v

# ================= HISTORY =================
async def history(uid):
    return await rget(f"h:{uid}", [
        {"role": "system", "content": SYSTEM_PROMPT}
    ])

async def save(uid, h):
    await rset(f"h:{uid}", h[-12:])

# ================= CIRCUIT + ROUTER =================
model_fail = {}
model_block = {}

def pick_model():
    now = time.time()
    for m in MODELS:
        if model_block.get(m, 0) < now:
            return m
    return MODELS[0]

def mark_fail(m):
    model_fail[m] = model_fail.get(m, 0) + 1
    if model_fail[m] >= 3:
        model_block[m] = time.time() + 60
        model_fail[m] = 0

# ================= LLM =================
async def call_llm(msgs):
    for _ in range(3):
        model = pick_model()
        try:
            r = await openai.chat.completions.create(
                model=model,
                messages=msgs,
                timeout=25
            )
            return r.choices[0].message.content
        except Exception as e:
            logging.warning(f"{model} fail: {e}")
            mark_fail(model)
            await asyncio.sleep(1)
    return None

# ================= WORKER (QUEUE) =================
async def worker():
    while True:
        job = await queue.get()
        uid = job["uid"]
        msg = job["msg"]
        wait = job["wait"]

        try:
            h = await history(uid)
            h.append({"role": "user", "content": msg.text})

            text = await call_llm(h)

            if not text:
                await wait.edit_text("Сейчас перегрузка ✨")
                continue

            await wait.edit_text(text)

            h.append({"role": "assistant", "content": text})
            await save(uid, h)

        except Exception as e:
            logging.error(e)

        queue.task_done()

# ================= HANDLERS =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Луна онлайн ✨")

@dp.message(Command("reset"))
async def reset(m: types.Message):
    await redis_client.delete(f"h:{m.from_user.id}")
    await m.answer("Очищено 🌙")

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    uid = m.from_user.id

    if not anti_flood(uid):
        return await m.answer("Слишком быстро ✨")

    if not await premium(uid):
        if await usage(uid) > FREE_LIMIT:
            return await m.answer("Лимит ✨ /buy")

    wait = await m.answer("🌙 думаю...")

    await queue.put({
        "uid": uid,
        "msg": m,
        "wait": wait
    })

# ================= WEBHOOK FIX =================
async def set_webhook_safe():
    for i in range(5):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(WEBHOOK_URL)
            logging.info(f"Webhook OK: {WEBHOOK_URL}")
            return
        except Exception as e:
            logging.warning(f"Webhook retry {i}: {e}")
            await asyncio.sleep(3)

# ================= STARTUP =================
async def on_startup(app):
    global redis_client

    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

    await set_webhook_safe()

    asyncio.create_task(worker())

    logging.info("Luna v4 started")

# ================= SHUTDOWN FIX =================
async def on_shutdown(app):
    try:
        await bot.delete_webhook()
    except:
        pass

    try:
        await bot.session.close()
    except:
        pass

    try:
        await redis_client.aclose()
    except:
        pass

# ================= SAFE EXIT =================
def safe_exit():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(bot.session.close())
    except:
        pass

atexit.register(safe_exit)

# ================= APP =================
def create_app():
    app = web.Application()

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app

if __name__ == "__main__":
    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=PORT
    )
