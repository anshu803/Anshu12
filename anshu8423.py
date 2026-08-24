import os
import sqlite3
import logging
import threading
from datetime import datetime
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot
from telebot import types

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("code_store_bot")

# ====== CONFIG (hardcoded — fill these in, or set as env vars on Render) ======
BOT_TOKEN = os.getenv("8955958124:AAFN0RzF9RgUoaDvMa8E2mxq45grDExT94A", "8955958124:AAFN0RzF9RgUoaDvMa8E2mxq45grDExT94A")        # e.g. "123456789:AAExampleTokenFromBotFather"
CHANNEL_ID = os.getenv("1004309680225", "-1004309680225")     # e.g. "-1001234567890" (your private admin/orders channel)
ADMIN_ID = int(os.getenv("6644342214", "6644342214"))                            # e.g. 5551234567 — your numeric Telegram user id

UPI_ID = os.getenv("8303721228@ibl", "8303721228@ibl")                          # your real UPI ID shown to buyers
QR_IMAGE_PATH = os.getenv("QR_IMAGE_PATH", "")                        # optional: path to a QR code image, e.g. "qr.jpg" — leave "" to skip

if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    raise ValueError("Set BOT_TOKEN at the top of the script (or as an env var).")
if not CHANNEL_ID or CHANNEL_ID == "PUT_YOUR_CHANNEL_ID_HERE":
    raise ValueError("Set CHANNEL_ID at the top of the script (or as an env var).")
if not ADMIN_ID:
    logger.warning("ADMIN_ID is not set — admin commands from private chat will be unusable (channel commands still work).")

# ====== PRODUCTS (ALL ₹30) ======
PRODUCTS = {
    "py": {"name": "🐍 Python Script", "price_inr": 30},
    "java": {"name": "☕ Java Program", "price_inr": 30},
    "cpp": {"name": "⚙️ C++ Code", "price_inr": 30},
    "js": {"name": "🟨 JavaScript Code", "price_inr": 30},
    "html": {"name": "🌐 HTML/CSS Website", "price_inr": 30},
    "php": {"name": "🐘 PHP Script", "price_inr": 30},
}

# ====== DATABASE ======
DB_PATH = os.getenv("DB_PATH", "orders.db")
DB_LOCK = threading.Lock()  # telebot runs handlers in threads, so writes need locking


@contextmanager
def db_connect():
    """sqlite3's own context manager only commits/rolls back — it never
    closes the connection. Wrap it so every call properly closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def db_init():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                product_key TEXT NOT NULL,
                product_name TEXT NOT NULL,
                amount_inr INTEGER NOT NULL,
                description TEXT,
                screenshot_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'awaiting_description',
                created_at TEXT NOT NULL,
                description_at TEXT,
                screenshot_at TEXT,
                fulfilled_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_files (
                product_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_name TEXT,
                set_by INTEGER,
                set_at TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'file'
            )
            """
        )
        # Backward-compatible: add 'kind' column if this DB was created before this feature existed.
        try:
            conn.execute("ALTER TABLE product_files ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    logger.info("Database ready at %s", DB_PATH)


# ---- orders ----

def db_create_order(user, product_key: str, product_name: str, amount_inr: int) -> int:
    with DB_LOCK, db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (
                user_id, username, full_name, product_key, product_name,
                amount_inr, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_description', ?)
            """,
            (
                user.id,
                user.username or "",
                (user.first_name or "") + (f" {user.last_name}" if user.last_name else ""),
                product_key,
                product_name,
                amount_inr,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.lastrowid


def db_get_order_awaiting_description(user_id: int):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_description' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def db_get_order_awaiting_payment(user_id: int):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_payment' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def db_save_description(order_id: int, description: str):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET description=?, status='awaiting_payment', description_at=? WHERE id=?",
            (description, datetime.utcnow().isoformat(timespec="seconds"), order_id),
        )
        conn.commit()


def db_mark_screenshot_received(order_id: int, file_id: str):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='screenshot_sent', screenshot_file_id=?, screenshot_at=? WHERE id=?",
            (file_id, datetime.utcnow().isoformat(timespec="seconds"), order_id),
        )
        conn.commit()


def db_mark_fulfilled(order_id: int):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='fulfilled', fulfilled_at=? WHERE id=?",
            (datetime.utcnow().isoformat(timespec="seconds"), order_id),
        )
        conn.commit()


def db_get_order(order_id: int):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None


def db_orders_by_status(status: str, limit: int = 20):
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---- product files (code library) ----

def db_set_product_file(product_key: str, file_id: str, file_name: str, set_by: int, kind: str = "file"):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO product_files (product_key, file_id, file_name, set_by, set_at, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_key) DO UPDATE SET
                file_id=excluded.file_id,
                file_name=excluded.file_name,
                set_by=excluded.set_by,
                set_at=excluded.set_at,
                kind=excluded.kind
            """,
            (product_key, file_id, file_name, set_by, datetime.utcnow().isoformat(timespec="seconds"), kind),
        )
        conn.commit()


def db_get_product_file(product_key: str):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT * FROM product_files WHERE product_key=?", (product_key,)).fetchone()
        return dict(row) if row else None


def db_list_product_files():
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute("SELECT * FROM product_files").fetchall()
        return {r["product_key"]: dict(r) for r in rows}


# ====== INIT BOT ======
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")


def esc(text: str) -> str:
    """Escape text so it can't break Markdown formatting (user-controlled text)."""
    if text is None:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def send_payment_instructions(chat_id: int, order_id: int, product: dict):
    caption = (
        f"🧾 *Order #{order_id} confirm ho gaya!*\n\n"
        f"📦 *Aapne liya:* {esc(product['name'])}\n"
        f"💰 *Pay karna hai:* ₹{product['price_inr']}\n\n"
        f"📖 *Ab yeh 2 steps follow karo:*\n"
        f"1️⃣ Neeche di gayi UPI ID par ₹{product['price_inr']} pay karo:\n"
        f"   💳 `{UPI_ID}`\n"
        f"2️⃣ Payment ho jaye toh *screenshot* isi chat mein bhej do 📸\n\n"
        f"⏳ Screenshot bhejne ke baad, aapka code *24 ghante ke andar* mil jayega. Dhanyavaad! 🙏"
    )
    if QR_IMAGE_PATH and os.path.isfile(QR_IMAGE_PATH):
        with open(QR_IMAGE_PATH, "rb") as qr:
            bot.send_photo(chat_id, qr, caption=caption)
    else:
        bot.send_message(chat_id, caption)


# ====== /start ======
@bot.message_handler(commands=["start"])
def start(message: types.Message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🛍️ Codes Dekho", callback_data="view_products"))
    bot.send_message(
        message.chat.id,
        "⚡️ *A N S H U   C O D E   S T O R E* ⚡️\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "💻 *Premium code, instant delivery.*\n"
        "🏷️ *Har code sirf* ₹{price} *mein!*\n\n"
        "🗂️ *Available:*\n"
        "🐍 Python   ☕ Java   ⚙️ C++\n"
        "🟨 JavaScript   🌐 HTML/CSS   🐘 PHP\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📌 *Kaise order kare:*\n"
        "1️⃣ Language choose karo\n"
        "2️⃣ Kaisa code chahiye likho\n"
        "3️⃣ Payment + screenshot bhejo\n"
        "✅ 24 ghante mein code ready!\n\n"
        "👇 *Shuru karo:*".format(price=next(iter(PRODUCTS.values()))["price_inr"]),
        reply_markup=markup,
    )


# ====== VIEW PRODUCTS ======
@bot.callback_query_handler(func=lambda c: c.data == "view_products")
def show_products(call: types.CallbackQuery):
    markup = types.InlineKeyboardMarkup()
    for key, product in PRODUCTS.items():
        markup.add(
            types.InlineKeyboardButton(
                f"🛒 {product['name']} — ₹{product['price_inr']}",
                callback_data=f"buy_{key}",
            )
        )
    markup.add(types.InlineKeyboardButton("◀️ Back", callback_data="back_to_start"))
    try:
        bot.edit_message_text(
            "🗂️ *Available Codes*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "👇 *Jo language chahiye us par tap karo:*",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
        )
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise
    bot.answer_callback_query(call.id)


# ====== BUY — ask what kind of code they want ======
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def buy_product(call: types.CallbackQuery):
    product_key = call.data.split("_", 1)[1]
    product = PRODUCTS.get(product_key)
    if not product:
        bot.answer_callback_query(call.id, "❌ Product not found!", show_alert=True)
        return

    user = call.from_user
    try:
        order_id = db_create_order(user, product_key, product["name"], product["price_inr"])
    except Exception:
        logger.exception("Failed to create order for user %s / product %s", user.id, product_key)
        bot.answer_callback_query(call.id, "❌ Something went wrong. Please try again.", show_alert=True)
        return

    try:
        bot.send_message(
            call.message.chat.id,
            f"✅ *{esc(product['name'])}* select ho gaya!\n\n"
            f"✍️ *Ab bas ek line mein likh do* — aapko kaisa code chahiye?\n\n"
            f"👉 Example: _'login form website'_, _'student management system'_, "
            f"_'calculator app'_ — jo bhi chahiye wahi likho 👇",
            reply_markup=types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("Failed to prompt for description on order %s", order_id)
        bot.answer_callback_query(call.id, "❌ Something went wrong. Please try again.", show_alert=True)


# ====== TEXT MESSAGES (description step + fallback) ======
@bot.message_handler(content_types=["text"], func=lambda m: not (m.text and m.text.startswith("/")))
def handle_text(message: types.Message):
    user = message.from_user
    order = db_get_order_awaiting_description(user.id)

    if order:
        description = message.text.strip()
        try:
            db_save_description(order["id"], description)
        except Exception:
            logger.exception("Failed to save description for order %s", order["id"])

        product = PRODUCTS.get(order["product_key"], {"name": order["product_name"], "price_inr": order["amount_inr"]})
        send_payment_instructions(message.chat.id, order["id"], product)
        return

    # No active order waiting on text — gentle nudge
    bot.reply_to(
        message,
        "👋 *Code order karne ke liye:*\n\n"
        "1️⃣ /start bhejo\n"
        "2️⃣ Jo language chahiye wo choose karo\n"
        "3️⃣ Payment karke screenshot bhejo\n\n"
        "Bas itna hi! 😊",
    )


# ====== SCREENSHOT RECEIVED ======
@bot.message_handler(content_types=["photo"])
def handle_screenshot(message: types.Message):
    user = message.from_user
    order = db_get_order_awaiting_payment(user.id)

    if not order:
        pending_desc = db_get_order_awaiting_description(user.id)
        if pending_desc:
            bot.reply_to(
                message,
                "✍️ *Ruko zara!*\n\n"
                "Pehle likh kar batao ki aapko kaisa code chahiye (jaise: _'calculator app'_), "
                "uske baad hi payment screenshot bhejna. 🙏",
            )
        else:
            bot.reply_to(
                message,
                "🤔 *Aapka koi order shuru nahi hua hai.*\n\n"
                "Pehle yeh karo:\n"
                "1️⃣ /start bhejo\n"
                "2️⃣ Language choose karo\n"
                "3️⃣ Code ka description likho\n"
                "4️⃣ Payment karke screenshot bhejo\n\n"
                "Fir sab sahi chalega! 😊",
            )
        return

    file_id = message.photo[-1].file_id  # highest resolution
    try:
        db_mark_screenshot_received(order["id"], file_id)
    except Exception:
        logger.exception("Failed to update order %s with screenshot", order["id"])

    username = user.username or "NoUsername"
    full_name = (user.first_name or "") + (f" {user.last_name}" if user.last_name else "") or "Unknown"

    caption = (
        "🧾 *PAYMENT SCREENSHOT RECEIVED* 🧾\n"
        "─────────────────────────\n"
        f"🆔 *Order ID:* `{order['id']}`\n"
        f"👤 *User ID:* `{user.id}`\n"
        f"👤 *Username:* @{esc(username)}\n"
        f"👤 *Name:* {esc(full_name)}\n"
        f"📦 *Code:* {esc(order['product_name'])}\n"
        f"📝 *Wants:* {esc(order['description'] or 'Not specified')}\n"
        f"💰 *Amount:* ₹{order['amount_inr']}\n"
        f"🕒 *Time:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        "─────────────────────────\n"
        f"📌 Verify payment, then:\n"
        f"`/approve {order['id']}` — sends the stored {esc(order['product_key'])} file automatically\n"
        f"(If no file is set yet for {esc(order['product_key'])}, use `/setcode {esc(order['product_key'])}` first.)"
    )
    try:
        bot.forward_message(CHANNEL_ID, message.chat.id, message.message_id)
        bot.send_message(CHANNEL_ID, caption)
    except Exception:
        logger.exception("Failed to forward screenshot / report to channel %s", CHANNEL_ID)

    bot.reply_to(
        message,
        "✅ *Screenshot mil gaya, shukriya!* 🙏\n\n"
        "Ab hum aapka payment check karenge. Verify hote hi, "
        "aapka code *24 ghante ke andar* isi chat mein bhej diya jayega. ❤️\n\n"
        "Koi jaldi nahi — bas thoda intezaar karo! 😊",
    )


# ====== BACK ======
@bot.callback_query_handler(func=lambda c: c.data == "back_to_start")
def back_to_start(call: types.CallbackQuery):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🛍️ Codes Dekho", callback_data="view_products"))
    try:
        bot.edit_message_text(
            "⚡️ *A N S H U   C O D E   S T O R E* ⚡️\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "💻 *Premium code, instant delivery.*\n"
            "🏷️ *Har code sirf* ₹{price} *mein!*\n"
            "💡 *Languages:* Python, Java, C++, JS, HTML/CSS, PHP\n\n"
            "👇 *Shuru karo:*".format(price=next(iter(PRODUCTS.values()))["price_inr"]),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
        )
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise
    bot.answer_callback_query(call.id)


# ====== ADMIN AUTHORIZATION ======

def _is_authorized_channel_command(message: types.Message) -> bool:
    """Channel posts are trusted because only channel admins can post there."""
    try:
        return str(message.chat.id) == str(CHANNEL_ID)
    except Exception:
        return False


def _is_admin_private_chat(message: types.Message) -> bool:
    """Direct messages from the configured ADMIN_ID are trusted too."""
    try:
        return bool(ADMIN_ID) and message.from_user is not None and message.from_user.id == ADMIN_ID
    except Exception:
        return False


def _authorized(message: types.Message) -> bool:
    return _is_authorized_channel_command(message) or _is_admin_private_chat(message)


# ====== ADMIN ACTIONS ======

def _do_listcodes(reply_target: types.Message):
    files = db_list_product_files()
    lines = ["📁 *Registered code files:*\n"]
    for key, product in PRODUCTS.items():
        if key in files:
            f = files[key]
            if f.get("kind") == "link":
                lines.append(f"🔗 `{key}` — {esc(product['name'])} — link set")
            else:
                lines.append(f"✅ `{key}` — {esc(product['name'])} — {esc(f['file_name'] or 'file')}")
        else:
            lines.append(f"❌ `{key}` — {esc(product['name'])} — not set")
    bot.reply_to(reply_target, "\n".join(lines))


def _do_orders(reply_target: types.Message):
    orders = db_orders_by_status("screenshot_sent")
    if not orders:
        bot.reply_to(reply_target, "✅ No orders waiting on you.")
        return
    lines = ["📋 *Orders awaiting fulfillment:*\n"]
    for o in orders:
        lines.append(
            f"`{o['id']}` — {esc(o['product_name'])} — {esc(o['description'] or 'no description')} — "
            f"user `{o['user_id']}` (@{esc(o['username'] or 'NoUsername')}) — {o['screenshot_at']}"
        )
    bot.reply_to(reply_target, "\n".join(lines))


def _do_approve(order_id_str: str, reply_target: types.Message):
    try:
        order_id = int(order_id_str)
    except ValueError:
        bot.reply_to(reply_target, "❌ order_id must be a number. Usage: /approve <order_id>")
        return

    order = db_get_order(order_id)
    if not order:
        bot.reply_to(reply_target, "❌ Order not found.")
        return
    if order["status"] == "fulfilled":
        bot.reply_to(reply_target, "⚠️ Order already fulfilled.")
        return

    product_file = db_get_product_file(order["product_key"])
    if not product_file:
        bot.reply_to(
            reply_target,
            f"❌ No code file registered for `{order['product_key']}` yet.\n"
            f"Upload the file (or send the link) in the channel with caption `{order['product_key']}` first, then run /approve again.",
        )
        return

    try:
        if product_file.get("kind") == "link":
            bot.send_message(
                chat_id=order["user_id"],
                text=(
                    "✅ Your code is ready!\n\n"
                    f"👉 Download link: {product_file['file_id']}\n\n"
                    "Thank you for your purchase ❤️"
                ),
                parse_mode=None,
                disable_web_page_preview=False,
            )
        else:
            bot.send_document(
                chat_id=order["user_id"],
                document=product_file["file_id"],
                caption="✅ *Your code is ready!*\n\nThank you for your purchase ❤️",
            )
        db_mark_fulfilled(order_id)
        bot.reply_to(reply_target, f"✅ Order {order_id} approved and code sent to user {order['user_id']}.")
    except Exception as e:
        # Most common cause: the buyer has blocked the bot or never started a chat with it,
        # so Telegram refuses to deliver the message. Order stays un-fulfilled so it can be retried.
        logger.exception("Failed to send code to user %s for order %s", order["user_id"], order_id)
        bot.reply_to(
            reply_target,
            f"❌ Order {order_id}: could not deliver the code to user {order['user_id']} ({e}).\n"
            f"They may have blocked the bot or never pressed /start. Order was NOT marked fulfilled.",
        )


def _do_setcode_link(product_key: str, link: str, set_by: int, reply_target: types.Message):
    if product_key not in PRODUCTS:
        bot.reply_to(reply_target, f"❌ Unknown product key. Valid: {', '.join(PRODUCTS.keys())}")
        return
    try:
        db_set_product_file(product_key, link, None, set_by, kind="link")
        bot.reply_to(reply_target, f"✅ Link registered for `{product_key}`.")
    except Exception:
        logger.exception("Failed to set product link for %s", product_key)
        bot.reply_to(reply_target, "❌ Failed to save link.")


def _do_setcode_document(product_key: str, document, set_by: int, reply_target: types.Message):
    if product_key not in PRODUCTS:
        bot.reply_to(reply_target, f"❌ Unknown product key. Valid: {', '.join(PRODUCTS.keys())}")
        return
    try:
        db_set_product_file(product_key, document.file_id, document.file_name, set_by, kind="file")
        bot.reply_to(reply_target, f"✅ File registered for `{product_key}`.")
    except Exception:
        logger.exception("Failed to set product file for %s", product_key)
        bot.reply_to(reply_target, "❌ Failed to save file.")


# ====== ADMIN COMMANDS — work from the channel AND from the admin's private chat ======

@bot.message_handler(commands=["approve"], func=_authorized)
@bot.channel_post_handler(commands=["approve"], func=_authorized)
def cmd_approve(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /approve <order_id>")
        return
    _do_approve(parts[1].strip(), message)


@bot.message_handler(commands=["orders"], func=_authorized)
@bot.channel_post_handler(commands=["orders"], func=_authorized)
def cmd_orders(message: types.Message):
    _do_orders(message)


@bot.message_handler(commands=["listcodes"], func=_authorized)
@bot.channel_post_handler(commands=["listcodes"], func=_authorized)
def cmd_listcodes(message: types.Message):
    _do_listcodes(message)


@bot.message_handler(commands=["setcode"], func=_authorized)
@bot.channel_post_handler(commands=["setcode"], func=_authorized)
def cmd_setcode(message: types.Message):
    """
    Usage:
      /setcode <key> <link>            — register a download link
      (reply to a document) /setcode <key>  — register the replied-to file
    """
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Usage:\n"
            "`/setcode <key> <link>`\n"
            "or reply to a document with `/setcode <key>`\n\n"
            f"Valid keys: {', '.join(PRODUCTS.keys())}",
        )
        return

    product_key = parts[1].strip().lower()
    set_by = message.from_user.id if message.from_user else 0

    if message.reply_to_message and message.reply_to_message.document:
        _do_setcode_document(product_key, message.reply_to_message.document, set_by, message)
        return

    if len(parts) >= 3:
        _do_setcode_link(product_key, parts[2].strip(), set_by, message)
        return

    bot.reply_to(message, "❌ Provide a link after the key, or reply to a document with this command.")


@bot.channel_post_handler(content_types=["document"])
def channel_document_upload(message: types.Message):
    """Lets an admin register a code file just by posting it in the channel
    with the product key (e.g. 'py') as the caption — no command needed."""
    if not _is_authorized_channel_command(message):
        return
    caption = (message.caption or "").strip().lower()
    if caption not in PRODUCTS:
        return  # not a product-file upload — ignore silently
    set_by = message.from_user.id if message.from_user else 0
    _do_setcode_document(caption, message.document, set_by, message)


# ====== RENDER WEB-SERVICE PORT FIX ======
# Render's "Web Service" type waits for the app to bind an HTTP port and will
# flag the deploy as unhealthy if none is found. This bot only does Telegram
# long-polling and never opens a port on its own, so we run a tiny dummy HTTP
# server in the background purely to satisfy Render's port scan.
# (If you deploy this as a Render "Background Worker" instead, PORT won't be
# set and this is harmless — it just binds a default port that nothing checks.)

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # silence default request logging, keep it out of the bot's own logs


def _run_health_server():
    port = int(os.getenv("PORT", 10000))
    try:
        HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()
    except Exception:
        logger.exception("Health server failed to start on port %s", port)


# ====== MAIN ======

def main():
    db_init()
    threading.Thread(target=_run_health_server, daemon=True).start()
    logger.info("Bot starting...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
