import asyncio
import logging
import os
import aiohttp
from io import BytesIO

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import redis.asyncio as redis

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))  # твой Telegram ID

BASE_URL = os.getenv("BASE_URL", "https://image-gen-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

redis_client = None

# ===== КОНФИГ =====
FREE_GENERATIONS = 2
PRICE_PER_GENERATION = 10
PREMIUM_PRICE = 50

# ===== АДМИН-КОМАНДА (просмотр баланса Stars) =====
@dp.message(Command("stars_balance"))
async def stars_balance(message: types.Message):
    """Показывает баланс Stars бота (только админу)"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Только для админа")
        return
    
    try:
        # Получаем баланс через Telegram Bot API
        balance = await bot.get_chat_member_count("@stars_balance_check")  # обходной метод
        # Лучше: прямой запрос к API
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getStarBalance") as resp:
                data = await resp.json()
                if data.get("ok"):
                    stars = data.get("result", {}).get("balance", 0)
                    await message.answer(f"⭐ **Баланс Stars бота:** {stars}\n\n"
                                        f"💎 1 генерация = {PRICE_PER_GENERATION} Stars\n"
                                        f"🌟 Безлимит = {PREMIUM_PRICE} Stars")
                else:
                    await message.answer("❌ Не удалось получить баланс")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ===== REDIS =====
async def get_generations_today(user_id: int) -> int:
    day_key = int(asyncio.get_event_loop().time() // 86400)
    key = f"gen:{user_id}:{day_key}"
    val = await redis_client.get(key)
    return int(val) if val else 0

async def incr_generations_today(user_id: int) -> int:
    day_key = int(asyncio.get_event_loop().time() // 86400)
    key = f"gen:{user_id}:{day_key}"
    new = await redis_client.incr(key)
    await redis_client.expire(key, 86400)
    return new

async def get_premium(user_id: int) -> bool:
    try:
        status = await redis_client.get(f"premium_gen:{user_id}")
        return status == "1"
    except:
        return False

async def set_premium(user_id: int, days: int = 30):
    await redis_client.setex(f"premium_gen:{user_id}", days * 86400, "1")

# ===== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ =====
async def generate_image(prompt: str) -> BytesIO | None:
    url = f"https://image.pollinations.ai/prompt/{prompt}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    return BytesIO(img_data)
                else:
                    logging.error(f"Image API error: {resp.status}")
                    return None
        except Exception as e:
            logging.error(f"Generation error: {e}")
            return None

# ===== ПЛАТЕЖИ =====
@dp.message(Command("buy"))
async def buy_premium(message: types.Message):
    prices = [LabeledPrice(label="Безлимит 30 дней 🎨", amount=PREMIUM_PRICE)]
    
    await message.answer_invoice(
        title="🎨 Безлимитная генерация",
        description=f"30 дней безлимита за {PREMIUM_PRICE} Stars!\n\n✨ Все картинки бесплатно",
        payload="premium_30days",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy_premium"
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    # Логируем попытку оплаты
    logging.info(f"Pre-checkout: user {query.from_user.id}, payload {query.invoice_payload}")
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: SuccessfulPayment):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    if payload == "premium_30days":
        await set_premium(user_id, days=30)
        await message.answer(
            "✅ **Премиум активирован!**\n\n"
            "Теперь ты можешь генерировать картинки без ограничений!\n"
            "Просто напиши, что хочешь нарисовать 🎨"
        )
    elif payload.startswith("single_gen:"):
        prompt = payload.split(":", 1)[1]
        # Начинаем генерацию сразу после оплаты
        await bot.send_chat_action(message.chat.id, "upload_photo")
        wait_msg = await message.answer("🎨 Генерирую картинку...")
        
        img_bytes = await generate_image(prompt)
        if img_bytes:
            photo = BufferedInputFile(img_bytes.getvalue(), filename="image.jpg")
            await message.answer_photo(photo, caption=f"🎨 По запросу: _{prompt[:100]}_")
            await wait_msg.delete()
        else:
            await wait_msg.edit_text("❌ Не получилось сгенерировать, попробуй другой промпт")
    else:
        await message.answer("✅ Оплата прошла успешно! Спасибо за поддержку ✨")

# ===== КОМАНДЫ =====
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🎨 **Генератор картинок через ИИ**\n\n"
        "Просто отправь мне текст — я нарисую картинку!\n"
        "Например: `кот в космосе`\n\n"
        f"📊 Бесплатно: {FREE_GENERATIONS} картинок в день\n"
        f"⭐ 1 картинка = {PRICE_PER_GENERATION} Stars\n"
        f"🌟 Безлимит 30 дней = {PREMIUM_PRICE} Stars\n\n"
        "Купить безлимит: /buy\n"
        "Мой статус: /status"
    )

@dp.message(Command("status"))
async def status_command(message: types.Message):
    user_id = message.from_user.id
    is_premium = await get_premium(user_id)
    
    if is_premium:
        await message.answer("🌟 У тебя активен безлимит! Генерируй сколько хочешь ✨")
        return
    
    today = await get_generations_today(user_id)
    remaining = max(0, FREE_GENERATIONS - today)
    
    await message.answer(
        f"📊 **Твой статус:**\n"
        f"🎨 Бесплатных генераций сегодня: {remaining}/{FREE_GENERATIONS}\n\n"
        f"⭐ Одна генерация: {PRICE_PER_GENERATION} Stars (отправится автоматически)\n"
        f"🌟 Безлимит 30 дней: {PREMIUM_PRICE} Stars — купить /buy"
    )

# ===== ОСНОВНОЙ ОБРАБОТЧИК =====
@dp.message()
async def generate(message: types.Message):
    if not message.text:
        return
    
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    if len(prompt) > 200:
        await message.answer("❌ Слишком длинный промпт (максимум 200 символов)")
        return
    
    is_premium = await get_premium(user_id)
    today = await get_generations_today(user_id)
    
    # Не премиум и лимит исчерпан → продаём одну генерацию
    if not is_premium and today >= FREE_GENERATIONS:
        prices = [LabeledPrice(label="Одна генерация 🎨", amount=PRICE_PER_GENERATION)]
        
        await message.answer_invoice(
            title="Генерация изображения",
            description=f"Сгенерирую картинку по запросу:\n_{prompt[:100]}_",
            payload=f"single_gen:{prompt}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="generate_image"
        )
        return
    
    # Увеличиваем счётчик (только для бесплатных)
    if not is_premium:
        await incr_generations_today(user_id)
    
    # Генерируем
    await bot.send_chat_action(message.chat.id, "upload_photo")
    wait_msg = await message.answer("🎨 Думаю... рисуем...")
    
    img_bytes = await generate_image(prompt)
    
    if img_bytes:
        photo = BufferedInputFile(img_bytes.getvalue(), filename="image.jpg")
        await message.answer_photo(photo, caption=f"🎨 По запросу: _{prompt[:100]}_\n✨ Сгенерировано нейросетью")
        await wait_msg.delete()
    else:
        await wait_msg.edit_text("❌ Не удалось сгенерировать. Попробуй другой промпт.")

# ===== WEBHOOK =====
async def root(request):
    return web.Response(text="Image Bot is alive")

async def ping(request):
    return web.Response(text="OK")

async def on_startup(app):
    global redis_client
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    logging.info("Redis connected")
    
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(app):
    await bot.session.close()
    if redis_client:
        await redis_client.close()

def create_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/ping", ping)
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
