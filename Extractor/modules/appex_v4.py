import asyncio
import base64
import json
import re
import time
import os
import logging
import cloudscraper
import aiohttp
import httpx
import jwt  # pip install pyjwt
from pyrogram import filters
from pyrogram.types import Message
from Extractor import app
from Extractor.core.utils import forward_to_log
from Extractor.modules.mix import v2_new
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from datetime import datetime
import pytz

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

india_timezone = pytz.timezone('Asia/Kolkata')
time_new = datetime.now(india_timezone).strftime("%d-%m-%Y %I:%M %p")

# ---------- Encryption / Decryption Functions ----------
def decrypt(enc):
    """Decrypt AES encrypted content using appx key."""
    try:
        if not enc:
            return ""
        enc = base64.b64decode(enc.split(':')[0])
        key = '638udh3829162018'.encode('utf-8')
        iv = 'fedcba9876543210'.encode('utf-8')
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = unpad(cipher.decrypt(enc), AES.block_size)
        return plaintext.decode('utf-8')
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return ""

def decode_base64(encoded_str):
    """Decode base64 string."""
    try:
        return base64.b64decode(encoded_str).decode('utf-8')
    except:
        return ""

# ---------- Async Fetch Helper (Using aiohttp) ----------
async def fetch(session, url, headers):
    """Fetch JSON from API asynchronously."""
    try:
        async with session.get(url, headers=headers, timeout=30) as resp:
            if resp.status != 200:
                return {}
            text = await resp.text()
            # Sometimes response is wrapped in HTML/JSONP, try to extract JSON
            soup = BeautifulSoup(text, 'html.parser')
            return json.loads(str(soup))
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return {}

# ---------- Process a Single Video ----------
async def process_video(session, api_base, batch_id, subject_id, topic, video, headers):
    """Extract video and PDF links from a video entry."""
    video_id = video.get("id")
    if not video_id:
        return None

    try:
        url = f"{api_base}/get/fetchVideoDetailsById?course_id={batch_id}&video_id={video_id}&ytflag=0&folder_wise_course=0"
        r4 = await fetch(session, url, headers)
        if not r4 or not r4.get("data"):
            return None

        data = r4["data"]
        title = data.get("Title", "")
        download_link = data.get("download_link", "")
        youtube_id = data.get("video_id", "")
        lines = []

        # YouTube link
        if youtube_id:
            decrypted_youtube = decrypt(youtube_id)
            lines.append(f"{title}:https://youtu.be/{decrypted_youtube}\n")

        # Direct download link
        if download_link:
            decrypted_link = decrypt(download_link)
            if ".pdf" not in decrypted_link:
                lines.append(f"{title}:{decrypted_link}\n")
        else:
            # Encrypted links (DRM)
            encrypted_links = data.get("encrypted_links", [])
            if encrypted_links:
                first = encrypted_links[0]
                path = first.get("path")
                key = first.get("key")
                if path and key:
                    decrypted_path = decrypt(path)
                    decrypted_key = decode_base64(decrypt(key))
                    lines.append(f"{title}:{decrypted_path}*{decrypted_key}\n")
                elif path:
                    lines.append(f"{title}:{decrypt(path)}\n")

        # PDFs (if material_type is VIDEO)
        if data.get("material_type") == "VIDEO":
            for pdf_num in range(1, 3):
                link_key = f"pdf_link{'' if pdf_num == 1 else str(pdf_num)}"
                key_key = f"pdf{'' if pdf_num == 1 else str(pdf_num)}_encryption_key"
                pdf_link = data.get(link_key, "")
                pdf_key = data.get(key_key, "")
                if pdf_link and pdf_key:
                    dp = decrypt(pdf_link)
                    dk = decrypt(pdf_key)
                    if dk == "abcdefg":
                        lines.append(f"{title}:{dp}\n")
                    else:
                        lines.append(f"{title}:{dp}*{dk}\n")

        return lines
    except Exception as e:
        logger.error(f"Error processing video {video_id}: {e}")
        return None

# ---------- Process a Course Subject ----------
async def handle_course(session, api_base, batch_id, subject_id, subject_name, topic, headers):
    """Process all videos in a topic."""
    topic_id = topic.get("topicid")
    if not topic_id:
        return []

    url = f"{api_base}/get/livecourseclassbycoursesubtopconceptapiv3?courseid={batch_id}&subjectid={subject_id}&topicid={topic_id}&conceptid=&start=-1"
    r3 = await fetch(session, url, headers)
    video_data = sorted(r3.get("data", []), key=lambda x: x.get("id"))

    tasks = [process_video(session, api_base, batch_id, subject_id, topic, video, headers) for video in video_data]
    results = await asyncio.gather(*tasks)
    # Flatten results
    return [line for lines in results if lines for line in lines]

# ---------- Main Extraction Function (Appex V5) ----------
async def appex_v5_txt(app, message, api, name):
    """Main extraction logic for Appx apps."""
    api_base = f"https://{api.replace('https://', '').replace('http://', '').rstrip('/')}"
    app_name = api_base.split('/')[-1] if '/' in api_base else api_base.split('.')[0]

    # Ask for credentials
    login_prompt = (
        "📝 **Send credentials in one of these formats:**\n"
        "1. `ID*Password`\n"
        "2. `Token*UserID` (if you have both)\n"
        "3. `Token` (if you have token only, I will ask for UserID)\n\n"
        "Example: `user@mail.com*pass123` or `eyJ...*12345`"
    )
    input1 = await app.ask(message.chat.id, login_prompt)
    await forward_to_log(input1, "Appex Extractor")
    raw_text = input1.text.strip()
    await input1.delete()

    token = None
    userid = None

    # Parse input
    if '*' in raw_text:
        parts = raw_text.split('*', 1)
        if len(parts) == 2:
            # Check if second part looks like a userid (numeric or JWT claim)
            if parts[1].isdigit() or len(parts[1]) < 20:  # likely userid
                token, userid = parts
            else:
                # It's ID*Password
                email, password = parts
                # Login using async httpx (primary)
                login_headers = {
                    "Auth-Key": "appxapi",
                    "User-Id": "-2",
                    "Language": "en",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "okhttp/4.9.1",
                    "Host": api_base.replace("https://", ""),
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "Keep-Alive"
                }
                data = {"email": email, "password": password}
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(f"{api_base}/post/userLogin", headers=login_headers, data=data)
                        if resp.status_code not in (200, 203):
                            await message.reply_text(f"❌ Login failed. Status: {resp.status_code}")
                            return
                        resp_json = resp.json()
                        if resp_json.get("status") not in (200, 203):
                            await message.reply_text("❌ Login failed. Invalid credentials or server error.")
                            return
                        token = resp_json["data"]["token"]
                        userid = str(resp_json["data"]["userid"])
                except Exception as e:
                    await message.reply_text(f"❌ Login error (HTTPX): {str(e)}")
                    return
    else:
        # Assume raw_text is token
        token = raw_text
        # Try to decode JWT to get userid
        try:
            decoded = jwt.decode(token, options={"verify_signature": False})
            userid = str(decoded.get("id") or decoded.get("userid") or decoded.get("user_id"))
        except:
            # If decode fails, ask user for UserID
            userid_input = await app.ask(message.chat.id, "🔑 Token received. Now send your **User ID**:")
            userid = userid_input.text.strip()
            await userid_input.delete()

    # Headers for API calls (Must match mobile app exactly)
    hdr1 = {
        "Client-Service": "Appx",
        "source": "website",
        "Auth-Key": "appxapi",
        "Authorization": token,
        "User-ID": userid,
        "User-Agent": "okhttp/4.9.1",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "Keep-Alive",
        "Host": api_base.replace("https://", "")
    }

    # --- FETCH COURSES WITH FALLBACK (SCRAPER FIX) ---
    mc1 = None
    loop = asyncio.get_event_loop()

    # Method 1: Try httpx first (faster, less prone to block if headers are correct)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{api_base}/get/mycoursev2?userid={userid}", headers=hdr1)
            if resp.status_code == 200:
                mc1 = resp.json()
    except Exception as e:
        logger.warning(f"HTTPX failed for course list: {e}. Falling back to cloudscraper...")

    # Method 2: Fallback to cloudscraper (if httpx fails or returns 403)
    if not mc1 or mc1.get("status") != 200:
        try:
            scraper = cloudscraper.create_scraper()
            # run in thread to avoid blocking
            mc1 = await loop.run_in_executor(
                None,
                lambda: scraper.get(f"{api_base}/get/mycoursev2?userid={userid}", headers=hdr1).json()
            )
        except Exception as e:
            await message.reply_text(f"❌ Both scrapers failed. Cannot fetch courses: {str(e)}")
            return

    if not mc1 or not mc1.get("data"):
        await message.reply_text("❌ No batches found or data empty.")
        return

    # Display batches
    batch_list = ""
    valid_ids = []
    for ct in mc1["data"]:
        batch_list += f"<code>{ct['id']}</code> - <b>{ct['course_name']}</b>\n"
        valid_ids.append(str(ct['id']))

    await message.reply_text(f"✅ **Login successful!**\n\n📚 **Available batches:**\n{batch_list}")

    # Ask for batch IDs
    input2 = await app.ask(message.chat.id, "📥 **Send Batch ID(s) to download** (separate multiple with `&`):")
    batch_ids = [b.strip() for b in input2.text.strip().split('&') if b.strip() in valid_ids]
    await input2.delete()

    if not batch_ids:
        await message.reply_text("❌ No valid batch IDs provided.")
        return

    # Process each batch
    for batch_id in batch_ids:
        status_msg = await message.reply_text(f"⏳ Extracting batch `{batch_id}`...")
        start_time = time.time()

        course_info = next((c for c in mc1["data"] if str(c['id']) == batch_id), {})
        course_name = course_info.get("course_name", "Course").replace("/", "_").replace(":", "_")

        # Try to get course details via course_by_id API (using scraper fallback)
        r_json = None
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{api_base}/get/course_by_id?id={batch_id}", headers=hdr1)
                if resp.status_code == 200:
                    r_json = resp.json()
        except:
            pass

        if not r_json or not r_json.get("data"):
            # Fallback to v2_new (folder-wise)
            await v2_new(
                app, message, token, userid, hdr1, app_name,
                batch_id, api_base, course_name,
                start_time, "", "", "", input2, status_msg, None
            )
            continue

        # Process using V3 method (subject-topic-video)
        filename = f"{batch_id}_{course_name}.txt"
        async with aiohttp.ClientSession() as session:
            with open(filename, 'w', encoding='utf-8') as f:
                # Fetch subjects
                r1 = await fetch(session, f"{api_base}/get/allsubjectfrmlivecourseclass?courseid={batch_id}&start=-1", hdr1)
                subjects = r1.get("data", [])

                # Process each subject concurrently
                tasks = []
                for subject in subjects:
                    si = subject.get("subjectid")
                    sn = subject.get("subject_name")
                    # Fetch topics for this subject
                    r2 = await fetch(session, f"{api_base}/get/alltopicfrmlivecourseclass?courseid={batch_id}&subjectid={si}&start=-1", hdr1)
                    topics = sorted(r2.get("data", []), key=lambda x: x.get("topicid"))
                    for topic in topics:
                        tasks.append(handle_course(session, api_base, batch_id, si, sn, topic, hdr1))

                # Wait for all topics to complete
                results = await asyncio.gather(*tasks)
                for lines in results:
                    if lines:
                        f.writelines(lines)

        # Check if file has content
        if os.path.getsize(filename) == 0:
            await message.reply_text(f"⚠️ No content found in batch `{batch_id}`.")
            os.remove(filename)
            await status_msg.delete()
            continue

        elapsed = time.time() - start_time
        caption = (
            f"🎓 **COURSE EXTRACTED**\n\n"
            f"📱 **App:** {app_name}\n"
            f"📚 **Batch:** {course_name} (ID: {batch_id})\n"
            f"⏱ **Time Taken:** {elapsed:.1f}s\n"
            f"📅 **Date:** {time_new}\n\n"
            f"🚀 Extracted by @{(await app.get_me()).username}"
        )

        # Send file to user and log channel
        await message.reply_document(filename, caption=caption)
        # await app.send_document(PREMIUM_LOGS, filename, caption=caption)  # Uncomment if you want to log

        os.remove(filename)
        await status_msg.delete()

# ---------- Command Handler ----------
@app.on_message(filters.command(["appx"]))
async def appex_v4_txt(app, message):
    """Entry point for /appx command."""
    api_prompt = (
        "🌐 **Enter API URL** (without https://)\n"
        "Example: `tcsexamzoneapi.classx.co.in`\n\n"
        "Or use /findapi to search."
    )
    api = await app.ask(message.chat.id, api_prompt)
    api_txt = api.text.strip()
    name = api_txt.split('.')[0].replace("api", "") if api else "Appx"

    if "api" in api_txt:
        await appex_v5_txt(app, message, api_txt, name)
    else:
        await message.reply_text("❌ Invalid API. Use /findapi to get correct one.")
