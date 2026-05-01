import asyncio
import logging
import os
import json
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
import redis.asyncio as redis


# ================= SAFE CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
REDIS_URL = os.getenv("REDIS_URL")

# 🔥 FIX: no crash if missing
BASE_URL = os.getenv("BASE_URL", "").strip()

PORT = int(os.getenv("PORT", 10000))

WEBHOOK_PATH = "/webhook"

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None

openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)


# ================= SAFE WEBHOOK =================
async def set_webhook_safe():
    if not BASE_URL:
        logging.warning("BASE_URL is missing → webhook NOT set (bot will still run)")
        return

    url = f"{BASE_URL}{WEBHOOK_PATH}"

    try:
        await bot.set_webhook(url, drop_pending_updates=True)
        logging.info(f"Webhook OK: {url}")
    except Exception as e:
        logging.error(f"Webhook failed: {e}")


# ================= STARTUP =================
async def on_startup(app):
    global redis_client

    if REDIS_URL:
        redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

    await set_webhook_safe()


async def on_shutdown(app):
    try:
        await bot.session.close()
    except:
        pass

    try:
        if redis_client:
            await redis_client.aclose()
    except:
        pass


# ================= SIMPLE HANDLER =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Луна онлайн ✨")


@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    try:
        resp = await openai_client.chat.completions.create(
            model="qwen/qwen-2.5-7b-instruct:free",
            messages=[
                {"role": "system", "content": "Ты Луна, короткие ответы"},
                {"role": "user", "content": m.text}
            ],
            timeout=20
        )

        text = resp.choices[0].message.content
        await m.answer(text)

    except Exception as e:
        logging.error(e)
        await m.answer("Сейчас перегрузка ✨")


# ================= APP =================
def create_app():
    app = web.Application()

    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


# ================= RUN =================
if __name__ == "__main__":
    app = create_app()

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
