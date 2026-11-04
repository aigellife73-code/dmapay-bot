import asyncio, json, os, threading, time, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)
from playwright.async_api import async_playwright

BOT_TOKEN = "9105269973:AAFtunQsu9g4roxGUi2RYTt_dSahQdIqlVw"
ACCOUNTS_FILE = "dmapay_accounts.json"
SESSION_DIR = "dmapay_sessions"

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
        uid = str(data.get("u", "")).strip()
        phone = str(data.get("p", "")).strip()
        fid = str(data.get("f", "")).strip()
        if not uid or not phone or not fid:
            return
        
        existing_accs = get_user_accounts(int(uid))
        if ts <= last_sync_ts and phone in existing_accs:
            return

        tg_file = await context.bot.get_file(fid)
        b_arr = await tg_file.download_as_bytearray()
        sess_json = json.loads(b_arr.decode("utf-8"))
        if isinstance(sess_json, dict) and len(sess_json) > 0:
            is_new_sync = (ts > last_sync_ts and last_sync_ts != 0) or (phone not in existing_accs)
            last_sync_ts = ts
            save_user_session_clean(uid, phone, sess_json)
            if is_new_sync:
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
    
    await poll_bookmark_sync(context)
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
    if chat_id in running_tasks and running_tasks[chat_id].get("active", False):
        running_tasks[chat_id]["active"] = False
        await update.message.reply_text("🛑 **Auto-Buy Scanning Rok Di Gayi!**", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ Koi bhi Auto-Buy task active nahi hai.")

async def add_acc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'WAIT_PHONE'
    await query.edit_message_text("👉 Apna **10-digit Phone Number** bhejein:", parse_mode="Markdown")

async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    phone = query.data.replace("setup_", "")
    context.user_data['active_phone'] = phone
    context.user_data['state'] = 'WAIT_AMOUNT'
    await query.answer()
    await query.edit_message_text(
        f"📱 **Account: `{phone}`**\n\n"
        f"Ab **Target Amount ya Range** bhejein (jaise `500`, `600-900` ya `1000`):",
        parse_mode="Markdown"
    )

async def handle_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    state = context.user_data.get('state', '')

    if (text.isdigit() and len(text) == 10) or state == 'WAIT_PHONE':
        if not text.isdigit() or len(text) != 10:
            await update.message.reply_text("❌ Kripya sahi **10-digit Phone Number** dalein:", parse_mode="Markdown")
            return
        
        context.user_data['state'] = ''
        bm = generate_user_bookmarklet(chat_id, text)
        await poll_bookmark_sync(context)
        accounts = get_user_accounts(int(chat_id))
        extra_btn = []
        if text in accounts:
            extra_btn.append([InlineKeyboardButton(f"🚀 Direct Setup {text} (Synced)", callback_data=f"setup_{text}")])
        
        msg = (
            f"✅ **Number `{text}` Registered!**\n\n"
            f"🔖 **Auto-Login Bookmark Code:**\n\n`{bm}`\n\n"
            f"📌 web.dmapay.vip par login karke bookmark run karein."
        )
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(extra_btn) if extra_btn else ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return

    active_phone = context.user_data.get('active_phone')
    if not active_phone:
        await poll_bookmark_sync(context)
        accounts = get_user_accounts(int(chat_id))
        if len(accounts) == 1:
            active_phone = list(accounts.keys())[0]
            context.user_data['active_phone'] = active_phone
        else:
            await update.message.reply_text("⚠️ Pehle `/start` dabakar Account select karein ya 10-digit number bhejein!")
            return

    if not re.match(r'^\d+(\s*-\s*\d+)?$', text):
        await update.message.reply_text("❌ Sahi Amount ya Range dalein! (Jaise `1000` ya `500-900`):")
        return

    context.user_data['target_amount'] = text.replace(" ", "")
    context.user_data['state'] = ''
    buttons = [
        [InlineKeyboardButton("💳 Mobikwik", callback_data="pay_mobikwik"), InlineKeyboardButton("💳 Amazon", callback_data="pay_amazon")],
        [InlineKeyboardButton("💳 Any / First Available", callback_data="pay_all")]
    ]
    await update.message.reply_text(
        f"📱 **Account:** `{active_phone}`\n"
        f"✅ **Target Set:** `{context.user_data['target_amount']}`\n\n"
        f"👇 Apna **Payment Mode** chunein:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

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
        f"🔍 **Status:** Setting smart price range filter...\n\n"
        f"🛑 Rokne ke liye `/stop` bhejein.",
        parse_mode="Markdown"
    )
    
    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await asyncio.sleep(1)
        
    running_tasks[chat_id] = {"active": True}
    asyncio.create_task(run_scanner_loop(chat_id, phone, session_data, target_amt, pay_mode, status_msg.message_id, context))

async def tap_point(page, x, y):
    try:
        await page.touchscreen.tap(x, y)
    except Exception:
        pass
    try:
        await page.mouse.click(x, y)
    except Exception:
        pass

async def tap_element_center(page, box):
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    await tap_point(page, cx, cy)

async def set_custom_price_filter(page, min_val: float, max_val: float):
    try:
        # Open filter drawer if collapsed
        arrow_box = await page.evaluate(r"""
            () => {
                let els = Array.from(document.querySelectorAll('div, span, i, svg'));
                let minEl = els.find(e => (e.innerText || '').trim() === 'Min');
                if (minEl) {
                    let r = minEl.getBoundingClientRect();
                    return {x: Math.max(20, r.left - 25), y: r.top + r.height / 2};
                }
                return {x: 65, y: 170};
            }
        """)
        if arrow_box:
            await tap_point(page, arrow_box["x"], arrow_box["y"])
            await asyncio.sleep(0.4)

        inputs = await page.query_selector_all("input")
        if len(inputs) >= 2:
            await inputs[0].click()
            await inputs[0].fill(str(int(min_val)))
            await inputs[1].click()
            await inputs[1].fill(str(int(max_val)))
            await asyncio.sleep(0.3)
            # Click 🔍 Search icon button reliably
            await tap_point(page, 355, 170)
            await asyncio.sleep(0.8)
    except Exception:
        pass

async def click_wallet_option(page, pay_mode: str):
    mode = pay_mode.lower().strip()
    coords = await page.evaluate(r"""
        (targetMode) => {
            let nodes = Array.from(document.querySelectorAll('div, span, p, li, label'));
            let targetEl = null;
            
            for (let el of nodes) {
                let t = (el.innerText || '').trim().toLowerCase();
                if (!t || t.startsWith('wallets:') || t.includes('pay using') || t.includes('manage upi')) continue;
                
                let r = el.getBoundingClientRect();
                if (r.top < 240 || r.top > 790 || r.height > 90 || r.height < 12) continue;
                
                if (targetMode === 'mobikwik' && (t.includes('@mbk') || t === 'mobikwik')) {
                    targetEl = el;
                    if (t.includes('@mbk')) break;
                } else if (targetMode === 'amazon' && (t.includes('@yapl') || t === 'amazon')) {
                    targetEl = el;
                    if (t.includes('@yapl')) break;
                } else if (targetMode === 'all' && (t.includes('@') || t === 'mobikwik' || t === 'amazon')) {
                    targetEl = el;
                    break;
                }
            }
            
            if (!targetEl) return null;
            
            let row = targetEl;
            for (let i = 0; i < 4; i++) {
                if (!row.parentElement) break;
                let pr = row.parentElement.getBoundingClientRect();
                if (pr.width >= 250 && pr.height <= 95) {
                    row = row.parentElement;
                    break;
                }
                row = row.parentElement;
            }
            
            row.click();
            targetEl.click();
            
            let children = row.querySelectorAll('*');
            let radioRect = null;
            for (let c of children) {
                let cr = c.getBoundingClientRect();
                if (cr.left > 290 && cr.width >= 10 && cr.width <= 45) {
                    c.click();
                    radioRect = {x: cr.left + cr.width / 2, y: cr.top + cr.height / 2};
                }
            }
            
            let rr = row.getBoundingClientRect();
            let cy = rr.top + rr.height / 2;
            return {
                textX: rr.left + 110,
                centerX: rr.left + rr.width / 2,
                radioX: radioRect ? radioRect.x : (rr.right - 25),
                y: radioRect ? radioRect.y : cy
            };
        }
    """, mode)

    if coords:
        await tap_point(page, coords["radioX"], coords["y"])
        await asyncio.sleep(0.15)
        await tap_point(page, coords["textX"], coords["y"])
        await asyncio.sleep(0.15)
        await tap_point(page, coords["centerX"], coords["y"])
        return True
    return False

async def run_scanner_loop(chat_id, phone, session_data, target_amt, pay_mode, message_id, context):
    start_time = time.time()
    scan_count = 0
    retry_count = 0
    
    min_val, max_val = 0.0, 0.0
    if '-' in target_amt:
        parts = target_amt.split('-')
        try:
            min_val = float(parts[0].strip())
            max_val = float(parts[1].strip())
        except:
            min_val, max_val = 500.0, 900.0
    else:
        try:
            val = float(target_amt.replace('₹','').strip())
            # Smart Range for single amounts (e.g. 500 -> 500 to 650, 1000 -> 1000 to 1150)
            min_val = val
            max_val = val + 150
        except:
            min_val, max_val = 1000.0, 1150.0

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

            # Set smart Min and Max range and click 🔍 search
            await set_custom_price_filter(page, min_val, max_val)
            await asyncio.sleep(1.2)

            # Send Startup Screenshot immediately so user sees the filter applied
            try:
                init_shot = await page.screenshot()
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=init_shot,
                    caption=f"✅ **Filter Applied (Min: {int(min_val)}, Max: {int(max_val)})!**\nScanning orders...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

            while running_tasks.get(chat_id, {}).get("active", False):
                scan_count += 1
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer_str = f"{mins:02d}:{secs:02d}"

                # 0. Check Bank Details Page (#/buyingbymod)
                cur_url = page.url
                b_text = await page.evaluate("() => document.body.innerText")
                if "buyingbymod" in cur_url and ("IFSC" in b_text or "Account" in b_text or "Upload Proof" in b_text):
                    running_tasks[chat_id]["active"] = False
                    await asyncio.sleep(1.0)
                    shot = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=(
                            f"🎉 **Bank Details Aa Gayi! Order Matched & Stopped!**\n"
                            f"💰 **Target:** `{target_amt}` | 💳 **Mode:** `{pay_mode}`\n"
                            f"⏱️ **Time Taken:** `{timer_str}`"
                        ),
                        parse_mode="Markdown"
                    )
                    break

                # 1. Check Notify Popup ("need to pay!" -> Go pay)
                notify_box = await page.evaluate(r"""
                    () => {
                        let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                        for (let d of divs) {
                            let t = (d.innerText || '').trim();
                            if (t.includes('need to pay!') || t === 'Notify') {
                                let goPayBtn = divs.find(b => (b.innerText || '').trim() === 'Go pay');
                                if (goPayBtn) {
                                    goPayBtn.click();
                                    let r = goPayBtn.getBoundingClientRect();
                                    return {x: r.left, y: r.top, width: r.width, height: r.height};
                                }
                            }
                        }
                        return null;
                    }
                """)
                if notify_box:
                    await tap_element_center(page, notify_box)
                    for _ in range(20):
                        await asyncio.sleep(0.4)
                        bt = await page.evaluate("() => document.body.innerText")
                        if "IFSC" in bt or "Account" in bt:
                            break
                    await asyncio.sleep(1.0)
                    running_tasks[chat_id]["active"] = False
                    shot = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=(
                            f"🎉 **Order Matched & Bank Details Opened!**\n"
                            f"💰 **Target:** `{target_amt}` | 💳 **Mode:** `{pay_mode}`\n"
                            f"⏱️ **Time Taken:** `{timer_str}`"
                        ),
                        parse_mode="Markdown"
                    )
                    break

                # 2. Handle 'Please slow down' / 'Failed to pick up order' -> Click 'Stay'
                stay_box = await page.evaluate(r"""
                    () => {
                        let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                        for (let d of divs) {
                            let t = (d.innerText || '').trim();
                            if (t.includes('Failed to pick up order') || t.includes('Please slow down') || t === 'Tips') {
                                let stayBtn = divs.find(b => (b.innerText || '').trim() === 'Stay');
                                if (stayBtn) {
                                    stayBtn.click();
                                    let r = stayBtn.getBoundingClientRect();
                                    return {x: r.left, y: r.top, width: r.width, height: r.height};
                                }
                            }
                        }
                        return null;
                    }
                """)
                if stay_box:
                    await tap_element_center(page, stay_box)
                    retry_count += 1
                    await asyncio.sleep(0.5)
                    continue

                # 3. Sequential Single-Order Scanner
                scan_result = await page.evaluate(r"""
                    ({minV, maxV}) => {
                        let visiblePrices = [];
                        let allEls = Array.from(document.querySelectorAll('button, div, span, a'));
                        for (let el of allEls) {
                            let txt = (el.innerText || '').trim();
                            if (txt !== 'Buy') continue;
                            
                            let hasSameChild = Array.from(el.children).some(c => (c.innerText || '').trim() === 'Buy');
                            if (hasSameChild) continue;
                            
                            let rect = el.getBoundingClientRect();
                            if (rect.top < 150 || rect.top > 760 || rect.left < 200 || rect.width < 25) continue;
                            
                            let card = el.parentElement;
                            for (let i = 0; i < 7; i++) {
                                if (!card) break;
                                let cText = card.innerText || '';
                                if (cText.includes('₹') && (cText.includes('Price') || cText.includes('Reward') || cText.includes('Itoken'))) {
                                    let m = cText.match(/₹\s*([0-9,]+(\.[0-9]+)?)/);
                                    if (m && m[1]) {
                                        let val = parseFloat(m[1].replace(/,/g, ''));
                                        visiblePrices.push(val);
                                        if (val >= minV && val <= maxV) {
                                            el.click();
                                            return {
                                                found: true,
                                                amount: val,
                                                box: {x: rect.left, y: rect.top, width: rect.width, height: rect.height},
                                                prices: visiblePrices
                                            };
                                        }
                                    }
                                    break;
                                }
                                card = card.parentElement;
                            }
                        }
                        return {found: false, prices: visiblePrices};
                    }
                """, {"minV": min_val, "maxV": max_val})

                visible_prices = scan_result.get("prices", []) if scan_result else []

                if scan_result and scan_result.get("found"):
                    amt = scan_result.get("amount")
                    
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=(
                                f"🎯 **Order Match Ho Gaya! (₹{amt})**\n"
                                f"💳 `{pay_mode}` select ho raha hai...\n"
                                f"⏱️ Timer: `{timer_str}` | 🔄 Retries: `{retry_count}`"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    # STEP 1 SCREENSHOT: Buy Clicked
                    await asyncio.sleep(0.4)
                    try:
                        shot_buy = await page.screenshot()
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot_buy,
                            caption=f"📸 **Step 1: Buy Clicked (₹{amt})!**",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    # Select Wallet (Mobikwik / Amazon)
                    await click_wallet_option(page, pay_mode)
                    await asyncio.sleep(0.5)

                    # STEP 2 SCREENSHOT: Wallet Selected
                    try:
                        shot_wallet = await page.screenshot()
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot_wallet,
                            caption=f"📸 **Step 2: `{pay_mode}` Selected (₹{amt})!**",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    # Handle 'OK' or 'Stay'
                    step_result = "unknown"
                    for _ in range(12):
                        await asyncio.sleep(0.2)
                        body_txt = await page.evaluate("() => document.body.innerText")
                        
                        if "Failed to pick up order" in body_txt or "Please slow down" in body_txt:
                            s_box = await page.evaluate(r"""
                                () => {
                                    let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                                    let stayBtn = divs.find(b => (b.innerText || '').trim() === 'Stay');
                                    if (stayBtn) {
                                        stayBtn.click();
                                        let r = stayBtn.getBoundingClientRect();
                                        return {x: r.left, y: r.top, width: r.width, height: r.height};
                                    }
                                    return null;
                                }
                            """)
                            if s_box:
                                await tap_element_center(page, s_box)
                            step_result = "failed_retry"
                            break

                        if "need to pay!" in body_txt or "Go pay" in body_txt:
                            step_result = "notify_ready"
                            break

                        ok_box = await page.evaluate(r"""
                            () => {
                                let btns = Array.from(document.querySelectorAll('button, div, span'));
                                for (let b of btns) {
                                    let t = (b.innerText || '').trim();
                                    if (t === 'OK' || t === 'Confirm' || t === '确定') {
                                        let r = b.getBoundingClientRect();
                                        if (r.width > 25 && r.height > 18 && r.top > 200) {
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
                            step_result = "ok_clicked"
                            break

                        if "buyingbymod" in page.url:
                            step_result = "ok_clicked"
                            break

                    if step_result == "failed_retry":
                        retry_count += 1
                        await asyncio.sleep(0.8)
                        await set_custom_price_filter(page, min_val, max_val)
                        await asyncio.sleep(0.6)
                        continue

                    # Wait for Final Bank Details
                    bank_loaded = False
                    for _ in range(25):
                        await asyncio.sleep(0.3)
                        cur_url = page.url
                        b_text = await page.evaluate("() => document.body.innerText")
                        
                        if "Failed to pick up order" in b_text or "Please slow down" in b_text:
                            s_box = await page.evaluate(r"""
                                () => {
                                    let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                                    let stayBtn = divs.find(b => (b.innerText || '').trim() === 'Stay');
                                    if (stayBtn) {
                                        stayBtn.click();
                                        let r = stayBtn.getBoundingClientRect();
                                        return {x: r.left, y: r.top, width: r.width, height: r.height};
                                    }
                                    return null;
                                }
                            """)
                            if s_box:
                                await tap_element_center(page, s_box)
                            bank_loaded = False
                            break

                        if ("buyingbymod" in cur_url and ("IFSC" in b_text or "Account" in b_text or "Upload Proof" in b_text or "Finish payment" in b_text)):
                            bank_loaded = True
                            break

                    if not bank_loaded:
                        retry_count += 1
                        await asyncio.sleep(0.5)
                        continue

                    await asyncio.sleep(1.0)
                    running_tasks[chat_id]["active"] = False
                    
                    shot_final = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot_final,
                        caption=(
                            f"🎉 **Order Matched & Bank Details Opened!**\n"
                            f"💰 **Amount:** ₹{amt}\n"
                            f"💳 **Mode:** {pay_mode}\n"
                            f"🔗 **URL:** `{page.url}`\n"
                            f"⏱️ **Time Taken:** `{timer_str}`"
                        ),
                        parse_mode="Markdown"
                    )
                    break

                if scan_count % 3 == 0:
                    prices_str = ", ".join([f"₹{int(p)}" for p in visible_prices[:5]]) if visible_prices else "None"
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=(
                                f"🚀 **Auto-Buy Active!**\n"
                                f"📱 `{phone}` | 💰 Target: `{target_amt}` | 💳 {pay_mode}\n\n"
                                f"⏱️ **Live Timer:** `{timer_str}`\n"
                                f"🔄 **Scans:** `{scan_count}` | **Retries:** `{retry_count}`\n"
                                f"👀 **Screen Prices:** `{prices_str}`\n\n"
                                f"🛑 Rokne ke liye `/stop` bhejein."
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                # Quick 🔍 search refresh click
                await set_custom_price_filter(page, min_val, max_val)
                await asyncio.sleep(0.8)

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
    
    app.add_handler(CommandHandler("start", start_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stop", stop_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(add_acc_callback, pattern="^add_acc$"))
    app.add_handler(CallbackQueryHandler(setup_callback, pattern="^setup_"))
    app.add_handler(CallbackQueryHandler(payment_choice_callback, pattern="^pay_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_all_text))
    
    print("DMAPay Bot Live...")
    app.run_polling()

if __name__ == "__main__":
    main()
