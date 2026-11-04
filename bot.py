import asyncio, re, json, os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from playwright.async_api import async_playwright

BOT_TOKEN = "9105269973:AAFtunQsu9g4roxGUi2RYTt_dSahQdIqlVw"

ACCOUNTS_FILE = "dmapay_accounts.json"
SESSION_DIR = "dmapay_sessions"

ASK_PHONE, ASK_SESSION = range(2)
user_sessions = {}
last_sync_ts = 0

class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Dmapay Bot Active!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), DummyServer)
    server.serve_forever()

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
    accs[uid_str] = {phone: sess_json}
    save_accounts(accs)

def generate_user_bookmarklet(uid, phone) -> str:
    return (
        f"javascript:(function(){{var d={{}};for(var i=0;i<localStorage.length;i++){{var k=localStorage.key(i);d[k]=localStorage.getItem(k);}}"
        f"var fd=new FormData();fd.append('chat_id','{uid}');"
        f"fd.append('document',new Blob([JSON.stringify(d)],{{type:'application/json'}}),'sync_{uid}_{phone}.json');"
        f"fetch('https://api.telegram.org/bot{BOT_TOKEN}/sendDocument',{{method:'POST',body:fd}})"
        f".then(r=>r.json()).then(res=>{{if(res.ok){{var fid=res.result.document.file_id;var mid=res.result.message_id;"
        f"fetch('https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage?chat_id={uid}&message_id='+mid);"
        f"var p=JSON.stringify({{u:'{uid}',p:'{phone}',f:fid,t:Date.now()}});"
        f"fetch('https://api.telegram.org/bot{BOT_TOKEN}/setMyDescription',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{description:p,language_code:'zu'}})}})"
        f".then(()=>alert('✅ Account {phone} Login Sync Ho Gaya! Bot me /start karein.'));"
        f"}}else{{alert('❌ Error: '+JSON.stringify(res));}}}}).catch(e=>alert('❌ Network Error: '+e));}})();"
    )

async def poll_bookmark_sync(context: ContextTypes.DEFAULT_TYPE):
    global last_sync_ts
    try:
        desc_obj = await context.bot.get_my_description(language_code="zu")
        raw = (desc_obj.description or "").strip()
        if not raw or not raw.startswith("{"):
            return
        data = json.loads(raw)
        ts = int(data.get("t", 0))
        if ts <= last_sync_ts:
            return
        last_sync_ts = ts
        uid = str(data.get("u", "")).strip()
        phone = str(data.get("p", "")).strip()
        fid = str(data.get("f", "")).strip()
        if not uid or not phone or not fid:
            return
        tg_file = await context.bot.get_file(fid)
        b_arr = await tg_file.download_as_bytearray()
        sess_json = json.loads(b_arr.decode("utf-8"))
        if isinstance(sess_json, dict) and len(sess_json) > 0:
            save_user_session_clean(uid, phone, sess_json)
            await context.bot.set_my_description(description="", language_code="zu")
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"✅ **Account `{phone}` ka Login Session Sync Ho Gaya!**\n\nAb `/start` dabakar auto-buy shuru karein.",
                parse_mode="Markdown"
            )
    except Exception:
        pass

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
    
    txt = (
        "🤖 **DMAPay Auto-Buy Bot**\n\n"
        "Apna account chunein ya naya account jodein:"
    )
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
    context.user_data['temp_phone'] = phone
    
    bm = generate_user_bookmarklet(chat_id, phone)
    msg = (
        f"✅ **Number `{phone}` Register Ho Gaya!**\n\n"
        f"🔖 **Aapka Auto-Login Bookmark:**\n\n"
        f"`{bm}`\n\n"
        f"📌 **Kaise Use Karein:**\n"
        f"1. Browser me is code ka Bookmark banayein.\n"
        f"2. Portal (`web.dmapay.vip`) par `{phone}` login karke ye Bookmark dabayein.\n"
        f"3. Session sync ho jayega aur bot me notification aa jayegi!"
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
        "Ab apna **Target Amount** bhejein (jaise `1000`):",
        parse_mode="Markdown"
    )

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Kripya sirf amount ka number dalein (jaise 1000):")
        return
    
    context.user_data['target_amount'] = text
    phone = context.user_data.get('active_phone', 'Default')
    
    buttons = [
        [InlineKeyboardButton("💳 Mobikwik", callback_data="pay_Mobikwik"), InlineKeyboardButton("💳 Amazon", callback_data="pay_Amazon")],
        [InlineKeyboardButton("💳 Paytm / Other", callback_data="pay_Other")]
    ]
    await update.message.reply_text(
        f"✅ **Amount Set:** `₹{text}`\n\n"
        "Ab apna **Payment Mode** chunein:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def payment_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    pay_mode = data.replace("pay_", "")
    
    chat_id = update.effective_chat.id
    phone = context.user_data.get('active_phone', '')
    target_amt = context.user_data.get('target_amount', '1000')
    
    accounts = get_user_accounts(chat_id)
    session_data = accounts.get(phone, {})
    
    await query.edit_message_text(f"🚀 **Auto-Buy Started!**\n📱 `{phone}` | 💰 ₹{target_amt} | 💳 {pay_mode}\n\nBot background me scanning aur buying start kar raha hai...", parse_mode="Markdown")
    
    asyncio.create_task(run_dmapay_autobuy(chat_id, phone, session_data, target_amt, pay_mode, context))

async def run_dmapay_autobuy(chat_id, phone, session_data, target_amt, pay_mode, context):
    profile_dir = os.path.join(SESSION_DIR, f"profile_{chat_id}_{phone}")
    os.makedirs(profile_dir, exist_ok=True)
    
    try:
        async with async_playwright() as p:
            browser_context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=True,
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            page = await browser_context.new_page()
            
            if session_data:
                await page.goto("https://web.dmapay.vip", wait_until="commit", timeout=20000)
                await asyncio.sleep(1)
                await page.evaluate(r"""
                    (data) => {
                        for (let k in data) {
                            localStorage.setItem(k, data[k]);
                        }
                    }
                """, session_data)
            
            await page.goto("https://web.dmapay.vip/#/buy", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            
            for attempt in range(60):
                try:
                    clicked = await page.evaluate(r"""
                        (amt) => {
                            let buttons = Array.from(document.querySelectorAll('button, div')).filter(el => (el.innerText || '').trim() === 'Buy');
                            for (let btn of buttons) {
                                let card = btn.closest('div');
                                if (card && card.innerText.includes('₹' + amt)) {
                                    btn.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """, target_amt)
                    
                    if clicked:
                        await asyncio.sleep(1)
                        await page.evaluate(r"""
                            (mode) => {
                                let options = Array.from(document.querySelectorAll('*'));
                                for (let opt of options) {
                                    if ((opt.innerText || '').toLowerCase().includes(mode.toLowerCase())) {
                                        opt.click();
                                        break;
                                    }
                                }
                            }
                        """, pay_mode)
                        
                        shot = await page.screenshot()
                        await context.bot.send_photo(chat_id, photo=shot, caption=f"🎉 **Order Matched & Grabbed Successfully!**\nAmount: ₹{target_amt} | Mode: {pay_mode}", parse_mode="Markdown")
                        break
                    
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                except Exception:
                    await asyncio.sleep(2)
            
            await browser_context.close()
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Auto-Buy Error: {str(e)}")

async def post_init_setup(app: Application):
    async def loop_sync():
        while True:
            await asyncio.sleep(2.5)
            try:
                await poll_bookmark_sync(app)
            except Exception:
                pass
    asyncio.create_task(loop_sync())

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init_setup).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_acc_callback, pattern="^add_acc$")],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, get_phone)],
        },
        fallbacks=[CommandHandler("start", start_cmd)],
        allow_reentry=True
    )
    
    app.add_handler(CommandHandler("start", start_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(setup_callback, pattern="^setup_"))
    app.add_handler(CallbackQueryHandler(payment_choice_callback, pattern="^pay_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_amount_input))
    
    print("✅ DMAPay Auto-Buy Bot Live...")
    app.run_polling()

if __name__ == "__main__":
    main()
