import asyncio
import logging
import os
import json
import time

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

SYSTEM_PROMPT = "Ты — Луна. Коротко, тепло ✨"
FREE_LIMIT = 20

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None
queue = asyncio.Queue()

openai = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)

# ================= CIRCUIT BREAKER =================
model_failures = {}
model_disabled_until = {}

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
async def incr(uid):
    v = await redis_client.incr(f"u:{uid}")
    await redis_client.expire(f"u:{uid}", 86400)
    return v

async def premium(uid):
    return await redis_client.get(f"p:{uid}") == "1"

# ================= HISTORY =================
async def history(uid):
    return await rget(f"h:{uid}", [{"role": "system", "content": SYSTEM_PROMPT}])

async def save(uid, h):
    await rset(f"h:{uid}", h[-10:])

# ================= MODEL ROUTER =================
def pick_model():
    now = time.time()

    for m in MODELS:
        if model_disabled_until.get(m, 0) < now:
            return m

    return MODELS[0]

def mark_fail(model):
    model_failures[model] = model_failures.get(model, 0) + 1

    if model_failures[model] >= 3:
        model_disabled_until[model] = time.time() + 60  # 1 min cooldown
        model_failures[model] = 0

# ================= LLM =================
async def call_llm(messages):
    for _ in range(3):
        model = pick_model()

        try:
            r = await openai.chat.completions.create(
                model=model,
                messages=messages,
                timeout=25
            )
            return r.choices[0].message.content

        except Exception as e:
            logging.warning(f"{model} failed: {e}")
            mark_fail(model)
            await asyncio.sleep(1)

    return None

# ================= WORKER QUEUE =================
async def worker():
    while True:
        ctx = await queue.get()

        uid = ctx["uid"]
        msg = ctx["msg"]
        wait = ctx["wait"]

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

        finally:
            queue.task_done()

# ================= HANDLER =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Луна онлайн ✨")

@dp.message(Command("reset"))
async def reset(m: types.Message):
    await redis_client.delete(f"h:{m.from_user.id}")
    await m.answer("Готово 🌙")

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    uid = m.from_user.id

    if not await premium(uid):
        if await incr(uid) > FREE_LIMIT:
            return await m.answer("Лимит ✨ /buy")

    wait = await m.answer("🌙 думаю...")

    await queue.put({
        "uid": uid,
        "msg": m,
        "wait": wait
    })

# ================= STARTUP =================
async def on_startup(app):
    global redis_client

    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

    asyncio.create_task(worker())

    logging.info("Luna v3 started")

# ================= SHUTDOWN =================
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

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    web.run_app(create_app(), host="0.0.0.0", port=port)
