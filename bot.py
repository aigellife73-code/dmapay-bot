import asyncio, json, os, threading, time, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from playwright.async_api import async_playwright

BOT_TOKEN = "9105269973:AAFtunQsu9g4roxGUi2RYTt_dSahQdIqlVw"
ACCOUNTS_FILE = "dmapay_accounts.json"
SESSION_DIR = "dmapay_sessions"

ASK_PHONE = range(1)
last_sync_ts = 0
running_tasks = {}

class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"DMAPay Bot Live")

def run_server():
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
    all_acc = load_accounts()
    return dict(all_acc.get(str(user_id), {}))

def save_user_session_clean(uid_str: str, phone: str, sess_json: dict):
    accs = load_accounts()
    if uid_str not in accs:
        accs[uid_str] = {}
    accs[uid_str][phone] = sess_json
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
                text=f"✅ **Account `{phone}` Sync Ho Gaya!**\n\nAb `/start` dabayein.",
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
    
    context.user_data.clear()
    accounts = get_user_accounts(user.id)
    
    buttons = []
    for phone in accounts.keys():
        buttons.append([InlineKeyboardButton(f"📱 {phone} (Auto-Buy Setup)", callback_data=f"setup_{phone}")])
    buttons.append([InlineKeyboardButton("➕ Add Account / Sync", callback_data="add_acc")])
    
    txt = "🤖 **DMAPay Auto-Buy Bot**\n\nApna account chunein ya naya jodein:"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await update.message.reply_text("🛑 **Auto-Buy Scanning Rok Di Gayi!**", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ Koi task active nahi hai.")

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
        f"✅ **Number `{phone}` Registered!**\n\n"
        f"🔖 **Auto-Login Bookmark:**\n\n`{bm}`\n\n"
        f"📌 web.dmapay.vip login karke bookmark run karein."
    )
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ConversationHandler.END

async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    phone = query.data.replace("setup_", "")
    context.user_data['active_phone'] = phone
    await query.answer()
    await query.edit_message_text(f"📱 **Account: `{phone}`**\n\nTarget Amount ya Range dalein (jaise `700-1000` ya `950`):", parse_mode="Markdown")

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    if '-' not in text and not text.isdigit():
        await update.message.reply_text("❌ Format galat hai! Jaise `700-1000` ya `950` dalein:")
        return
    
    context.user_data['target_amount'] = text
    buttons = [
        [InlineKeyboardButton("💳 Mobikwik", callback_data="pay_Mobikwik"), InlineKeyboardButton("💳 Amazon", callback_data="pay_Amazon")],
        [InlineKeyboardButton("💳 All Modes", callback_data="pay_All")]
    ]
    await update.message.reply_text(f"✅ **Target Set:** `{text}`\n\nPayment Mode chunein:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def payment_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pay_mode = query.data.replace("pay_", "")
    chat_id = update.effective_chat.id
    phone = context.user_data.get('active_phone', '')
    target_amt = context.user_data.get('target_amount', '1000')
    
    accounts = get_user_accounts(chat_id)
    session_data = accounts.get(phone, {})
    
    status_msg = await query.edit_message_text(
        f"🚀 **Auto-Buy Started!**\n"
        f"📱 `{phone}` | 💰 Target: `{target_amt}` | 💳 {pay_mode}\n\n"
        f"⏱️ **Live Timer:** `00:00`\n"
        f"🔍 **Status:** Searching orders...\n\n"
        f"🛑 Rokne ke liye `/stop` bhejein.",
        parse_mode="Markdown"
    )
    
    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await asyncio.sleep(1)
        
    running_tasks[chat_id] = {"active": True}
    asyncio.create_task(run_scanner_loop(chat_id, phone, session_data, target_amt, pay_mode, status_msg.message_id, context))

async def run_scanner_loop(chat_id, phone, session_data, target_amt, pay_mode, message_id, context):
    start_time = time.time()
    scan_count = 0
    
    min_val, max_val = 0.0, 0.0
    if '-' in target_amt:
        parts = target_amt.split('-')
        try:
            min_val = float(parts[0].strip())
            max_val = float(parts[1].strip())
        except:
            min_val, max_val = 700.0, 1000.0
    else:
        try:
            min_val = max_val = float(target_amt.replace('₹','').strip())
        except:
            min_val = max_val = 1000.0

    profile_dir = os.path.join(SESSION_DIR, f"profile_{chat_id}_{phone}")
    os.makedirs(profile_dir, exist_ok=True)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            browser_context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
            )
            page = await browser_context.new_page()

            if session_data:
                await page.goto("https://web.dmapay.vip", wait_until="commit", timeout=20000)
                await asyncio.sleep(1)
                await page.evaluate(r"""(data) => { for (let k in data) { localStorage.setItem(k, data[k]); } }""", session_data)

            await page.goto("https://web.dmapay.vip/#/buy", wait_until="networkidle")
            await asyncio.sleep(2)

            while running_tasks.get(chat_id, {}).get("active", False):
                scan_count += 1
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer_str = f"{mins:02d}:{secs:02d}"

                # Strict Matcher for exact Card layout seen in DMA Pay screenshot
                matched = await page.evaluate(r"""
                    ({minV, maxV}) => {
                        let allBuyBtns = Array.from(document.querySelectorAll('*')).filter(el => {
                            return (el.innerText || '').trim() === 'Buy' && el.children.length === 0;
                        });
                        
                        for (let btn of allBuyBtns) {
                            // Find card container (go up 2-4 levels)
                            let card = btn.parentElement;
                            for (let i = 0; i < 4; i++) {
                                if (card && card.innerText && card.innerText.includes('Price')) break;
                                if (card) card = card.parentElement;
                            }
                            
                            if (card && card.innerText) {
                                let txt = card.innerText;
                                // Regex matching '₹ 950' or 'Price \n ₹ 950'
                                let m = txt.match(/₹\s*([0-9]+(\.[0-9]+)?)/);
                                if (m && m[1]) {
                                    let val = parseFloat(m[1]);
                                    if (val >= minV && val <= maxV) {
                                        btn.click();
                                        return {found: true, amount: val};
                                    }
                                }
                            }
                        }
                        return {found: false};
                    }
                """, {"minV": min_val, "maxV": max_val})

                if matched and matched.get("found"):
                    await asyncio.sleep(1.2)
                    
                    # Payment mode click if not all
                    if pay_mode.lower() != "all":
                        await page.evaluate(r"""
                            (mode) => {
                                let opts = Array.from(document.querySelectorAll('*')).filter(el => {
                                    return (el.innerText || '').toLowerCase().includes(mode.toLowerCase()) && el.children.length === 0;
                                });
                                if (opts.length > 0) opts[0].click();
                            }
                        """, pay_mode)
                    
                    await asyncio.sleep(1)
                    shot = await page.screenshot()
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=f"🎉 **Order Buy Clicked Successfully!**\n💰 Amount: ₹{matched.get('amount')}\n💳 Mode: {pay_mode}\n⏱️ Time Taken: {timer_str}",
                        parse_mode="Markdown"
                    )
                    break

                # UI message update every 3 scans
                if scan_count % 3 == 0:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=(
                                f"🚀 **Auto-Buy Active!**\n"
                                f"📱 `{phone}` | 💰 Target: `{target_amt}` | 💳 {pay_mode}\n\n"
                                f"⏱️ **Live Timer:** `{timer_str}`\n"
                                f"🔄 **Scans Done:** `{scan_count} baar`\n"
                                f"🔍 **Status:** Searching orders on page...\n\n"
                                f"🛑 Rokne ke liye `/stop` bhejein."
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                # SPA refresh without losing tab state
                await page.evaluate(r"""() => {
                    let refreshIcon = document.querySelector('.van-pull-refresh, i.van-icon-replay');
                    if (refreshIcon) refreshIcon.click();
                }""")
                await asyncio.sleep(2)

            await browser.close()
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Auto-Buy Error: `{str(e)}`", parse_mode="Markdown")
    finally:
        if chat_id in running_tasks:
            running_tasks[chat_id]["active"] = False

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
    threading.Thread(target=run_server, daemon=True).start()
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
    app.add_handler(CommandHandler("stop", stop_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(setup_callback, pattern="^setup_"))
    app.add_handler(CallbackQueryHandler(payment_choice_callback, pattern="^pay_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_amount_input))
    
    print("DMAPay Bot Live...")
    app.run_polling()

if __name__ == "__main__":
    main()
