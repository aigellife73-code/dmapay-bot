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
    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await update.message.reply_text("🛑 **Auto-Buy Scanning Rok Di Gayi!**", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ Koi task active nahi hai.")

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
        f"Ab **Target Amount ya Range** bhejein (jaise `700-1000` ya `1000`):",
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
        await update.message.reply_text("❌ Sahi Amount ya Range dalein! (Jaise `1000` ya `700-1000`):")
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
        f"🔍 **Status:** Browser starting...\n\n"
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
                if (r.top < 260 || r.top > 750 || r.height > 80 || r.height < 12) continue;
                
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
                if (pr.width >= 260 && pr.height <= 90) {
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
                if (cr.left > 300 && cr.width >= 10 && cr.width <= 45) {
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
    
    min_val, max_val = 0.0, 0.0
    if '-' in target_amt:
        parts = target_amt.split('-')
        try:
            min_val = float(parts[0].strip())
            max_val = float(parts[1].strip())
        except:
            min_val, max_val = 500.0, 1000.0
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

            while running_tasks.get(chat_id, {}).get("active", False):
                scan_count += 1
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer_str = f"{mins:02d}:{secs:02d}"

                # 0. Check Notify Popup ("need to pay!") -> Click Go pay
                notify_handled = await page.evaluate(r"""
                    () => {
                        let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                        for (let d of divs) {
                            let t = (d.innerText || '').trim();
                            if (t.includes('need to pay!') || t === 'Notify') {
                                let goPayBtn = divs.find(b => (b.innerText || '').trim() === 'Go pay');
                                if (goPayBtn) {
                                    goPayBtn.click();
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                if notify_handled:
                    await asyncio.sleep(1.5)
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=f"🎉 **Order Match Ho Gaya! Payment page open ho gaya hai.** (Time: {timer_str})",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                    
                    shot = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=f"🎉 **Order Grabbed Successfully!**\n💰 Target: ₹{target_amt}\n💳 Mode: {pay_mode}\n⏱️ Time Taken: {timer_str}",
                        parse_mode="Markdown"
                    )
                    break

                # 1. Check Tips Popup ("Failed to pick up order") -> Click Stay
                popup_handled = await page.evaluate(r"""
                    () => {
                        let divs = Array.from(document.querySelectorAll('div, span, p'));
                        for (let d of divs) {
                            let t = (d.innerText || '').trim();
                            if (t.includes('Failed to pick up order') || t === 'Tips') {
                                let stayBtn = divs.find(b => (b.innerText || '').trim() === 'Stay');
                                if (stayBtn) {
                                    stayBtn.click();
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)

                if popup_handled:
                    await asyncio.sleep(0.8)
                    continue

                # 2. Smart DOM Scanner: Find any card with price in range and click its Buy button
                target_info = await page.evaluate(r"""
                    ({minV, maxV}) => {
                        let cards = Array.from(document.querySelectorAll('div, section, article')).filter(el => {
                            let t = el.innerText || '';
                            return t.includes('Price') && t.includes('Buy');
                        });
                        
                        for (let card of cards) {
                            let txt = card.innerText;
                            let m = txt.match(/₹\s*([0-9,]+(\.[0-9]+)?)/);
                            if (m && m[1]) {
                                let val = parseFloat(m[1].replace(/,/g, ''));
                                if (val >= minV && val <= maxV) {
                                    let btn = Array.from(card.querySelectorAll('button, div, span')).find(b => (b.innerText || '').trim() === 'Buy' && b.children.length === 0);
                                    if (btn) {
                                        let rect = btn.getBoundingClientRect();
                                        if (rect.top > 150 && rect.top < 780 && rect.width > 20) {
                                            btn.click();
                                            return {
                                                found: true,
                                                amount: val,
                                                box: {x: rect.left, y: rect.top, width: rect.width, height: rect.height}
                                            };
                                        }
                                    }
                                }
                            }
                        }
                        return {found: false};
                    }
                """, {"minV": min_val, "maxV": max_val})

                if target_info and target_info.get("found"):
                    running_tasks[chat_id]["active"] = False # Stop loop immediately
                    
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=f"🎯 **Order Match Ho Gaya! (₹{target_info.get('amount')}) Click kar diya gaya hai. Processing...**",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    await tap_element_center(page, target_info["box"])
                    
                    popup_type = "none"
                    for _ in range(15):
                        await asyncio.sleep(0.3)
                        body_txt = await page.evaluate("() => document.body.innerText")
                        if "need to pay!" in body_txt or "Go pay" in body_txt:
                            popup_type = "notify"
                            break
                        if "Failed to pick up order" in body_txt:
                            popup_type = "failed"
                            break
                        if "Pay using the following" in body_txt or "Manage UPI" in body_txt:
                            popup_type = "success"
                            break
                    
                    if popup_type == "notify":
                        await page.evaluate(r"""
                            () => {
                                let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                                let goPayBtn = divs.find(b => (b.innerText || '').trim() === 'Go pay');
                                if (goPayBtn) goPayBtn.click();
                            }
                        """)
                        await asyncio.sleep(2)
                        shot = await page.screenshot(full_page=True)
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot,
                            caption=f"🎉 **Order Grabbed Successfully!**\n💰 Amount: ₹{target_info.get('amount')}\n💳 Mode: {pay_mode}\n⏱️ Time Taken: {timer_str}",
                            parse_mode="Markdown"
                        )
                        break

                    if popup_type == "failed":
                        await page.evaluate(r"""
                            () => {
                                let divs = Array.from(document.querySelectorAll('div, span, p'));
                                let stayBtn = divs.find(b => (b.innerText || '').trim() === 'Stay');
                                if (stayBtn) stayBtn.click();
                            }
                        """)
                        await asyncio.sleep(0.5)
                        # Restart loop if failed
                        running_tasks[chat_id]["active"] = True
                        continue

                    await asyncio.sleep(0.6)

                    tips_done = False
                    for _ in range(4):
                        await click_wallet_option(page, pay_mode)
                        
                        for _ in range(8):
                            await asyncio.sleep(0.3)
                            
                            body_txt = await page.evaluate("() => document.body.innerText")
                            if "need to pay!" in body_txt or "Go pay" in body_txt:
                                await page.evaluate(r"""
                                    () => {
                                        let divs = Array.from(document.querySelectorAll('div, span, p, button'));
                                        let goPayBtn = divs.find(b => (b.innerText || '').trim() === 'Go pay');
                                        if (goPayBtn) goPayBtn.click();
                                    }
                                """)
                                tips_done = "notify_done"
                                break

                            if "Failed to pick up order" in body_txt:
                                await page.evaluate(r"""
                                    () => {
                                        let divs = Array.from(document.querySelectorAll('div, span, p'));
                                        let stayBtn = divs.find(b => (b.innerText || '').trim() === 'Stay');
                                        if (stayBtn) stayBtn.click();
                                    }
                                """)
                                tips_done = "failed_retry"
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
                                tips_done = True
                                break
                            
                            if "buyingbymod" in page.url:
                                tips_done = True
                                break
                        
                        if tips_done == "failed_retry":
                            break
                        if tips_done:
                            break

                    if tips_done == "failed_retry":
                        await asyncio.sleep(0.5)
                        running_tasks[chat_id]["active"] = True
                        continue

                    if tips_done == "notify_done":
                        await asyncio.sleep(2)
                        shot = await page.screenshot(full_page=True)
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot,
                            caption=f"🎉 **Order Grabbed Successfully!**\n💰 Amount: ₹{target_info.get('amount')}\n💳 Mode: {pay_mode}\n⏱️ Time Taken: {timer_str}",
                            parse_mode="Markdown"
                        )
                        break

                    for _ in range(25):
                        await asyncio.sleep(0.4)
                        cur_url = page.url
                        b_text = await page.evaluate("() => document.body.innerText")
                        if "buyingbymod" in cur_url and ("IFSC" in b_text or "Account" in b_text or "Upload Proof" in b_text):
                            break

                    await asyncio.sleep(1.5)
                    
                    shot = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=f"🎉 **Order Grabbed Successfully!**\n💰 Amount: ₹{target_info.get('amount')}\n💳 Mode: {pay_mode}\n⏱️ Time Taken: {timer_str}",
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

                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(1.2)

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
