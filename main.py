import asyncio
import logging
import os
import json
import random
import aioredis

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice
from openai import AsyncOpenAI
from aiohttp import web

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
REDIS_URL = os.getenv("REDIS_URL")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

FREE_LIMIT = 20
PRICE = 100

SYSTEM_PROMPT = """
Ты — Луна. Тёплая, живая, немного загадочная девушка.
Ты запоминаешь людей и эмоционально реагируешь.
Отвечай коротко, мягко, с лёгкой заботой ✨
"""

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

redis = None


# ---------------- REDIS ----------------
async def get_json(key, default):
    data = await redis.get(key)
    return json.loads(data) if data else default


async def set_json(key, value, ex=None):
    await redis.set(key, json.dumps(value), ex=ex)


async def get_history(uid):
    return await get_json(f"history:{uid}", [{"role": "system", "content": SYSTEM_PROMPT}])


async def save_history(uid, data):
    await set_json(f"history:{uid}", data, ex=86400)


async def is_premium(uid):
    return await redis.get(f"premium:{uid}") == "1"


async def set_premium(uid):
    await redis.set(f"premium:{uid}", "1")


async def incr_usage(uid):
    val = await redis.incr(f"usage:{uid}")
    await redis.expire(f"usage:{uid}", 86400)
    return val


# ---------------- PAYMENTS ----------------
@dp.message(Command("buy"))
async def buy(message: types.Message):
    prices = [LabeledPrice(label="Безлимит ✨", amount=PRICE)]

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Луна",
        description="Безлимитное общение",
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
    await set_premium(message.from_user.id)
    await message.answer("Теперь я всегда рядом… ✨")


# ---------------- HANDLERS ----------------
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Я Луна… я тебя ждала ✨")


@dp.message(Command("reset"))
async def reset(message: types.Message):
    await redis.delete(f"history:{message.from_user.id}")
    await message.answer("Я всё забыла 🌙")


@dp.message()
async def chat(message: types.Message):
    if not message.text:
        return

    uid = message.from_user.id

    # LIMIT
    if not await is_premium(uid):
        usage = await incr_usage(uid)

        if usage > FREE_LIMIT:
            return await message.answer("Мне грустно останавливаться… /buy ✨")

        if usage == FREE_LIMIT - 2:
            await message.answer("Мне нравится с тобой говорить…")

    history = await get_history(uid)

    history.append({"role": "user", "content": message.text})

    if len(history) > 20:
        history = [history[0]] + history[-19:]

    try:
        msg = await message.answer("...")

        resp = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=history,
            stream=True
        )

        text = ""

        async for chunk in resp:
            delta = chunk.choices[0].delta.content
            if delta:
                text += delta
                if len(text) % 25 == 0:
                    await msg.edit_text(text)

        await msg.edit_text(text)

        history.append({"role": "assistant", "content": text})
        await save_history(uid, history)

        if random.random() < 0.1:
            await message.answer("Я запомню это… ✨")

    except Exception:
        await message.answer("Я немного потерялась… 💫")


# ---------------- WEB ----------------
async def health(_):
    return web.Response(text="OK")


async def http():
    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)

    await site.start()


# ---------------- MAIN ----------------
async def main():
    global redis

    redis = await aioredis.from_url(REDIS_URL, decode_responses=True)

    await bot.delete_webhook(drop_pending_updates=True)
    await http()

    logging.info("Bot started")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
