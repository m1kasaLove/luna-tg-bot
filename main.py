import asyncio
import logging
import os
import json
import random

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
import redis.asyncio as redis

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
REDIS_URL = os.getenv("REDIS_URL")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

BASE_URL = https://luna-tg-bot.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

FREE_LIMIT = 20
PRICE = 100

SYSTEM_PROMPT = "Ты — Луна. Тёплая, мягкая, коротко отвечаешь ✨"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
redis_client = None


# ===== REDIS =====
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


async def get_history(uid):
    return await rget(f"history:{uid}", [
        {"role": "system", "content": SYSTEM_PROMPT}
    ])


async def save_history(uid, h):
    await rset(f"history:{uid}", h, ex=86400)


async def is_premium(uid):
    return await redis_client.get(f"premium:{uid}") == "1"


async def incr_usage(uid):
    v = await redis_client.incr(f"usage:{uid}")
    await redis_client.expire(f"usage:{uid}", 86400)
    return v


# ===== COMMANDS =====
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Я Луна ✨")


@dp.message(Command("reset"))
async def reset(message: types.Message):
    await redis_client.delete(f"history:{message.from_user.id}")
    await message.answer("Я всё забыла 🌙")


@dp.message(Command("buy"))
async def buy(message: types.Message):
    prices = [LabeledPrice(label="Premium", amount=PRICE)]
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Луна",
        description="Безлимит",
        payload="premium",
        provider_token=PROVIDER_TOKEN,
        currency="XTR",
        prices=prices
    )


@dp.pre_checkout_query()
async def checkout(q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def success(message: types.Message):
    await redis_client.set(f"premium:{message.from_user.id}", "1")
    await message.answer("Теперь я всегда рядом ✨")


# ===== CHAT =====
@dp.message()
async def chat(message: types.Message):
    if not message.text:
        return

    uid = message.from_user.id

    if not await is_premium(uid):
        u = await incr_usage(uid)
        if u > FREE_LIMIT:
            return await message.answer("Лимит. /buy ✨")

    history = await get_history(uid)
    history.append({"role": "user", "content": message.text})

    try:
        wait = await message.answer("...")

        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY
        )

        resp = await client.chat.completions.create(
            model="meta-llama/llama-3.2-3b-instruct:free",
            messages=history,
            stream=False
        )

        text = resp.choices[0].message.content or "..."

        await wait.edit_text(text)

        history.append({"role": "assistant", "content": text})
        await save_history(uid, history)

    except Exception as e:
        logging.error(e)
        await message.answer("Ошибка. Попробуй позже ✨")


# ===== WEBHOOK APP =====
async def on_startup(app: web.Application):
    global redis_client

    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info(f"Webhook set: {WEBHOOK_URL}")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()


def create_app():
    app = web.Application()

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


# ===== RUN =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
