import os
import json
import logging
import asyncio
import threading
from flask import Flask, request, jsonify, send_from_directory
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
ADMIN_ID      = int(os.environ["ADMIN_ID"])
EVAL_GROUP_ID = int(os.environ["EVAL_GROUP_ID"])
WEBAPP_URL    = os.environ["WEBAPP_URL"].rstrip("/")
DB_FILE       = "db.json"

# Telegram bot file size limit: 50 MB
MAX_FILE_BYTES = 50 * 1024 * 1024

flask_app = Flask(__name__)

# ── DB ────────────────────────────────────────────────────────────────────────
def load_db():
    try:
        with open(DB_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "students": {},
            "current_question": None,
            "submissions": {},
            "rated": {}
        }

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

# ── HTML escape helper ────────────────────────────────────────────────────────
def he(text) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))

# ── Dedicated PTB event loop (one loop, one thread, forever) ─────────────────
ptb_app = Application.builder().token(BOT_TOKEN).build()
_ptb_loop: asyncio.AbstractEventLoop | None = None
_ptb_loop_lock = threading.Lock()

def get_ptb_loop() -> asyncio.AbstractEventLoop:
    global _ptb_loop
    with _ptb_loop_lock:
        if _ptb_loop is None or _ptb_loop.is_closed():
            _ptb_loop = asyncio.new_event_loop()
        return _ptb_loop

def run_in_ptb_loop(coro):
    """Schedule coroutine on PTB loop from any thread; block until result."""
    future = asyncio.run_coroutine_threadsafe(coro, get_ptb_loop())
    return future.result(timeout=30)

def _run_ptb_loop_forever():
    loop = get_ptb_loop()
    asyncio.set_event_loop(loop)
    loop.run_forever()

# ── Shared rating keyboard builder ───────────────────────────────────────────
def rating_kb(student_id):
    labels = ["⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(labels[i], callback_data=f"rate:{student_id}:{i+1}")
        for i in range(5)
    ]])

# ── Shared submission guard ───────────────────────────────────────────────────
def check_submission_allowed(student_id, db):
    """Returns (allowed: bool, error_key: str|None)"""
    if not db.get("current_question"):
        return False, "No active question"
    if str(student_id) in db.get("submissions", {}):
        return False, "already_submitted"
    return True, None

def register_submission(student_id, student_name, username, db):
    db["students"][str(student_id)] = {"name": student_name, "username": username}
    db.setdefault("submissions", {})[str(student_id)] = True
    save_db(db)

# ── FLASK ROUTES ──────────────────────────────────────────────────────────────

@flask_app.route("/")
def index():
    return send_from_directory("static", "index.html")

@flask_app.route("/api/question")
def get_question():
    db = load_db()
    return jsonify({"question": db.get("current_question")})

# ── Text submission (JSON) ────────────────────────────────────────────────────
@flask_app.route("/api/submit", methods=["POST"])
def submit_text():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    student_id   = data.get("student_id")
    student_name = data.get("student_name", "Unknown")
    username     = data.get("username", "")
    answer_text  = data.get("answer", "").strip()
    answer_type  = data.get("type", "text")   # "text" or "note"

    if not student_id or not answer_text:
        return jsonify({"ok": False, "error": "Missing fields"}), 400

    db = load_db()
    allowed, err = check_submission_allowed(student_id, db)
    if not allowed:
        return jsonify({"ok": False, "error": err}), 400

    register_submission(student_id, student_name, username, db)

    question = db["current_question"]
    uname    = f"@{username}" if username else student_name
    mode_label = "📝 Notes/Points" if answer_type == "note" else "✍️ Text"

    caption = (
        f"📨 <b>New Answer!</b> {mode_label}\n\n"
        f"👤 {he(student_name)} ({he(uname)})\n"
        f"🆔 <code>{he(student_id)}</code>\n"
        f"❓ {he(question)}\n\n"
        f"💬 <b>Answer:</b>\n{he(answer_text)}"
    )

    try:
        run_in_ptb_loop(ptb_app.bot.send_message(
            chat_id=EVAL_GROUP_ID,
            text=caption,
            parse_mode="HTML",
            reply_markup=rating_kb(student_id)
        ))
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error sending text submission to eval group: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── File submission (multipart/form-data) ─────────────────────────────────────
@flask_app.route("/api/submit-file", methods=["POST"])
def submit_file():
    student_id   = request.form.get("student_id", "").strip()
    student_name = request.form.get("student_name", "Unknown").strip()
    username     = request.form.get("username", "").strip()
    answer_type  = request.form.get("type", "video")  # "video" or "audio"
    file_obj     = request.files.get("file")

    if not student_id:
        return jsonify({"ok": False, "error": "Missing student_id"}), 400
    if not file_obj or file_obj.filename == "":
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    # Read file into memory and check size
    file_bytes = file_obj.read()
    if len(file_bytes) > MAX_FILE_BYTES:
        mb = len(file_bytes) / (1024 * 1024)
        return jsonify({
            "ok": False,
            "error": f"File too large ({mb:.1f} MB). Maximum allowed: 50 MB."
        }), 400

    db = load_db()
    allowed, err = check_submission_allowed(student_id, db)
    if not allowed:
        return jsonify({"ok": False, "error": err}), 400

    register_submission(student_id, student_name, username, db)

    question   = db["current_question"]
    uname      = f"@{username}" if username else student_name
    mode_label = "🎥 Video" if answer_type == "video" else "🎙️ Audio"

    caption = (
        f"📨 <b>New Answer!</b> {mode_label}\n\n"
        f"👤 {he(student_name)} ({he(uname)})\n"
        f"🆔 <code>{he(student_id)}</code>\n"
        f"❓ {he(question)}"
    )

    try:
        if answer_type == "video":
            run_in_ptb_loop(ptb_app.bot.send_video(
                chat_id=EVAL_GROUP_ID,
                video=file_bytes,
                caption=caption,
                parse_mode="HTML",
                reply_markup=rating_kb(student_id),
                supports_streaming=True
            ))
        else:
            run_in_ptb_loop(ptb_app.bot.send_audio(
                chat_id=EVAL_GROUP_ID,
                audio=file_bytes,
                caption=caption,
                parse_mode="HTML",
                reply_markup=rating_kb(student_id)
            ))
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error sending file submission to eval group: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Telegram webhook ──────────────────────────────────────────────────────────
@flask_app.route("/api/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return "bad request", 400
    update = Update.de_json(data, ptb_app.bot)
    asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), get_ptb_loop())
    return "ok"

# ── BOT HANDLERS ──────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db = load_db()
    if str(user.id) not in db["students"]:
        db["students"][str(user.id)] = {
            "name": user.full_name,
            "username": user.username or ""
        }
        save_db(db)
    await update.message.reply_text(
        f"🎓 <b>Pareeksha Gurukul</b>\n\n"
        f"Welcome, {he(user.first_name)}! 🙏\n\n"
        f"You will be notified when the admin posts a new question.\n"
        f"Stay ready! ✊",
        parse_mode="HTML"
    )

async def ask_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/ask Write your question here</code>", parse_mode="HTML"
        )
        return

    question = " ".join(ctx.args)
    db = load_db()
    db["current_question"] = question
    db["submissions"] = {}
    db["rated"] = {}
    save_db(db)

    if not db["students"]:
        await update.message.reply_text("⚠️ No students are registered yet.")
        return

    webapp_button = InlineKeyboardMarkup([[
        InlineKeyboardButton("📝 Submit Answer", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])

    sent = failed = 0
    for uid in db["students"]:
        try:
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=(
                    f"📢 <b>New Question!</b>\n\n"
                    f"❓ {he(question)}\n\n"
                    f"Tap the button below to submit your answer 👇\n"
                    f"<i>(You can answer via Text, Video, or Audio)</i>"
                ),
                parse_mode="HTML",
                reply_markup=webapp_button
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Could not send to {uid}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Question sent!\n• Sent: {sent}\n• Failed: {failed}"
    )

async def handle_rating(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid data.")
        return

    student_id = parts[1]
    stars      = int(parts[2])

    # Prevent double-rating
    db = load_db()
    if str(student_id) in db.get("rated", {}):
        await query.answer("This answer has already been rated! ✅")
        return

    db.setdefault("rated", {})[str(student_id)] = stars
    save_db(db)

    await query.answer(f"Rating saved: {'⭐' * stars}")
    await query.edit_message_reply_markup(reply_markup=None)

    star_str  = "⭐" * stars
    evaluator = he(query.from_user.full_name)

    await query.message.reply_text(
        f"✅ <b>Rating Done!</b>\n{star_str} ({stars}/5)\nBy: {evaluator}",
        parse_mode="HTML"
    )

    score_messages = {
        1: "Needs more practice and effort. Don't give up! 💪",
        2: "More preparation needed. Keep going! 📚",
        3: "Good answer! There's room for improvement. 👍",
        4: "Great answer! You're on the right track. 🌟",
        5: "Outstanding! A perfect answer! 🏆"
    }

    try:
        await ctx.bot.send_message(
            chat_id=int(student_id),
            text=(
                f"🎯 <b>Your Score is Here!</b>\n\n"
                f"Rating: {star_str} ({stars}/5)\n\n"
                f"<i>{he(score_messages[stars])}</i>"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Could not DM score to {student_id}: {e}")

async def reset_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db = load_db()
    db["current_question"] = None
    db["submissions"] = {}
    db["rated"] = {}
    save_db(db)
    await update.message.reply_text(
        "🔄 Question has been reset. Use /ask to send a new question."
    )

async def list_students(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db = load_db()
    if not db["students"]:
        await update.message.reply_text("No students are registered yet.")
        return
    lines = ["📋 <b>Registered Students:</b>\n"]
    for uid, info in db["students"].items():
        uname = f"@{info['username']}" if info['username'] else "—"
        lines.append(f"• {he(info['name'])} ({he(uname)})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        text = (
            "🛠 <b>Admin Commands:</b>\n\n"
            "<code>/ask Write your question here</code> — Broadcast a question\n"
            "<code>/students</code> — View registered students\n"
            "<code>/reset</code> — Reset question and submissions\n"
            "<code>/help</code> — This message"
        )
    else:
        text = (
            "📖 <b>Help:</b>\n\n"
            "• Wait for the admin to post a question\n"
            "• Tap the '📝 Submit Answer' button\n"
            "• Answer via Text, Video, or Audio\n"
            "• Your score will be sent to your Telegram DM"
        )
    await update.message.reply_text(text, parse_mode="HTML")

# ── Register handlers ─────────────────────────────────────────────────────────
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("ask", ask_question))
ptb_app.add_handler(CommandHandler("students", list_students))
ptb_app.add_handler(CommandHandler("reset", reset_question))
ptb_app.add_handler(CommandHandler("help", help_cmd))
ptb_app.add_handler(CallbackQueryHandler(handle_rating, pattern=r"^rate:"))

# ── Startup: launch PTB loop, initialize bot, set webhook ────────────────────
threading.Thread(target=_run_ptb_loop_forever, daemon=True, name="ptb-loop").start()
run_in_ptb_loop(ptb_app.initialize())
run_in_ptb_loop(ptb_app.bot.set_webhook(f"{WEBAPP_URL}/api/webhook"))
logger.info(f"✅ Bot ready. Webhook: {WEBAPP_URL}/api/webhook")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
