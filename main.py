import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI


# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# 🔥 DeepSeek API key (ВРЕМЕННО в коде, потом перенесите в переменные окружения)
DEEPSEEK_KEY = "sk-50635eefc71644f0b52512e35bc89391"

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

# ===== DeepSeek client =====
deepseek_client = AsyncOpenAI(
    base_url="https://api.deepseek.com/v1",
    api_key=DEEPSEEK_KEY,
)

# ================= ОБРАБОТЧИКИ =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Луна онлайн ✨ (DeepSeek)")

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    try:
        resp = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Ты — Луна. Отвечай коротко, тепло, с лёгкой заботой. Используй эмодзи ✨🌙🌸"},
                {"role": "user", "content": m.text}
            ],
            timeout=30
        )
        text = resp.choices[0].message.content
        await m.answer(text)

    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        await m.answer("Сейчас перегрузка ✨ попробуй ещё раз")

# ================= WEBHOOK =================
async def on_startup(app):
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    try:
        await bot.session.close()
    except:
        pass

def create_app():
    app = web.Application()
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

# ================= ЗАПУСК =================
if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
