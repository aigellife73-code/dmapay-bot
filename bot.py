import asyncio, json, os, threading, time, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)
from playwright.async_api import async_playwright

BOT_TOKEN = "7927071708:AAGZS1WfeoqG3ydoV3zs_qyAGFmdt0TZ0GM"
ACCOUNTS_FILE = "dmapay_accounts.json"

last_sync_ts = 0
running_tasks = {}


class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"DMAPay Bot Live")

    def log_message(self, *args):
        pass


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
        f".then(()=>alert('✅ Account {phone} Sync Ho Gaya! Bot me /start karein.'));"
        f"}}else{{alert('❌ Error: '+JSON.stringify(res));}}}}).catch(e=>alert('❌ Network Error: '+e));}})();"
    )


async def poll_bookmark_sync(context):
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


# ---------------------------------------------------------------
# FETCH ACTIVE UPI OPTIONS FROM #/kycpartner
# ---------------------------------------------------------------
async def fetch_active_upis(session_data: dict):
    active_list = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            ctx = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                has_touch=True,
                is_mobile=True
            )
            page = await ctx.new_page()
            if session_data:
                await page.goto("https://web.dmapay.vip", wait_until="commit", timeout=20000)
                await asyncio.sleep(0.8)
                await page.evaluate(
                    r"""(data) => { for (let k in data) { localStorage.setItem(k, data[k]); } }""",
                    session_data
                )

            await page.goto("https://web.dmapay.vip/#/kycpartner", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2.5)

            active_list = await page.evaluate(r"""
                () => {
                    let results = [];
                    let seen = new Set();
                    let allEls = Array.from(document.querySelectorAll('span, div, button, label'));

                    for (let el of allEls) {
                        let txt = (el.innerText || '').trim();
                        if (txt !== 'Active') continue;
                        if (el.children.length > 0) continue;

                        let card = el.parentElement;
                        for (let i = 0; i < 6; i++) {
                            if (!card) break;
                            let cText = (card.innerText || '').trim();
                            if (cText.includes('@') || /mobikwik|amazon|indus|moneyview/i.test(cText)) {
                                if (/unlinked|unlink/i.test(cText) && !cText.includes('Active')) break;

                                let upiMatch = cText.match(/[a-zA-Z0-9.*_-]+@[a-zA-Z0-9._-]+/);
                                let upi = upiMatch ? upiMatch[0] : '';

                                let wallet = 'UPI';
                                if (/amazon/i.test(cText) || upi.includes('@yapl')) wallet = 'Amazon';
                                else if (/mobikwik/i.test(cText) || upi.includes('@mbk')) wallet = 'Mobikwik';
                                else if (/indus/i.test(cText)) wallet = 'IndusPay';
                                else if (/moneyview/i.test(cText)) wallet = 'Moneyview';

                                let key = (wallet + '_' + upi).toLowerCase();
                                if (!seen.has(key)) {
                                    seen.add(key);
                                    results.push({
                                        wallet: wallet,
                                        upi: upi,
                                        label: upi ? `${wallet} (${upi})` : wallet,
                                        mode: wallet.toLowerCase()
                                    });
                                }
                                break;
                            }
                            card = card.parentElement;
                        }
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception:
        pass
    return active_list


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
        buttons.append([InlineKeyboardButton(f"📱 {phone}", callback_data=f"setup_{phone}")])
    buttons.append([InlineKeyboardButton("➕ Add Account / Sync", callback_data="add_acc")])

    txt = "🤖 **DMAPay Auto-Buy**\n\nAccount select karein:"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in running_tasks and running_tasks[chat_id].get("active", False):
        running_tasks[chat_id]["active"] = False
        await update.message.reply_text("🛑 **Stopped!**", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ Koi active task nahi hai.")


async def add_acc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'WAIT_PHONE'
    await query.edit_message_text("👉 **10-digit Phone Number** bhejein:", parse_mode="Markdown")


async def setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    phone = query.data.replace("setup_", "")
    context.user_data['active_phone'] = phone
    context.user_data['state'] = 'WAIT_AMOUNT'
    await query.answer()
    await query.edit_message_text("💰 **Set Range**", parse_mode="Markdown")


async def handle_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    state = context.user_data.get('state', '')

    if (text.isdigit() and len(text) == 10) or state == 'WAIT_PHONE':
        if not text.isdigit() or len(text) != 10:
            await update.message.reply_text("❌ **10-digit Phone Number** dalein:", parse_mode="Markdown")
            return

        context.user_data['state'] = ''
        bm = generate_user_bookmarklet(chat_id, text)
        await poll_bookmark_sync(context)
        accounts = get_user_accounts(int(chat_id))
        extra_btn = []
        if text in accounts:
            extra_btn.append([InlineKeyboardButton(f"📱 {text}", callback_data=f"setup_{text}")])

        msg = (
            f"✅ **Number `{text}` Registered!**\n\n"
            f"🔖 **Bookmark Code:**\n\n`{bm}`\n\n"
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
            await update.message.reply_text("⚠️ Pehle `/start` karein.")
            return

    if not re.match(r'^\d+(\s*-\s*\d+)?$', text):
        await update.message.reply_text("💰 **Set Range**", parse_mode="Markdown")
        return

    context.user_data['target_amount'] = text.replace(" ", "")
    context.user_data['state'] = ''

    wait_msg = await update.message.reply_text("🔍 **Checking Active UPI...**", parse_mode="Markdown")

    accounts = get_user_accounts(int(chat_id))
    session_data = accounts.get(active_phone, {})
    active_upis = await fetch_active_upis(session_data)

    buttons = []
    if active_upis:
        context.user_data['active_upis'] = active_upis
        for idx, item in enumerate(active_upis):
            buttons.append([InlineKeyboardButton(f"✅ {item['label']}", callback_data=f"pay_{idx}")])
        await wait_msg.edit_text(
            f"💰 **Range:** `{context.user_data['target_amount']}`\n\n👇 **Active UPI select karein:**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
    else:
        context.user_data['active_upis'] = [
            {"wallet": "Amazon", "upi": "", "label": "Amazon", "mode": "amazon"},
            {"wallet": "Mobikwik", "upi": "", "label": "Mobikwik", "mode": "mobikwik"}
        ]
        buttons = [
            [
                InlineKeyboardButton("💳 Amazon", callback_data="pay_0"),
                InlineKeyboardButton("💳 Mobikwik", callback_data="pay_1")
            ]
        ]
        await wait_msg.edit_text(
            f"⚠️ Koi Active UPI detect nahi hua (`#/kycpartner` par check karein).\n"
            f"💰 **Range:** `{context.user_data['target_amount']}`\n\n👇 **Select UPI:**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )


async def payment_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx_str = query.data.replace("pay_", "")
    chat_id = update.effective_chat.id
    phone = context.user_data.get('active_phone', '')
    target_amt = context.user_data.get('target_amount', '1000')

    active_upis = context.user_data.get('active_upis', [])
    selected_upi = {"wallet": "Amazon", "upi": "", "label": "Amazon", "mode": "amazon"}
    if idx_str.isdigit() and int(idx_str) < len(active_upis):
        selected_upi = active_upis[int(idx_str)]

    accounts = get_user_accounts(chat_id)
    session_data = accounts.get(phone, {})

    status_msg = await query.edit_message_text(
        f"🚀 **Auto-Buy Active**\n"
        f"📱 `{phone}` | 💰 `{target_amt}` | 💳 `{selected_upi['label']}`\n\n"
        f"⏱️ **Timer:** `00:00` | 🔄 **Scans:** `0`\n"
        f"🛑 Stop: `/stop`",
        parse_mode="Markdown"
    )

    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await asyncio.sleep(1)

    running_tasks[chat_id] = {"active": True}
    asyncio.create_task(
        run_scanner_loop(chat_id, phone, session_data, target_amt, selected_upi, status_msg.message_id, context)
    )


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


# ---------------------------------------------------------------
# MAND MOBILE FAKE INPUT + CUSTOM NUMBER KEYBOARD (.md-number-keyboard)
# ---------------------------------------------------------------
async def type_on_mand_keyboard(page, field_label: str, value_str: str):
    """
    1. Clicks the visible .md-input-item-fake for 'Min' or 'Max'
    2. Clicks the clear button (.md-input-item-clear) or delete key on .md-number-keyboard
    3. Taps each digit on the visible .md-number-keyboard
    """
    box = await page.evaluate(r"""
        (labelTxt) => {
            let items = Array.from(document.querySelectorAll('.max-min .md-input-item'));
            for (let item of items) {
                let r = item.getBoundingClientRect();
                if (r.left < 0 || r.width === 0) continue;
                let lbl = item.querySelector('.md-field-item-title');
                if (lbl && (lbl.innerText || '').trim() === labelTxt) {
                    let fake = item.querySelector('.md-input-item-fake') || item;
                    fake.click();
                    let fr = fake.getBoundingClientRect();
                    return {x: fr.left + fr.width / 2, y: fr.top + fr.height / 2};
                }
            }
            return null;
        }
    """, field_label)

    if not box:
        return False

    await tap_point(page, box["x"], box["y"])
    await asyncio.sleep(0.35)

    # Click clear icon (.md-input-item-clear) if visible
    clear_box = await page.evaluate(r"""
        (labelTxt) => {
            let items = Array.from(document.querySelectorAll('.max-min .md-input-item'));
            for (let item of items) {
                let r = item.getBoundingClientRect();
                if (r.left < 0 || r.width === 0) continue;
                let lbl = item.querySelector('.md-field-item-title');
                if (lbl && (lbl.innerText || '').trim() === labelTxt) {
                    let clr = item.querySelector('.md-input-item-clear');
                    if (clr) {
                        let cr = clr.getBoundingClientRect();
                        if (cr.width > 0 && cr.height > 0) {
                            clr.click();
                            return {x: cr.left + cr.width / 2, y: cr.top + cr.height / 2};
                        }
                    }
                }
            }
            return null;
        }
    """, field_label)

    if clear_box:
        await tap_point(page, clear_box["x"], clear_box["y"])
        await asyncio.sleep(0.2)

    # Fallback: tap delete button on visible .md-number-keyboard if text still remains
    for _ in range(8):
        cur_txt = await page.evaluate(r"""
            (labelTxt) => {
                let items = Array.from(document.querySelectorAll('.max-min .md-input-item'));
                for (let item of items) {
                    let r = item.getBoundingClientRect();
                    if (r.left < 0 || r.width === 0) continue;
                    let lbl = item.querySelector('.md-field-item-title');
                    if (lbl && (lbl.innerText || '').trim() === labelTxt) {
                        let sp = item.querySelector('.md-input-item-fake span');
                        return sp ? (sp.innerText || '').trim() : '';
                    }
                }
                return '';
            }
        """, field_label)
        if not cur_txt:
            break
        del_box = await page.evaluate(r"""
            () => {
                let dels = Array.from(document.querySelectorAll('.md-number-keyboard li.keyboard-operate-item.delete'));
                for (let d of dels) {
                    let r = d.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                        d.click();
                        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                    }
                }
                return null;
            }
        """)
        if del_box:
            await tap_point(page, del_box["x"], del_box["y"])
            await asyncio.sleep(0.1)
        else:
            break

    # Tap each digit on the visible .md-number-keyboard
    for ch in value_str:
        key_box = await page.evaluate(r"""
            (digit) => {
                let keys = Array.from(document.querySelectorAll('.md-number-keyboard li.keyboard-number-item'));
                for (let k of keys) {
                    let r = k.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                        if ((k.innerText || '').trim() === digit) {
                            k.click();
                            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                        }
                    }
                }
                return null;
            }
        """, ch)
        if key_box:
            await tap_point(page, key_box["x"], key_box["y"])
            await asyncio.sleep(0.12)

    return True


async def set_min_max_via_keypad(page, min_val: float, max_val: float):
    min_s, max_s = str(int(min_val)), str(int(max_val))
    try:
        await type_on_mand_keyboard(page, "Min", min_s)
        await asyncio.sleep(0.25)
        await type_on_mand_keyboard(page, "Max", max_s)
        await asyncio.sleep(0.25)

        # Tap 'Ok' on the custom number keyboard
        ok_box = await page.evaluate(r"""
            () => {
                let oks = Array.from(document.querySelectorAll('.md-number-keyboard li.keyboard-operate-item.confirm'));
                for (let ok of oks) {
                    let r = ok.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                        ok.click();
                        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                    }
                }
                return null;
            }
        """)
        if ok_box:
            await tap_point(page, ok_box["x"], ok_box["y"])
            await asyncio.sleep(0.35)

        # Click search icon or arrow to trigger filter
        await trigger_refresh_arrows_or_search(page, use_search=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------
# REFRESH USING LEFT < UP / DOWN > ARROWS OR SEARCH ICON
# ---------------------------------------------------------------
async def trigger_refresh_arrows_or_search(page, use_search=False):
    """
    Left side me jo arrow-up (^) aur arrow-down (v) hain, unme se jo gray (inactive)
    hai us par click karta hai taaki list turant refresh ho jaye.
    """
    try:
        target_box = await page.evaluate(r"""
            (preferSearch) => {
                if (preferSearch) {
                    let imgs = Array.from(document.querySelectorAll('.search-item .md-cell-item-right img'));
                    for (let img of imgs) {
                        let r = img.getBoundingClientRect();
                        if (r.left > 0 && r.width > 0) {
                            img.click();
                            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                        }
                    }
                }
                let containers = Array.from(document.querySelectorAll('.max-min'));
                for (let c of containers) {
                    let cr = c.getBoundingClientRect();
                    if (cr.left < 0 || cr.width === 0) continue;
                    let up = c.querySelector('i.arrow-up');
                    let down = c.querySelector('i.arrow-down');
                    if (up && down) {
                        let upColor = (up.style.color || '').toLowerCase();
                        let target = (upColor === 'blue') ? down : up;
                        target.click();
                        let tr = target.getBoundingClientRect();
                        return {x: tr.left + tr.width / 2, y: tr.top + tr.height / 2};
                    }
                }
                return null;
            }
        """, use_search)
        if target_box:
            await tap_point(page, target_box["x"], target_box["y"])
    except Exception:
        pass


# ---------------------------------------------------------------
# SELECT ACTIVE WALLET IN POPUP (.md-selector.active_ct)
# ---------------------------------------------------------------
async def select_active_wallet_in_popup(page, selected_upi: dict):
    target_upi = (selected_upi.get("upi") or "").replace("*", "").lower()
    target_mode = (selected_upi.get("mode") or "amazon").lower()

    coords = await page.evaluate(r"""
        ({tMode, tUpi}) => {
            let popup = document.querySelector('.md-selector.active_ct .md-popup');
            let container = popup || document.body;
            let nodes = Array.from(container.querySelectorAll('.md-radio-item, .md-cell-item, div, li, label'));

            let targetEl = null;
            for (let el of nodes) {
                let t = (el.innerText || '').trim().toLowerCase();
                if (!t || t.startsWith('wallets:') || t.includes('pay using') || t.includes('manage upi')) continue;
                let r = el.getBoundingClientRect();
                if (r.top < 220 || r.top > 800 || r.height > 95 || r.height < 14 || r.width < 150) continue;

                if (tMode === 'mobikwik' && (t.includes('@mbk') || t.includes('mobikwik'))) {
                    targetEl = el;
                    break;
                } else if (tMode === 'amazon' && (t.includes('@yapl') || t.includes('amazon'))) {
                    targetEl = el;
                    break;
                } else if (t.includes('@')) {
                    targetEl = el;
                    break;
                }
            }

            if (!targetEl) return null;
            targetEl.click();
            let r = targetEl.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }
    """, {"tMode": target_mode, "tUpi": target_upi})

    if coords:
        await tap_point(page, coords["x"], coords["y"])
        return True
    return False


# ---------------------------------------------------------------
# POPUP HANDLERS: 'Stay' (Taken by someone else) vs 'OK' (Matched)
# ---------------------------------------------------------------
async def check_and_click_stay(page):
    """
    Jab order kisi aur ne le liya ho ('Order is processing', 'Failed to pick up order',
    'Please slow down') aur 'Stay' button dikhe, toh 'Stay' click karein.
    """
    stay_box = await page.evaluate(r"""
        () => {
            let els = Array.from(document.querySelectorAll('div, span, button, a'));
            let stayBtn = els.find(e => (e.innerText || '').trim() === 'Stay' && e.children.length === 0);
            if (stayBtn) {
                let r = stayBtn.getBoundingClientRect();
                if (r.width > 10 && r.height > 10) {
                    stayBtn.click();
                    return {x: r.left, y: r.top, width: r.width, height: r.height};
                }
            }
            return null;
        }
    """)
    if stay_box:
        await tap_element_center(page, stay_box)
        return True
    return False


async def check_and_click_success_ok(page):
    """
    Jab order match ho jaye ('The pre-order is successful...') ya 'Go pay' aaye,
    toh 'OK' / 'Go pay' par click karein.
    """
    ok_box = await page.evaluate(r"""
        () => {
            let bodyTxt = document.body.innerText || '';
            let els = Array.from(document.querySelectorAll('button, div, span, a'));

            if (bodyTxt.includes('need to pay!')) {
                let gp = els.find(e => (e.innerText || '').trim() === 'Go pay' && e.children.length === 0);
                if (gp) {
                    let r = gp.getBoundingClientRect();
                    if (r.width > 10 && r.height > 10) {
                        gp.click();
                        return {x: r.left, y: r.top, width: r.width, height: r.height};
                    }
                }
            }

            if (bodyTxt.toLowerCase().includes('pre-order is successful') || bodyTxt.toLowerCase().includes('selected payment tool')) {
                for (let b of els) {
                    let t = (b.innerText || '').trim();
                    if ((t === 'OK' || t === 'Confirm') && b.children.length === 0) {
                        let r = b.getBoundingClientRect();
                        if (r.width > 15 && r.height > 15 && r.top > 200) {
                            b.click();
                            return {x: r.left, y: r.top, width: r.width, height: r.height};
                        }
                    }
                }
            }
            return null;
        }
    """)
    if ok_box:
        await tap_element_center(page, ok_box)
        return True
    return False


async def is_bank_details_open(page):
    """
    Checks if final Bank Details page (#/buyierdetail... or #/buyingbymod...) is open.
    """
    cur_url = page.url
    b_text = await page.evaluate("() => document.body.innerText || ''")
    if ("buyierdetail" in cur_url or "buyingbymod" in cur_url) and (
        "IFSC" in b_text or "Account" in b_text or "Upload Proof" in b_text
    ):
        # Ensure the 'OK' modal overlay is dismissed before taking final screenshot
        await check_and_click_success_ok(page)
        return True
    return False


async def get_matching_orders(page, min_val, max_val):
    result = await page.evaluate(r"""
        ({minV, maxV}) => {
            let allPrices = [];
            let matched = [];
            let items = Array.from(document.querySelectorAll('div.buyitem'));
            items.forEach((item, idx) => {
                let rItem = item.getBoundingClientRect();
                if (rItem.width === 0 || rItem.left < 0 || rItem.left >= window.innerWidth) return;
                let cText = item.innerText || '';
                let m = cText.match(/₹\s*([0-9,]+(\.[0-9]+)?)/);
                if (m && m[1]) {
                    let val = parseFloat(m[1].replace(/,/g, ''));
                    allPrices.push(val);
                    if (val >= minV && val <= maxV) {
                        matched.push({idx: idx, amount: val});
                    }
                }
            });
            return {matched: matched, prices: allPrices};
        }
    """, {"minV": min_val, "maxV": max_val})
    return result or {"matched": [], "prices": []}


async def click_order_by_index(page, idx):
    result = await page.evaluate(r"""
        (i) => {
            let items = document.querySelectorAll('div.buyitem');
            let item = items[i];
            if (!item) return null;
            let btn = item.querySelector('button');
            if (!btn) return null;
            item.scrollIntoView({behavior: 'instant', block: 'center'});
            let br = btn.getBoundingClientRect();
            btn.click();
            return {x: br.left, y: br.top, width: br.width, height: br.height};
        }
    """, idx)
    if result and 0 < result["y"] < 840:
        await tap_element_center(page, result)
        return True
    return bool(result)


# ---------------------------------------------------------------
# MAIN SCANNER LOOP
# ---------------------------------------------------------------
async def run_scanner_loop(chat_id, phone, session_data, target_amt, selected_upi, message_id, context):
    start_time = time.time()
    scan_count = 0
    retry_count = 0

    min_val, max_val = 0.0, 0.0
    if '-' in target_amt:
        parts = target_amt.split('-')
        try:
            min_val = float(parts[0].strip())
            max_val = float(parts[1].strip())
        except Exception:
            min_val = max_val = 1000.0
    else:
        try:
            min_val = max_val = float(target_amt.replace('₹', '').strip())
        except Exception:
            min_val = max_val = 1000.0

    if min_val > max_val:
        min_val, max_val = max_val, min_val

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                ]
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
                await asyncio.sleep(0.8)
                await page.evaluate(
                    r"""(data) => { for (let k in data) { localStorage.setItem(k, data[k]); } }""",
                    session_data
                )

            await page.goto("https://web.dmapay.vip/#/buy", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2.5)

            # Set Min and Max using the custom Mand Mobile number keyboard
            await set_min_max_via_keypad(page, min_val, max_val)
            await asyncio.sleep(0.8)

            while running_tasks.get(chat_id, {}).get("active", False):
                scan_count += 1
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer_str = f"{mins:02d}:{secs:02d}"

                # 1. Check if Bank Details page is already open
                if await is_bank_details_open(page):
                    await asyncio.sleep(0.6)
                    running_tasks[chat_id]["active"] = False
                    shot = await page.screenshot(full_page=True)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=shot,
                        caption=f"🎉 **Order Matched!**\n💰 `₹{target_amt}` | 💳 `{selected_upi['label']}`",
                        parse_mode="Markdown"
                    )
                    break

                # 2. Check if 'Stay' popup is on screen
                if await check_and_click_stay(page):
                    retry_count += 1
                    await asyncio.sleep(0.6)

                # 3. Check if 'OK' / 'Go pay' notice is on screen
                if await check_and_click_success_ok(page):
                    await asyncio.sleep(1.0)
                    if await is_bank_details_open(page):
                        running_tasks[chat_id]["active"] = False
                        shot = await page.screenshot(full_page=True)
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot,
                            caption=f"🎉 **Order Matched!**\n💰 `₹{target_amt}` | 💳 `{selected_upi['label']}`",
                            parse_mode="Markdown"
                        )
                        break

                # 4. Scan orders matching range
                scan_data = await get_matching_orders(page, min_val, max_val)
                matched_list = scan_data.get("matched", [])

                order_matched_final = False

                for m_item in matched_list:
                    if not running_tasks.get(chat_id, {}).get("active", False):
                        break

                    idx = m_item["idx"]
                    amt = m_item["amount"]

                    clicked = await click_order_by_index(page, idx)
                    if not clicked:
                        continue

                    await asyncio.sleep(0.5)

                    # If someone else already took it right on Buy click
                    if await check_and_click_stay(page):
                        retry_count += 1
                        await asyncio.sleep(0.6)
                        continue

                    # SCREENSHOT 1: Jab Buy par click ho
                    try:
                        shot_buy = await page.screenshot()
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot_buy,
                            caption=f"📸 **Buy Clicked (₹{int(amt)})**",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    # Select the active UPI in the popup
                    await select_active_wallet_in_popup(page, selected_upi)

                    # Poll for outcome: either 'Stay' (taken by someone else) or 'OK' / Bank Details (Matched!)
                    outcome = "pending"
                    for _ in range(25):
                        await asyncio.sleep(0.35)

                        if await check_and_click_stay(page):
                            outcome = "stay_retry"
                            break

                        if await check_and_click_success_ok(page):
                            await asyncio.sleep(0.8)

                        if await is_bank_details_open(page):
                            outcome = "matched"
                            break

                    if outcome == "stay_retry":
                        retry_count += 1
                        await asyncio.sleep(0.6)
                        continue

                    if outcome == "matched":
                        order_matched_final = True
                        await asyncio.sleep(0.8)
                        running_tasks[chat_id]["active"] = False
                        # SCREENSHOT 2: Jab order match ho jaye aur Bank Details khul jaye
                        shot_final = await page.screenshot(full_page=True)
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot_final,
                            caption=(
                                f"🎉 **Order Matched & Bank Details Opened!**\n"
                                f"💰 **Amount:** `₹{int(amt)}`\n"
                                f"💳 **UPI:** `{selected_upi['label']}`\n"
                                f"⏱️ **Time:** `{timer_str}`"
                            ),
                            parse_mode="Markdown"
                        )
                        break

                if order_matched_final:
                    break

                # Update status message periodically
                if scan_count % 4 == 0:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=(
                                f"🚀 **Auto-Buy Active**\n"
                                f"📱 `{phone}` | 💰 `{target_amt}` | 💳 `{selected_upi['label']}`\n\n"
                                f"⏱️ **Timer:** `{timer_str}` | 🔄 **Scans:** `{scan_count}` | **Retries:** `{retry_count}`\n"
                                f"🛑 Stop: `/stop`"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                # Refresh using left < up / down > arrows (and search icon every 5th scan)
                await trigger_refresh_arrows_or_search(page, use_search=(scan_count % 5 == 0))
                await asyncio.sleep(0.9)

            await browser.close()
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: `{str(e)}`", parse_mode="Markdown")
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
