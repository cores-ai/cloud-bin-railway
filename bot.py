"""
Railway Telegram Intake Bot.

Automated 2-photo & 1-email collector for Cloudflare D1 (doc-dustbin).
Detects email (Gmail or custom domain with @), receives 2 photos in any order,
prompts step-by-step inline keyboards for Uploader (A / P / AP), Sale Status (Unsold / Sold),
and Processing Status (Pending / Complete), timestamps with Indian Standard Time (IST),
and uploads everything directly to Cloudflare D1.
"""

import base64
import io
import os
import re
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageOps

from d1 import D1, load_env

IST = ZoneInfo("Asia/Kolkata")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
API = "https://api.telegram.org/bot{token}/{method}"
DRAFT_LOCK = threading.Lock()


def owner_ids(raw: str) -> set:
    owners = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                owners.add(int(part))
            except ValueError:
                pass
    return owners


def smart_label(email: str) -> str:
    """Clean folder label derived from the mailbox address."""
    local = email.split("@", 1)[0]
    words = [word for word in re.sub(r"[._+\-]+", " ", local).split() if word]
    if not words:
        return "Folder"
    if len(words) == 1 or len(words[0]) >= 12:
        label = words[0][:18]
    else:
        label = " ".join(words[:2])[:20].rstrip()
    parts = []
    for word in label.split(" "):
        parts.append(word[:1].upper() + word[1:] if word.isalpha() else word)
    return " ".join(parts)


def slug_base(email: str, now: datetime) -> str:
    local = email.split("@", 1)[0].lower()
    local = re.sub(r"[^a-z0-9]+", "-", local).strip("-")[:18] or "folder"
    rand_entropy = secrets.token_hex(4)  # 32-bit cryptographic entropy prevents IDOR/enumeration
    return f"{local}-{now.strftime('%d%b-%H%M').lower()}-{rand_entropy}"


def extract_email(text: str) -> Optional[str]:
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else None


def photo_file_id(message: Dict[str, Any]) -> Optional[str]:
    photos = message.get("photo") or []
    if photos:
        # Highest resolution is always the last item in Telegram photo array
        return photos[-1].get("file_id")
    document = message.get("document") or {}
    if str(document.get("mime_type") or "").startswith("image/") and document.get("file_id"):
        return document["file_id"]
    return None


def shrink(data: bytes) -> bytes:
    """Optimize photo for D1 storage without sacrificing high visual clarity."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    image = image.convert("RGB")
    image.thumbnail((1440, 1440))
    quality = 82
    blob = b""
    while quality >= 45:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        blob = buffer.getvalue()
        if len(blob) <= 220_000:
            return blob
        quality -= 8
    smaller = image.copy()
    smaller.thumbnail((1024, 1024))
    buffer = io.BytesIO()
    smaller.save(buffer, format="JPEG", quality=50, optimize=True)
    return buffer.getvalue()


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.offset = 0

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 40) -> Dict[str, Any]:
        response = requests.post(API.format(token=self.token, method=method), json=payload or {}, timeout=timeout)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Telegram {method} returned non-JSON ({response.status_code})") from exc
        if not response.ok or not data.get("ok"):
            raise RuntimeError(data.get("description") or f"Telegram {method} failed")
        return data

    def send(self, chat_id: int, text: str, parse_mode: Optional[str] = "HTML", reply_markup: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.call("sendMessage", payload, timeout=20)

    def file_bytes(self, file_id: str) -> bytes:
        meta = self.call("getFile", {"file_id": file_id}, timeout=30)
        path = meta["result"]["file_path"]
        response = requests.get(f"https://api.telegram.org/file/bot{self.token}/{path}", timeout=90)
        response.raise_for_status()
        return response.content

    def updates(self) -> List[Dict[str, Any]]:
        data = self.call(
            "getUpdates",
            {"offset": self.offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
            timeout=40,
        )
        batch = data.get("result") or []
        if batch:
            self.offset = batch[-1]["update_id"] + 1
        return batch


def blank_draft() -> Dict[str, Any]:
    return {
        "email": None,
        "photo1_b64": None,
        "photo1_mime": None,
        "photo2_b64": None,
        "photo2_mime": None,
        "author": None,
        "sale_status": None,
        "work_status": None,
        "step": None,
    }


def load_draft(d1: D1, owner_id: int) -> Dict[str, Any]:
    rows = d1.rows("SELECT * FROM intake_drafts WHERE owner_id = ?", [owner_id])
    draft = blank_draft()
    if rows:
        for key in draft:
            draft[key] = rows[0].get(key)
    return draft


def save_draft(d1: D1, owner_id: int, draft: Dict[str, Any], now: datetime) -> None:
    d1.run(
        """
        INSERT INTO intake_drafts (
            owner_id, email, photo1_b64, photo1_mime, photo2_b64, photo2_mime,
            author, sale_status, work_status, step, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_id) DO UPDATE SET
            email = excluded.email,
            photo1_b64 = excluded.photo1_b64,
            photo1_mime = excluded.photo1_mime,
            photo2_b64 = excluded.photo2_b64,
            photo2_mime = excluded.photo2_mime,
            author = excluded.author,
            sale_status = excluded.sale_status,
            work_status = excluded.work_status,
            step = excluded.step,
            updated_at = excluded.updated_at
        """,
        [
            owner_id,
            draft.get("email"),
            draft.get("photo1_b64"),
            draft.get("photo1_mime"),
            draft.get("photo2_b64"),
            draft.get("photo2_mime"),
            draft.get("author"),
            draft.get("sale_status"),
            draft.get("work_status"),
            draft.get("step"),
            now.isoformat(),
        ],
    )


def clear_draft(d1: D1, owner_id: int) -> None:
    d1.run("DELETE FROM intake_drafts WHERE owner_id = ?", [owner_id])


def is_ready(draft: Dict[str, Any]) -> bool:
    return bool(draft.get("email") and draft.get("photo1_b64") and draft.get("photo2_b64"))


def unique_slug(d1: D1, base: str) -> str:
    slug = base
    number = 2
    while d1.rows("SELECT id FROM submissions WHERE folder_slug = ?", [slug]):
        slug = f"{base}-{number}"
        number += 1
    return slug


def commit_submission(d1: D1, owner_id: int, draft: Dict[str, Any], now: datetime) -> Dict[str, str]:
    email = draft["email"]
    label = smart_label(email)
    slug = unique_slug(d1, slug_base(email, now))
    date_ist = now.strftime("%d %b %Y")
    time_ist = now.strftime("%I:%M %p").lstrip("0")
    author = draft.get("author") or "A"
    sale_status = draft.get("sale_status") or "unsold"
    work_status = draft.get("work_status") or "pending"

    inserted = d1.run(
        """
        INSERT INTO submissions (
            owner_id, email, folder_label, folder_slug, date_ist, time_ist, created_at,
            author, sale_status, work_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [owner_id, email, label, slug, date_ist, time_ist, now.isoformat(), author, sale_status, work_status],
    )
    submission_id = (inserted.get("meta") or {}).get("last_row_id")
    if not submission_id:
        raise RuntimeError("D1 did not return the new folder ID")

    for position, blob_key, mime_key in (
        (1, "photo1_b64", "photo1_mime"),
        (2, "photo2_b64", "photo2_mime"),
    ):
        d1.run(
            """
            INSERT INTO submission_photos (submission_id, position, mime_type, image_base64)
            VALUES (?, ?, ?, ?)
            """,
            [submission_id, position, draft.get(mime_key) or "image/jpeg", draft[blob_key]],
        )

    clear_draft(d1, owner_id)
    return {
        "label": label,
        "email": email,
        "slug": slug,
        "date_ist": date_ist,
        "time_ist": time_ist,
        "author": author,
        "sale_status": sale_status,
        "work_status": work_status,
    }


def attach_photo(token_api: Telegram, draft: Dict[str, Any], file_id: str) -> str:
    if draft.get("photo1_b64") and draft.get("photo2_b64"):
        return "full"
    raw_bytes = token_api.file_bytes(file_id)
    blob = base64.b64encode(shrink(raw_bytes)).decode("ascii")
    if not draft.get("photo1_b64"):
        draft["photo1_b64"] = blob
        draft["photo1_mime"] = "image/jpeg"
        return "first"
    draft["photo2_b64"] = blob
    draft["photo2_mime"] = "image/jpeg"
    return "second"


def get_status_message(draft: Dict[str, Any], action_taken: str = "") -> str:
    has_photo1 = bool(draft.get("photo1_b64"))
    has_photo2 = bool(draft.get("photo2_b64"))
    has_email = bool(draft.get("email"))
    email_val = draft.get("email") or ""

    pics_count = int(has_photo1) + int(has_photo2)

    parts = []
    if action_taken == "photo1":
        parts.append("📸 <b>1st photo received!</b>\n👉 <i>Send another pic</i> (2nd photo required).")
    elif action_taken == "photo2":
        parts.append("📸 <b>2nd photo received!</b>")
    elif action_taken == "email":
        parts.append(f"📧 <b>Email registered:</b> <code>{email_val}</code>")

    # What's remaining
    if pics_count == 0 and has_email:
        parts.append("👉 Send the <b>first photo</b> (1 of 2 required).")
    elif pics_count == 1 and not has_email:
        if action_taken != "photo1":
            parts.append("👉 <i>Send another pic</i> (Photo 2) or send the <b>email</b>.")
    elif pics_count == 1 and has_email:
        if action_taken != "photo1":
            parts.append("👉 <i>Send another pic</i> to complete this folder.")
    elif pics_count == 2 and not has_email:
        parts.append("👉 Now send the <b>email address</b> (Gmail or any @ domain) to save this folder.")

    return "\n\n".join(parts)


def handle_message(api: Telegram, d1: D1, master_owners: set, message: Dict[str, Any], passcode: str) -> None:
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = user.get("id")
    chat_id = chat.get("id")
    if chat_id is None or user.get("is_bot") or user_id is None:
        return

    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()
    username = user.get("username")
    first_name = user.get("first_name")

    # Check whether user is an authorized owner (via master list or Cloudflare D1 record)
    is_authed = (user_id in master_owners) or d1.is_user_authorized(user_id)

    if not is_authed:
        # Not authorized yet: inspect message for passcode
        cleaned = text.replace(" ", "")
        if cleaned and cleaned == passcode:
            d1.authorize_user(user_id, username, first_name)
            api.send(
                chat_id,
                "🔓 <b>Access Granted!</b> Account successfully authorized with owner rights.\n"
                "Saved permanently in Cloudflare D1. You won't be asked for passcode again!\n\n"
                "👋 <b>Auto! Folder Intake.</b>\n\n"
                "1️⃣ <b>Email address</b> (Gmail or custom domain with @)\n"
                "2️⃣ <b>2 Photos</b> (upload or forward)"
            )
            return

        if cleaned.isdigit() and len(cleaned) == 4:
            api.send(
                chat_id,
                "⛔ <b>Access Denied:</b> Incorrect 4-digit passcode.\n"
                "Please enter the valid passcode to authorize this Telegram account."
            )
        else:
            api.send(
                chat_id,
                "🔒 <b>Security Gate:</b> Please enter the 4-digit passcode to authorize this Telegram account."
            )
        return

    # Master owner or authorized user: ensure entry exists in D1
    if not d1.is_user_authorized(user_id):
        d1.authorize_user(user_id, username, first_name)

    # Commands & Reset helpers
    if text.lower() in ("/start", "/help", "hi", "hello"):
        api.send(
            chat_id,
            "👋 <b>Auto! Folder Intake.</b>\n\n"
            "No manual commands needed. Simply send:\n"
            "1️⃣ <b>Email address</b> (Gmail or custom domain with @)\n"
            "2️⃣ <b>2 Photos</b> (upload or forward)"
        )
        return

    if text.lower() in ("/reset", "reset", "/cancel", "cancel"):
        with DRAFT_LOCK:
            clear_draft(d1, user_id)
        api.send(chat_id, "🔄 <b>Draft cleared!</b> Ready to start a fresh folder.")
        return

    with DRAFT_LOCK:
        draft = load_draft(d1, user_id)
        email = extract_email(text) or extract_email(caption)
        file_id = photo_file_id(message)
        now = datetime.now(IST)

        action_taken = ""

        if email:
            draft["email"] = email
            action_taken = "email"

        if file_id:
            placed = attach_photo(api, draft, file_id)
            if placed == "first":
                action_taken = "photo1"
            elif placed == "second":
                action_taken = "photo2"
            elif placed == "full" and not is_ready(draft):
                api.send(chat_id, "⚠️ Already have 2 photos! Please send the <b>email address</b> to finish.")
                return

        if email or file_id:
            # Check if both photos and email are ready
            if is_ready(draft):
                # Start step-by-step inline chooser flow (Step 1: Uploader Name)
                draft["step"] = "pick_author"
                save_draft(d1, user_id, draft, now)

                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "🅰️ A", "callback_data": "choose_author:A"},
                            {"text": "🅿️ P", "callback_data": "choose_author:P"},
                            {"text": "🆎 AP", "callback_data": "choose_author:AP"},
                        ]
                    ]
                }
                api.send(
                    chat_id,
                    "📥 <b>Photos & Email Received!</b>\n\n"
                    "<b>Step 1 of 3:</b> Who is uploading this document?",
                    reply_markup=keyboard,
                )
                return

            save_draft(d1, user_id, draft, now)
            reply = get_status_message(draft, action_taken=action_taken)
            api.send(chat_id, reply)
            return

        # If user sent unrecognized text without @ and without photo
        api.send(
            chat_id,
            "⚠️ <b>Notice:</b> Please send an <b>email</b> (must contain <code>@</code>) or a <b>photo</b> to proceed."
        )


def handle_callback(api: Telegram, d1: D1, master_owners: set, query: Dict[str, Any]) -> None:
    query_id = query.get("id")
    user = query.get("from") or {}
    user_id = user.get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    data = query.get("data") or ""

    if not query_id:
        return

    # Acknowledge callback immediately to eliminate client loading spinners
    try:
        api.call("answerCallbackQuery", {"callback_query_id": query_id})
    except Exception:
        pass

    # Verify authorization
    if not (user_id in master_owners or d1.is_user_authorized(user_id)):
        api.send(chat_id, "⛔ <b>Unauthorized:</b> Please enter the passcode first.")
        return

    with DRAFT_LOCK:
        draft = load_draft(d1, user_id)
        if not is_ready(draft):
            api.send(chat_id, "⚠️ <i>Draft missing or expired. Please send photos and email again.</i>")
            return

        now = datetime.now(IST)

        # Step 1: Selected Author (A, P, AP)
        if data.startswith("choose_author:"):
            chosen_author = data.split(":", 1)[1]
            if chosen_author not in ("A", "P", "AP"):
                chosen_author = "A"
            draft["author"] = chosen_author
            draft["step"] = "pick_sale"
            save_draft(d1, user_id, draft, now)

            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🏷️ Unsold", "callback_data": "choose_sale:unsold"},
                        {"text": "💰 Sold", "callback_data": "choose_sale:sold"},
                    ]
                ]
            }
            api.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": (
                        f"👤 Uploader: <b>{chosen_author}</b>\n\n"
                        "<b>Step 2 of 3:</b> Select Sale Status:"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
            )
            return

        # Step 2: Selected Sale Status (unsold, sold)
        if data.startswith("choose_sale:"):
            chosen_sale = data.split(":", 1)[1]
            if chosen_sale not in ("unsold", "sold"):
                chosen_sale = "unsold"
            draft["sale_status"] = chosen_sale
            draft["step"] = "pick_work"
            save_draft(d1, user_id, draft, now)

            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "⏳ Pending", "callback_data": "choose_work:pending"},
                        {"text": "✅ Complete", "callback_data": "choose_work:complete"},
                    ]
                ]
            }
            author_val = draft.get("author") or "A"
            api.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": (
                        f"👤 Uploader: <b>{author_val}</b>\n"
                        f"🏷️ Sale Status: <b>{chosen_sale.upper()}</b>\n\n"
                        "<b>Step 3 of 3:</b> Select Processing Status:"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
            )
            return

        # Step 3: Selected Processing Status (pending, complete) -> Commit to D1!
        if data.startswith("choose_work:"):
            chosen_work = data.split(":", 1)[1]
            if chosen_work not in ("pending", "complete"):
                chosen_work = "pending"
            draft["work_status"] = chosen_work

            saved = commit_submission(d1, user_id, draft, now)
            api.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": (
                        f"🎉 <b>Folder Created Successfully!</b>\n\n"
                        f"📁 <b>Folder:</b> {saved['label']}\n"
                        f"📧 <b>Email:</b> <code>{saved['email']}</code>\n"
                        f"👤 <b>Uploader:</b> {saved['author']}\n"
                        f"🏷️ <b>Sale:</b> {saved['sale_status'].upper()}\n"
                        f"⚡ <b>Status:</b> {saved['work_status'].upper()}\n"
                        f"🕒 <b>Saved at:</b> {saved['date_ist']} · {saved['time_ist']} IST\n"
                        f"🖼️ <b>Files:</b> 2 photos securely stored in Cloudflare D1\n\n"
                        f"🌐 <i>Viewable instantly on your Vercel Dashboard!</i>"
                    ),
                    "parse_mode": "HTML",
                },
            )
            return


def serve_health() -> None:
    port = int(os.environ.get("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args):
            return

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        server.serve_forever()
    except Exception as exc:
        print(f"[-] Health server error: {exc}")


def main() -> None:
    load_env()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    account_id = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    api_token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    database_id = (os.environ.get("CLOUDFLARE_D1_DATABASE_ID") or "").strip()
    passcode = (os.environ.get("APP_PASSCODE") or os.environ.get("PASSCODE") or "2026").strip()

    if not token:
        print("[-] TELEGRAM_BOT_TOKEN is missing in .env")
        return
    if not all([account_id, api_token, database_id]):
        print("[-] Cloudflare D1 environment variables missing in .env")
        return

    owners = owner_ids(os.environ.get("TELEGRAM_OWNER_IDS") or "")

    threading.Thread(target=serve_health, daemon=True).start()
    api = Telegram(token)
    me = api.call("getMe", timeout=20)["result"]
    d1 = D1(account_id, api_token, database_id)
    d1.init()

    # Ensure master owners from config are pre-seeded in D1
    for o_id in owners:
        if not d1.is_user_authorized(o_id):
            d1.authorize_user(o_id, "master_owner", "Master Admin")

    print("=" * 60)
    print(f"[+] Telegram Bot Connected: @{me.get('username') or 'bot'}")
    print(f"[+] Master Owners: {', '.join(str(item) for item in sorted(owners))}")
    print(f"[+] Security Passcode Configured: {'*' * len(passcode)}")
    print("[+] Cloudflare D1 Database Initialized: 'doc-dustbin'")
    print("[*] Intake Loop Active: Listening for photos, email, and inline selections...")
    print("=" * 60)

    while True:
        try:
            for update in api.updates():
                try:
                    if "message" in update:
                        handle_message(api, d1, owners, update["message"], passcode)
                    elif "callback_query" in update:
                        handle_callback(api, d1, owners, update["callback_query"])
                except Exception as exc:
                    print(f"[-] Update handling error: {exc}")
        except KeyboardInterrupt:
            print("\n[*] Bot stopped by user.")
            return
        except requests.RequestException as exc:
            print(f"[-] Network connection issue: {exc}")
            time.sleep(3)
        except Exception as exc:
            print(f"[-] Unexpected poll exception: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
