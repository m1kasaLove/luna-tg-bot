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
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY
)

# ================= МОДЕЛИ С АВТОПЕРЕКЛЮЧЕНИЕМ =================
# Сначала самые стабильные
MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "microsoft/phi-3-mini-128k-instruct:free",
    "deepseek/deepseek-r1:free",
    "qwen/qwen-2.5-7b-instruct:free",
]

# ================= ОБРАБОТЧИКИ =================
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Луна онлайн ✨")

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    # Пробуем все модели по очереди
    for model in MODELS:
        try:
            resp = await openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ты — Луна. Отвечай коротко, тепло, с лёгкой заботой. Используй эмодзи ✨🌙🌸"},
                    {"role": "user", "content": m.text}
                ],
                timeout=30
            )
            text = resp.choices[0].message.content
            await m.answer(text)
            return  # Успешно — выходим

        except Exception as e:
            logging.warning(f"Модель {model} не ответила: {e}")
            await asyncio.sleep(1)  # Пауза перед следующей моделью
            continue

    # Если все модели не сработали
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
