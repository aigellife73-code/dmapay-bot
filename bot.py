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
    await query.edit_message_text(f"📱 **Account: `{phone}`**\n\nTarget Amount ya Range dalein (jaise `700-1000` ya `1000`):", parse_mode="Markdown")

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    if '-' not in text and not text.isdigit():
        await update.message.reply_text("❌ Format galat hai! Jaise `700-1000` ya `1000` dalein:")
        return
    
    context.user_data['target_amount'] = text
    buttons = [
        [InlineKeyboardButton("💳 Mobikwik", callback_data="pay_mobikwik"), InlineKeyboardButton("💳 Amazon", callback_data="pay_amazon")],
        [InlineKeyboardButton("💳 Any / First Available", callback_data="pay_all")]
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
        f"🚀 **Auto-Buy Fully Active!**\n"
        f"📱 `{phone}` | 💰 Target: `{target_amt}` | 💳 {pay_mode}\n\n"
        f"⏱️ **Live Timer:** `00:00`\n"
        f"🔍 **Status:** Browser starting...\n\n"
        f"🛑 Rokne ke liye `/stop` bhejein.",
        parse_mode="Markdown"
    )
    
    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await asyncio.sleep(1)
        
    running_tasks[chat_id] = {"active": True}
    asyncio.create_task(run_scanner_loop(chat_id, phone, session_data, target_amt, pay_mode, status_msg.message_id, context))

async def tap_element_center(page, box):
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    try:
        await page.touchscreen.tap(cx, cy)
    except Exception:
        pass
    try:
        await page.mouse.click(cx, cy)
    except Exception:
        pass

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
            min_val, max_val = 1000.0

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            browser_context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                has_touch=True,
                is_mobile=True
            )
            page = await browser_context.new_page()

            if session_data:
                await page.goto("https://web.dmapay.vip", wait_until="commit", timeout=20000)
                await asyncio.sleep(1)
                await page.evaluate(r"""(data) => { for (let k in data) { localStorage.setItem(k, data[k]); } }""", session_data)

            await page.goto("https://web.dmapay.vip/#/buy", wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Filter chip select karna
            chip_text = None
            if max_val <= 999:
                chip_text = "0~999"
            elif min_val >= 1000 and max_val <= 2999:
                chip_text = "1000~2999"
            
            if chip_text:
                try:
                    chip_loc = page.get_by_text(chip_text, exact=True).first
                    if await chip_loc.count() > 0:
                        await chip_loc.click(force=True)
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

            while running_tasks.get(chat_id, {}).get("active", False):
                scan_count += 1
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer_str = f"{mins:02d}:{secs:02d}"

                # Sirf ASLI Order Card ke Buy button ka exact coordinate nikalna
                # Header (y < 180) aur Bottom Nav (y > 760) ke 'Buy' ko ignore karega
                target_info = await page.evaluate(r"""
                    ({minV, maxV}) => {
                        let allEls = Array.from(document.querySelectorAll('button, div, span, a'));
                        for (let el of allEls) {
                            let directText = '';
                            for (let node of el.childNodes) {
                                if (node.nodeType === 3) directText += node.nodeValue;
                            }
                            if (directText.trim() !== 'Buy') continue;
                            
                            let rect = el.getBoundingClientRect();
                            // Order list beech me hoti hai (y: 180 se 750) aur Buy button right side (x > 250)
                            if (rect.top < 180 || rect.top > 750 || rect.left < 240 || rect.width < 30) continue;
                            
                            // Upar jao jab tak sirf ek single order card mile (jisme No: aur ₹ ho)
                            let card = el.parentElement;
                            for (let i = 0; i < 6; i++) {
                                if (!card) break;
                                let cText = card.innerText || '';
                                if (cText.includes('Price') && cText.includes('₹')) {
                                    let m = cText.match(/₹\s*([0-9,]+(\.[0-9]+)?)/);
                                    if (m && m[1]) {
                                        let val = parseFloat(m[1].replace(/,/g, ''));
                                        if (val >= minV && val <= maxV) {
                                            el.click(); // JS click
                                            return {
                                                found: true,
                                                amount: val,
                                                box: {x: rect.left, y: rect.top, width: rect.width, height: rect.height}
                                            };
                                        }
                                    }
                                    break;
                                }
                                card = card.parentElement;
                            }
                        }
                        return {found: false};
                    }
                """, {"minV": min_val, "maxV": max_val})

                if target_info and target_info.get("found"):
                    # 1. Real Touchscreen Tap on Buy Button
                    await tap_element_center(page, target_info["box"])
                    
                    # Wait for bottom popup ("Pay using the following payment UPIs:")
                    popup_opened = False
                    for _ in range(15):
                        await asyncio.sleep(0.3)
                        body_txt = await page.evaluate("() => document.body.innerText")
                        if "Pay using the following" in body_txt or "Manage UPI" in body_txt or "mobikwik" in body_txt.lower() or "amazon" in body_txt.lower():
                            popup_opened = True
                            break
                    
                    if not popup_opened:
                        # Agar pehle tap me popup nahi khula, toh dubara tap karo
                        await tap_element_center(page, target_info["box"])
                        await asyncio.sleep(1.2)

                    # 2. Wallet/UPI Option Click (mobikwik / amazon)
                    wallet_box = await page.evaluate(r"""
                        (mode) => {
                            let targetMode = mode.toLowerCase();
                            let els = Array.from(document.querySelectorAll('div, span, p, li'));
                            for (let el of els) {
                                let txt = (el.innerText || '').trim().toLowerCase();
                                let rect = el.getBoundingClientRect();
                                // Bottom sheet me (y > 400) wallet item dhoondhna
                                if (rect.top > 380 && rect.height > 15 && rect.height < 120 && rect.width > 50) {
                                    if ((targetMode !== 'all' && txt.includes(targetMode)) || 
                                        (targetMode === 'all' && (txt.includes('@') || txt.includes('mobikwik') || txt.includes('amazon')))) {
                                        el.click();
                                        return {x: rect.left, y: rect.top, width: rect.width, height: rect.height};
                                    }
                                }
                            }
                            return null;
                        }
                    """, pay_mode)

                    if wallet_box:
                        await tap_element_center(page, wallet_box)
                    
                    # 3. Wait for 'Tips' dialog ("The pre-order is successful..." -> Click 'OK')
                    for _ in range(15):
                        await asyncio.sleep(0.3)
                        ok_box = await page.evaluate(r"""
                            () => {
                                let btns = Array.from(document.querySelectorAll('button, div, span'));
                                for (let b of btns) {
                                    let t = (b.innerText || '').trim();
                                    if (t === 'OK' || t === 'Confirm' || t === '确定') {
                                        let r = b.getBoundingClientRect();
                                        if (r.width > 30 && r.height > 20 && r.top > 200) {
                                            b.click();
                                            return {x: r.left, y: r.top, width: r.width, height: r.height};
                                        }
                                    }
                                }
                                return null;
                            }
                        """)
                        if ok_box:
                            await tap_element_center(page, ok_box)
                            break

                    # 4. Wait until URL changes to buyingbymod or payment details load
                    for _ in range(20):
                        await asyncio.sleep(0.4)
                        cur_url = page.url
                        b_text = await page.evaluate("() => document.body.innerText")
                        if "buyingbymod" in cur_url or "Payment info" in b_text or "IFSC" in b_text or "Finish payment" in b_text or "Upload Proof" in b_text:
                            break

                    await asyncio.sleep(2.5) # Wait for Account/IFSC details to populate
                    
                    shot = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=(
                            f"🎉 **Order Grabbed & Payment Page Opened!**\n"
                            f"💰 **Amount:** ₹{target_info.get('amount')}\n"
                            f"💳 **Mode:** {pay_mode}\n"
                            f"🔗 **URL:** `{page.url}`\n"
                            f"⏱️ **Time Taken:** {timer_str}"
                        ),
                        parse_mode="Markdown"
                    )
                    break

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
                                f"🔍 **Status:** Scanning orders...\n\n"
                                f"🛑 Rokne ke liye `/stop` bhejein."
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                # Refresh without breaking filter chip
                if chip_text:
                    await page.evaluate(r"""(chip) => {
                        let els = Array.from(document.querySelectorAll('div, span')).filter(e => (e.innerText || '').trim() === chip);
                        if (els.length > 0) els[0].click();
                    }""", chip_text)
                else:
                    await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(1.5)

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
