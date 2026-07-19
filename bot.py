import os 
import io
import time
import pytz
from datetime import datetime
import logging
import random
import asyncio
import httpx
import threading
import schedule
import urllib.request
from PIL import Image
from dotenv import load_dotenv

from telegram import Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from supabase import create_client, Client
from flask import Flask

# Import from our new ai_processor
from ai_processor import setup_ai, generate_wallpaper_metadata

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load env variables
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_USER_IDS = os.getenv("ADMIN_USER_IDS", "") # Comma-separated admin IDs
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://yourwebsite.com")
TODAYS_SCHEDULE = []

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("❌ Missing environment variables! Please check your .env file.")
    exit(1)

# Ensure AI is setup
setup_ai()

# Supabase Client setup
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Separate lightweight bot instance just for the scheduler posting
# The uploader will use the application's bot instance
request = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)
scheduler_bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)

# -------------------------------------------------------------
# FLASK WEB SERVER & PINGER FOR RENDER
# -------------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Unified AuraWalls Bot is running perfectly on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_awake_pinger():
    """Automatically pings its own Render URL every 10 minutes so it never sleeps."""
    my_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not my_url:
        logger.warning("No RENDER_EXTERNAL_URL found. Auto-ping disabled.")
        return
        
    while True:
        try:
            time.sleep(10 * 60)
            logger.info(f"🔄 Auto-Knock: Pinging {my_url} to stay awake...")
            urllib.request.urlopen(my_url)
        except Exception as e:
            logger.error(f"❌ Auto-Knock Failed: {e}")

# -------------------------------------------------------------
# UPLOADER LOGIC (Telegram -> Supabase)
# -------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Hello Admin!**\n\n"
        "Send me a wallpaper (Photo or Document) and I will:\n"
        "1. Auto-tag it using Gemini AI.\n"
        "2. Upload to Supabase Storage.\n"
        "3. Save to Database.",
        parse_mode="Markdown"
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming photos and documents."""
    # SECURITY CHECK: Only allow the admins to upload
    user_id = str(update.message.from_user.id)
    allowed_admins = [uid.strip() for uid in ADMIN_USER_IDS.split(",") if uid.strip()]
    
    if allowed_admins and user_id not in allowed_admins:
        logger.warning(f"Unauthorized upload attempt by user ID {user_id}")
        await update.message.reply_text("❌ You are not authorized to upload wallpapers.")
        return

    msg = await update.message.reply_text("⚙️ Receiving image... Please wait.")
    temp_file_path = None
    
    try:
        file_id = None
        extension = "jpg"
        
        if update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
        elif update.message.document:
            doc = update.message.document
            mime = doc.mime_type
            if not mime or not mime.startswith("image/"):
                await msg.edit_text("❌ Please send an IMAGE file.")
                return
            file_id = doc.file_id
            if "." in doc.file_name:
                extension = doc.file_name.split(".")[-1]
                
        if not file_id:
            await msg.edit_text("❌ Unknown media format.")
            return
            
        temp_file_path = f"temp_{file_id}.{extension}"
            
        await msg.edit_text("⏳ Downloading image from Telegram servers...")
        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(temp_file_path)
        
        await msg.edit_text("🧠 Analyzing image using multi-model Gemini AI...")
        metadata = generate_wallpaper_metadata(temp_file_path)
        
        title = metadata.get("title", "Premium Wallpaper").strip()
        category = metadata.get("category", "Aesthetic").strip()
        description = metadata.get("description", "").strip()
        tags = metadata.get("tags", [])
        
        await msg.edit_text("☁️ Uploading to Supabase Cloud Storage bucket 'image'...")
        
        clean_title = title.lower().replace(" ", "-")
        safe_title = "".join(c for c in clean_title if c.isalnum() or c == "-")
        unique_filename = f"{int(time.time())}-{safe_title}.{extension}"
        
        supabase.storage.from_("image").upload(
            path=unique_filename, 
            file=temp_file_path,
            file_options={"content-type": f"image/{extension}"}
        )
        
        file_url = supabase.storage.from_("image").get_public_url(unique_filename)
        
        await msg.edit_text("📝 Saving record to Supabase Database...")
        
        current_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        db_payload = {
            "title": title,
            "category": category,
            "description": description,
            "file_url": file_url,
            "created_at": current_time
        }
        
        supabase.table("photos").insert(db_payload).execute()
        
        success_text = (
            f"✅ **Wallpaper Successfully Uploaded!**\n\n"
            f"📌 **Title:** {title}\n"
            f"📂 **Category:** {category}\n"
            f"📝 **Description:** {description}\n"
            f"🏷️ **Tags:** {', '.join(tags)}\n\n"
            f"🔗 [View Source Image]({file_url})"
        )
        await msg.edit_text(success_text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error handling media: {e}")
        await msg.edit_text(f"❌ An error occurred during upload:\n`{e}`", parse_mode="Markdown")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up temp file: {cleanup_error}")

# -------------------------------------------------------------
# PUBLISHER LOGIC (Supabase -> Telegram Channel)
# -------------------------------------------------------------
async def post_wallpaper(wallpaper):
    """Posts a single wallpaper to the Telegram channel and updates the database."""
    try:
        wp_id    = wallpaper.get("id")
        title    = wallpaper.get("title", "Awesome Wallpaper")
        file_url = wallpaper.get("file_url")
        category = wallpaper.get("category", "")

        if not file_url:
            logger.error(f"Wallpaper '{title}' has no file_url. Skipping.")
            return False

        if category:
            tag = category.replace(" ", "").lower()
            hashtags = f"#{tag} #wallpaper #4k #hd"
        else:
            hashtags = "#wallpaper #4k #hd #aesthetic"

        caption  = f"<b>{title}</b>\n\n{hashtags}"

        logger.info(f"📤 Downloading image for Telegram: {title}")
        
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(file_url)
            if resp.status_code != 200:
                logger.error(f"Failed to download image from {file_url}.")
                return False
            image_bytes = resp.content

        preview_bytes = image_bytes
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((2560, 2560))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            thumb_io = io.BytesIO()
            img.save(thumb_io, format="JPEG", quality=85)
            preview_bytes = thumb_io.getvalue()
        except Exception as e:
            logger.warning(f"Failed to resize photo preview: {e}")

        logger.info("📤 Posting Photo preview to Telegram...")
        await scheduler_bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_ID,
            photo=preview_bytes,
            caption=caption,
            parse_mode="HTML"
        )

        logger.info("📤 Posting original file as Document to Telegram...")
        await scheduler_bot.send_document(
            chat_id=TELEGRAM_CHANNEL_ID,
            document=image_bytes,
            filename=f"{title.replace(' ', '_')}.jpg"
        )
        
        # Mark as posted
        current_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        supabase.table("photos").update({"last_posted_at": current_time}).eq("id", wp_id).execute()
        logger.info(f"Successfully posted and marked {wp_id} as posted.")
        return True
        
    except Exception as e:
        logger.error(f"Failed to post wallpaper {wallpaper.get('title')}: {e}")
        return False

async def post_single_wallpaper_job():
    logger.info("Scheduled task triggered — posting one wallpaper...")
    try:
        response = supabase.table("photos").select("*").is_("last_posted_at", "null").order("created_at", desc=True).limit(10).execute()
        wallpapers = response.data

        if not wallpapers:
            logger.info("No unposted wallpapers found.")
            return

        if random.random() < 0.5:
            chosen = wallpapers[0]
        else:
            chosen = random.choice(wallpapers)

        await post_wallpaper(chosen)
    except Exception as e:
        logger.error(f"Error in single wallpaper job: {e}")

def single_job_wrapper():
    asyncio.run(post_single_wallpaper_job())

async def get_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TODAYS_SCHEDULE:
        await update.message.reply_text("❌ No schedule generated yet.")
        return
    
    # Convert UTC times to IST for display
    ist_tz = pytz.timezone('Asia/Kolkata')
    msg = "📅 **Today's Posting Schedule (IST):**\n\n"
    
    for time_str in TODAYS_SCHEDULE:
        # Create a datetime object for today at the scheduled time (UTC)
        utc_dt = datetime.strptime(f"{datetime.now().strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
        utc_dt = pytz.utc.localize(utc_dt)
        ist_dt = utc_dt.astimezone(ist_tz)
        
        # Format as HH:MM AM/PM
        msg += f"• {ist_dt.strftime('%I:%M %p')}\n"
        
    await update.message.reply_text(msg, parse_mode="Markdown")

def schedule_random_times_for_today():
    global TODAYS_SCHEDULE
    TODAYS_SCHEDULE = []
    schedule.clear('daily_posts')
    start_minutes = 90
    end_minutes   = 810
    num_posts = random.randint(5, 6)
    chosen_minutes = sorted(random.sample(range(start_minutes, end_minutes), num_posts))

    logger.info(f"Today's schedule ({num_posts} posts):")
    for mins in chosen_minutes:
        hour   = mins // 60
        minute = mins % 60
        time_str = f"{hour:02d}:{minute:02d}"
        TODAYS_SCHEDULE.append(time_str)
        schedule.every().day.at(time_str).do(single_job_wrapper).tag('daily_posts')
        logger.info(f"  → {time_str} UTC")

def scheduler_thread():
    logger.info("Starting background scheduler thread...")
    schedule_random_times_for_today()
    schedule.every().day.at("00:00").do(schedule_random_times_for_today)
    
    while True:
        schedule.run_pending()
        time.sleep(30)

# -------------------------------------------------------------
# MAIN ENTRY POINT
# -------------------------------------------------------------
def main():
    logger.info("🚀 Starting Unified AuraWalls Telegram Bot...")
    
    # 1. Start Flask Web Server
    threading.Thread(target=run_flask, daemon=True).start()
    
    # 2. Start Keep-Awake Pinger
    threading.Thread(target=keep_awake_pinger, daemon=True).start()
    
    # 3. Start Wallpaper Posting Scheduler
    threading.Thread(target=scheduler_thread, daemon=True).start()
    
    # 4. Start Telegram Message Listener (Uploader) in main thread
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).read_timeout(60).write_timeout(60).connect_timeout(60).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("schedule", get_schedule))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_media))
    
    logger.info("🎧 Listening for direct messages to upload wallpapers...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
