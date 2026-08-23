import os
import sqlite3
import logging
import threading
from datetime import datetime

import telebot
from telebot import types

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("code_store_bot")

# ====== CONFIG (hardcoded — fill these in) ======
BOT_TOKEN = "8658924202:AAGlaypnLN7XtUi4_BPLy6axRrgE_nFOZOA"        # e.g. "123456789:AAExampleTokenFromBotFather"
CHANNEL_ID = "-1004309680225"      # e.g. "-1001234567890" (your private admin/orders channel)
ADMIN_ID = 6644342214                                  # e.g. 5551234567 — your numeric Telegram user id

UPI_ID = "8303721228@ibl"                       # your real UPI ID shown to buyers
QR_IMAGE_PATH = ""                             # optional: path to a QR code image, e.g. "qr.jpg" — leave "" to skip

if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    raise ValueError("Set BOT_TOKEN at the top of the script.")
if not CHANNEL_ID or CHANNEL_ID == "PUT_YOUR_CHANNEL_ID_HERE":
    raise ValueError("Set CHANNEL_ID at the top of the script.")
if not ADMIN_ID:
    logger.warning("ADMIN_ID is not set — admin commands will be unusable.")

# ====== PRODUCTS (ALL ₹50) ======
PRODUCTS = {
    "py": {"name": "🐍 Python Script", "price_inr": 20},
    "java": {"name": "☕ Java Program", "price_inr": 25},
    "cpp": {"name": "⚙️ C++ Code", "price_inr": 30},
    "js": {"name": "🟨 JavaScript Code", "price_inr": 20},
    "html": {"name": "🌐 HTML/CSS Website", "price_inr": 40},
    "php": {"name": "🐘 PHP Script", "price_inr": 45},
}

# ====== DATABASE ======
DB_PATH = os.getenv("DB_PATH", "orders.db")
DB_LOCK = threading.Lock()  # telebot runs handlers in threads, so writes need locking


from contextlib import contextmanager


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
    markup.add(types.InlineKeyboardButton("📚 Code Dekho / View Codes", callback_data="view_products"))
    bot.send_message(
        message.chat.id,
        "🛒 *Anshu Ethical Code Store mein aapka swagat hai!*\n\n"
        "📌 *Har code ka price:* ₹50\n\n"
        "💡 *Yeh languages available hain:*\n"
        "• Python 🐍\n• Java ☕\n• C++ ⚙️\n• JavaScript 🟨\n• HTML/CSS 🌐\n• PHP 🐘\n\n"
        "📖 *Order kaise kare (3 simple steps):*\n"
        "1️⃣ Neeche button dabao aur language choose karo\n"
        "2️⃣ Kaisa code chahiye likh kar bhejo (jaise: _login form_, _calculator_)\n"
        "3️⃣ Payment karke screenshot bhejo — 24 ghante mein code mil jayega ✅\n\n"
        "👇 Shuru karne ke liye neeche button dabao:",
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
            "📚 *Yeh sab codes available hain — sabka price ₹50 hai*\n\n"
            "👇 Jo language chahiye us par tap karo:",
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
    markup.add(types.InlineKeyboardButton("📚 Code Dekho / View Codes", callback_data="view_products"))
    try:
        bot.edit_message_text(
            "🛒 *Anshu Ethical Code Store mein aapka swagat hai!*\n\n"
            "📌 *Har code ka price:* ₹50\n"
            "💡 *Languages:* Python, Java, C++, JavaScript, HTML/CSS, PHP\n\n"
            "👇 Shuru karne ke liye neeche button dabao:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
        )
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise
    bot.answer_callback_query(call.id)


def _is_authorized_channel_command(message: types.Message) -> bool:
    """Channel posts are trusted because only channel admins can post there."""
    try:
        return str(message.chat.id) == str(CHANNEL_ID)
    except Exception:
        return False


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
        logger.exception("Failed to deliver code for order %s", order_id)
        bot.reply_to(reply_target, f"❌ Error sending file: {e}")


def _do_fulfilled(order_id_str: str, reply_target: types.Message):
    try:
        order_id = int(order_id_str)
    except ValueError:
        bot.reply_to(reply_target, "❌ order_id must be a number. Usage: /fulfilled <order_id>")
        return

    order = db_get_order(order_id)
    if not order:
        bot.reply_to(reply_target, "❌ Order not found.")
        return
    if order["status"] == "fulfilled":
        bot.reply_to(reply_target, "⚠️ Order already fulfilled.")
        return

    db_mark_fulfilled(order_id)
    bot.reply_to(reply_target, f"✅ Order {order_id} marked as fulfilled (no file was sent — you handled delivery yourself).")


# ---- channel-post versions: run these commands directly inside your channel ----

@bot.channel_post_handler(commands=["approve"])
def approve_order_channel(message: types.Message):
    if not _is_authorized_channel_command(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /approve <order_id>")
        return
    _do_approve(parts[1].strip(), message)


@bot.channel_post_handler(commands=["fulfilled"])
def mark_fulfilled_channel(message: types.Message):
    if not _is_authorized_channel_command(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /fulfilled <order_id>")
        return
    _do_fulfilled(parts[1].strip(), message)


@bot.channel_post_handler(commands=["listcodes"])
def list_codes_channel(message: types.Message):
    if not _is_authorized_channel_command(message):
        return
    _do_listcodes(message)


@bot.channel_post_handler(commands=["orders"])
def list_orders_channel(message: types.Message):
    if not _is_authorized_channel_command(message):
        return
    _do_orders(message)


# ====== ADMIN: REGISTER A DOWNLOAD LINK FOR A PRODUCT (instead of a file) ======
# In the channel, post a plain text message: "<key> <link>"
# Example: py https://drive.google.com/file/d/xxxxxxx/view
@bot.channel_post_handler(
    content_types=["text"],
    func=lambda m: len((m.text or "").split(maxsplit=1)) == 2
    and (m.text or "").split(maxsplit=1)[0].strip().lower() in PRODUCTS
    and (m.text or "").split(maxsplit=1)[1].strip().lower().startswith(("http://", "https://")),
)
def setlink_via_channel_post(message: types.Message):
    if not _is_authorized_channel_command(message):
        return

    key, link = message.text.split(maxsplit=1)
    key = key.strip().lower()
    link = link.strip()

    try:
        db_set_product_file(key, link, None, 0, kind="link")
        bot.reply_to(
            message,
            f"🔗 *{esc(PRODUCTS[key]['name'])}* ke liye yeh link set ho gaya!\n\n"
            f"Ab jab bhi koi is product ka payment approve hoga, yahi link automatic bhej diya jayega "
            f"(file ki jagah).",
        )
        logger.info("Product link for %s registered via channel post", key)
    except Exception:
        logger.exception("Failed to register product link for %s via channel post", key)
        try:
            bot.reply_to(message, "❌ Kuch galat ho gaya, link save nahi ho paya. Dobara try karo.")
        except Exception:
            pass


# ====== ADMIN: REGISTER A CODE FILE FOR A PRODUCT ======
# Ways to use:
#  1) Post the file directly in your CHANNEL with caption just being the product key, e.g. "py"
#  2) Post plain text in your CHANNEL: "py https://your-link-here" to register a link instead
#  3) Send the file directly to the bot in DM with caption "/setcode py"
#  4) Send the file to the bot in DM first, then reply to that message with "/setcode py"

@bot.channel_post_handler(content_types=["document"])
def setcode_via_channel_post(message: types.Message):
    # Only react to posts in our own configured channel — ignore everything else.
    try:
        if str(message.chat.id) != str(CHANNEL_ID):
            return
    except Exception:
        return

    caption = (message.caption or "").strip()
    if not caption:
        return  # no caption -> nothing to do, ignore silently

    # Accept either a bare key ("py") or "/setcode py"
    key = caption
    if key.lower().startswith("/setcode"):
        parts = key.split(maxsplit=1)
        key = parts[1].strip() if len(parts) > 1 else ""
    key = key.strip().lower()

    if key not in PRODUCTS:
        try:
            bot.reply_to(
                message,
                f"❌ `{esc(key)}` ek valid product key nahi hai.\n"
                f"Valid keys: {', '.join(PRODUCTS.keys())}\n\n"
                f"Caption mein bas yeh likho, jaise: `py`",
            )
        except Exception:
            logger.exception("Failed to reply with invalid-key message in channel")
        return

    try:
        db_set_product_file(key, message.document.file_id, message.document.file_name, 0)
        bot.reply_to(
            message,
            f"✅ *{esc(PRODUCTS[key]['name'])}* ke liye yeh file set ho gayi!\n\n"
            f"Ab jab bhi koi is product ka payment approve hoga, yahi file automatic bhej di jayegi.",
        )
        logger.info("Product file for %s registered via channel post", key)
    except Exception:
        logger.exception("Failed to register product file for %s via channel post", key)
        try:
            bot.reply_to(message, "❌ Kuch galat ho gaya, file save nahi ho payi. Dobara try karo.")
        except Exception:
            pass


@bot.message_handler(content_types=["document"], func=lambda m: m.from_user.id == ADMIN_ID and m.caption and m.caption.strip().startswith("/setcode"))
def setcode_via_caption(message: types.Message):
    parts = message.caption.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: send the file with caption `/setcode <key>` e.g. `/setcode py`")
        return
    key = parts[1].strip()
    if key not in PRODUCTS:
        bot.reply_to(message, f"❌ Unknown product key `{key}`. Valid keys: {', '.join(PRODUCTS.keys())}")
        return
    db_set_product_file(key, message.document.file_id, message.document.file_name, message.from_user.id)
    bot.reply_to(message, f"✅ Code file for `{key}` ({esc(PRODUCTS[key]['name'])}) registered.")


@bot.message_handler(commands=["setcode"])
def setcode_via_reply(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Unauthorized!")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: reply to a document with `/setcode <key>`, or send the file with caption `/setcode <key>`.")
        return
    key = parts[1].strip()
    if key not in PRODUCTS:
        bot.reply_to(message, f"❌ Unknown product key `{key}`. Valid keys: {', '.join(PRODUCTS.keys())}")
        return

    if not message.reply_to_message or not message.reply_to_message.document:
        bot.reply_to(message, "❌ Reply to the message containing the code file with this command.")
        return

    doc = message.reply_to_message.document
    db_set_product_file(key, doc.file_id, doc.file_name, message.from_user.id)
    bot.reply_to(message, f"✅ Code file for `{key}` ({esc(PRODUCTS[key]['name'])}) registered.")


# ====== ADMIN: LIST REGISTERED CODE FILES ======
@bot.message_handler(commands=["listcodes"])
def list_codes(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Unauthorized!")
        return
    _do_listcodes(message)


# ====== ADMIN: LIST ORDERS AWAITING FULFILLMENT ======
@bot.message_handler(commands=["orders"])
def list_orders(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Unauthorized!")
        return
    _do_orders(message)


# ====== ADMIN: APPROVE ORDER — AUTO-SEND THE REGISTERED FILE ======
@bot.message_handler(commands=["approve"])
def approve_order(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Unauthorized!")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /approve <order_id>")
        return
    _do_approve(parts[1].strip(), message)


# ====== ADMIN: MARK FULFILLED MANUALLY (no auto-send) ======
@bot.message_handler(commands=["fulfilled"])
def mark_fulfilled(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Unauthorized!")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /fulfilled <order_id>")
        return
    _do_fulfilled(parts[1].strip(), message)


# ====== ENTRY POINT ======
if __name__ == "__main__":
    db_init()
    logger.info("Bot starting...")
    bot.infinity_polling(skip_pending=True)
