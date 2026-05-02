import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from openai import AsyncOpenAI
from httpx import Timeout

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
POLZA_API_KEY = os.getenv("POLZA_API_KEY")

BASE_URL = os.getenv("BASE_URL", "https://luna-tg-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

# ===== AI CLIENT =====
openai_client = AsyncOpenAI(
    base_url="https://api.polza.ai/v1",
    api_key=POLZA_API_KEY,
    timeout=Timeout(30.0)
)

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

    return "Сейчас перегрузка, попробуй позже"

# ===== TYPING EFFECT =====
async def type_message(message: types.Message, text: str):
    """
    Реалистичный эффект печатания через editMessageText
    """

    if len(text) < 20:
        await message.answer(text)
        return

    sent = await message.answer("...")

    buffer = ""
    step = max(1, len(text) // 40)  # ~40 обновлений

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

# ===== HANDLERS =====
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Луна онлайн ✨")

@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return

    await bot.send_chat_action(m.chat.id, "typing")

    try:
        text = await ask_ai([
            {
                "role": "system",
                "content": "Ты — Луна. Отвечай спокойно, тепло и кратко."
            },
            {"role": "user", "content": m.text}
        ])

        await type_message(m, text)

    except Exception as e:
        logging.exception(f"HANDLER ERROR: {e}")
        await m.answer("Ошибка... попробуй ещё раз")

# ===== WEB =====
async def root(request):
    return web.Response(text="Luna bot is alive")

async def ping(request):
    return web.Response(text="OK")

async def on_startup(app):
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.session.close()

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
