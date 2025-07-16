import os
import sys
import io
import re
import time
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()

from PIL import Image, ImageDraw
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
import aiohttp
from keep_alive import keep_alive  # İstersen yoruma al

# LOG AYARLARI
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN ortam değişkeni ayarlanmamış! .env dosyasına ekleyin veya Render ortam değişkeni tanımlayın.")
    exit(1)

COUNTRY_FLAGS = {
    "DE": "🇩🇪 ALM", "FR": "🇫🇷 FRA", "COM": "🇺🇸 USA",
    "ES": "🇪🇸 ISP", "PL": "🇵🇱 POL", "SE": "🇸🇪 ISV",
    "COM.BE": "🇧🇪 BEL", "NL": "🇳🇱 HOL"
}

EXCHANGE_RATES = {
    "DE": 47.0, "FR": 47.0, "COM": 40.0,
    "ES": 47.0, "PL": 11, "SE": 4,
    "COM.BE": 46.1, "NL": 47.0
}

def parse_prices_from_html(html: str, asin: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    boxes = soup.select(".amzbox")
    if not boxes:
        return "❌ Ürün bulunamadı veya fiyat bilgisi yok.", "COM"

    result = ""
    first_country_code = ""
    for box in boxes:
        data_id = box.get("data-id", "")
        country_code = data_id.split("-")[-1].upper() if "-" in data_id else data_id.upper()
        if not first_country_code:
            first_country_code = country_code
        country_name = COUNTRY_FLAGS.get(country_code, country_code)
        price_span = box.select_one(".offered-price")
        price_text = price_span.get_text(strip=True) if price_span else "Fiyat yok"
        price_number_match = re.search(r"[\d.,]+", price_text)
        amazon_domain = country_code.lower().replace("com.", "com.").replace("COM", "com")
        product_url = f"https://www.amazon.{amazon_domain}/dp/{asin}"

        if price_number_match:
            price_str = price_number_match.group().replace(",", ".")
            try:
                price_value = float(price_str)
                kur = EXCHANGE_RATES.get(country_code, 1)
                tl_value = price_value * kur
                result += f"{country_name}: 💰 *{price_text}* → Amazon {int(tl_value)} TL [🔗 Bağlantıya git]({product_url})\n"
            except Exception as e:
                logging.warning(f"TL dönüşüm hatası: {e}")
                result += f"{country_name}: 💰 *{price_text}* → Amazon [🔗]({product_url})\n"
        else:
            result += f"{country_name}: 💰 *{price_text}* → Amazon [🔗]({product_url})\n"
    return result.strip(), first_country_code

async def fetch_amazon_image_and_title_simple(asin: str, country_code: str) -> tuple[str, str]:
    url = f"https://www.amazon.{country_code.lower()}/dp/{asin}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                img_tag = soup.select_one("#imgTagWrapperId img")
                image_url = ""
                if img_tag:
                    image_url = img_tag.get("src") or img_tag.get("data-old-hires") or ""
                    if not image_url:
                        match = re.search(r'"(https://[^"]+)"', img_tag.get("data-a-dynamic-image", ""))
                        if match:
                            image_url = match.group(1)
                title_tag = soup.select_one("#productTitle")
                title = title_tag.get_text(strip=True) if title_tag else f"{asin} Ürünü"
                return image_url, title
    except Exception as e:
        logging.warning(f"Amazon'dan veri alınamadı: {e}")
        return "", f"{asin} Ürünü"

async def get_prices_simple(asin: str, retries: int = 7) -> tuple[str, str, str, str]:
    url = f"https://webprice.eu/amazon/{asin}/"
    for attempt in range(1, retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()
                    prices_text, first_country = parse_prices_from_html(html, asin)
                    if "Ürün bulunamadı" in prices_text and attempt < retries:
                        logging.warning(f"{asin} için {attempt}. deneme başarısız, tekrar deneniyor...")
                        await asyncio.sleep(3.0 * attempt)
                        continue
                    image_url, product_title = await fetch_amazon_image_and_title_simple(asin, first_country)
                    return prices_text, image_url, product_title, first_country
        except Exception as e:
            logging.warning(f"get_prices_simple hatası (deneme {attempt}): {e}")
            if attempt == retries:
                return f"❌ Hata oluştu: {e}", "", "", "COM"
            await asyncio.sleep(3.0 * attempt)
    return "❌ Ürün bulunamadı veya fiyat bilgisi yok.", "", "", "COM"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_heartbeat
    last_heartbeat = time.time()
    await asyncio.sleep(2)
    asin = update.message.text.strip().upper()
    if not (asin.startswith("B0") and len(asin) == 10):
        await update.message.reply_text("⚠️ Lütfen geçerli bir ASIN gönderin. Örnek: B0DZGHZQ7V")
        return

    await update.message.reply_text("🔍 Fiyatlar çekiliyor, lütfen bekleyiniz...")
    prices_text, image_url, product_title, _ = await get_prices_simple(asin)
    signature = "🔥Ens🔥Hsn🔥Ibr🔥Kad🔥Onr🔥Sdk🔥Ilk🔥"
    message_text = f"*{product_title}*\n\n{prices_text}\n\n{signature}"

    if image_url.startswith("http"):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if "text/html" in resp.headers.get("Content-Type", ""):
                        raise Exception("Resim yerine HTML döndü.")
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                        draw = ImageDraw.Draw(img)
                        draw.rectangle([img.width - 2, img.height - 2, img.width - 1, img.height - 1], fill=(254, 254, 254))
                        img_buffer = io.BytesIO()
                        img.save(img_buffer, format='JPEG', quality=95)
                        img_buffer.seek(0)
                        img_file = InputFile(img_buffer, filename="product.jpg")
                        await update.message.reply_photo(
                            photo=img_file, caption=message_text, parse_mode="Markdown"
                        )
                        return
        except Exception as e:
            logging.warning(f"Görsel gönderilemedi: {e}")

    await update.message.reply_text(message_text, parse_mode="Markdown")

# --- Watchdog ---
last_heartbeat = time.time()

async def update_heartbeat():
    global last_heartbeat
    while True:
        last_heartbeat = time.time()
        await asyncio.sleep(60)

async def watchdog():
    global last_heartbeat
    while True:
        if time.time() - last_heartbeat > 300:
            logging.warning("⚠️ Bot tepki vermiyor. Yeniden başlatılıyor...")
            os.execv(sys.executable, ['python'] + sys.argv)
        await asyncio.sleep(60)

def main():
    keep_alive()  # Gerekirse yoruma al
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logging.info("✅ Bot hazır. Telegram'dan ASIN gönder...")
    loop = asyncio.get_event_loop()
    loop.create_task(update_heartbeat())
    loop.create_task(watchdog())
    app.run_polling()

if __name__ == "__main__":
    main()
