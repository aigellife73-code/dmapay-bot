import asyncio, json, os, threading, time, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)
from playwright.async_api import async_playwright

BOT_TOKEN = "7927071708:AAGZS1WfeoqG3ydoV3zs_qyAGFmdt0TZ0GM"
ACCOUNTS_FILE = "dmapay_accounts.json"
PREFS_FILE = "dmapay_prefs.json"

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


# ---------------------------------------------------------------
# SAVE & LOAD USER LAST RANGE / UPI PREFERENCES
# ---------------------------------------------------------------
def get_user_prefs(user_id: int) -> dict:
    all_prefs = load_json(PREFS_FILE, {})
    return dict(all_prefs.get(str(user_id), {}))


def save_user_pref(user_id: int, phone: str, target_amt: str, selected_upi: dict):
    all_prefs = load_json(PREFS_FILE, {})
    uid_str = str(user_id)
    user_p = all_prefs.get(uid_str, {})

    recent_ranges = user_p.get("recent_ranges", [])
    if target_amt in recent_ranges:
        recent_ranges.remove(target_amt)
    recent_ranges.insert(0, target_amt)
    recent_ranges = recent_ranges[:4]

    all_prefs[uid_str] = {
        "phone": phone,
        "target_amt": target_amt,
        "selected_upi": selected_upi,
        "recent_ranges": recent_ranges
    }
    save_json(PREFS_FILE, all_prefs)


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
                    parse_mode="Markdown",
                    disable_notification=True
                )
    except Exception:
        pass


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
    prefs = get_user_prefs(user.id)

    buttons = []

    # Direct 1-click button for previously configured Range + UPI
    last_phone = prefs.get("phone")
    last_amt = prefs.get("target_amt")
    last_upi = prefs.get("selected_upi")
    if last_phone and last_amt and last_upi and last_phone in accounts:
        w_name = last_upi.get("wallet") or last_upi.get("mode", "UPI").capitalize()
        mode_str = last_upi.get("mode", "mobikwik")
        buttons.append([
            InlineKeyboardButton(
                f"⚡ Direct Start: ₹{last_amt} | {w_name}",
                callback_data=f"repeat_{last_phone}_{last_amt}_{mode_str}"
            )
        ])

    for phone in accounts.keys():
        buttons.append([InlineKeyboardButton(f"📱 {phone}", callback_data=f"setup_{phone}")])
    buttons.append([InlineKeyboardButton("➕ Add Account / Sync", callback_data="add_acc")])

    txt = "🤖 **DMAPay Auto-Buy**\n\nAccount select karein ya Direct Start dabayein:"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    else:
        await update.message.reply_text(
            txt,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
            disable_notification=True
        )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in running_tasks and running_tasks[chat_id].get("active", False):
        running_tasks[chat_id]["active"] = False
        await update.message.reply_text("🛑 **Stopped!**", parse_mode="Markdown", disable_notification=True)
    else:
        await update.message.reply_text("ℹ️ Koi active task nahi hai.", disable_notification=True)


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

    prefs = get_user_prefs(update.effective_user.id)
    recent_ranges = prefs.get("recent_ranges", [])
    last_upi = prefs.get("selected_upi")

    buttons = []
    # Quick buttons for previously used ranges
    if recent_ranges:
        row = []
        for r_amt in recent_ranges:
            row.append(InlineKeyboardButton(f"💰 {r_amt}", callback_data=f"qrange_{phone}_{r_amt}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    # Also offer direct 1-tap repeat with last UPI if available
    if prefs.get("target_amt") and last_upi:
        w_name = last_upi.get("wallet") or last_upi.get("mode", "UPI").capitalize()
        mode_str = last_upi.get("mode", "mobikwik")
        buttons.append([
            InlineKeyboardButton(
                f"🔄 Repeat Last: ₹{prefs['target_amt']} ({w_name})",
                callback_data=f"repeat_{phone}_{prefs['target_amt']}_{mode_str}"
            )
        ])

    await query.edit_message_text(
        "Set Range",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        parse_mode="Markdown"
    )


async def ask_upi_for_range(chat_id: int, active_phone: str, target_amt: str, context: ContextTypes.DEFAULT_TYPE, msg_to_edit=None):
    if msg_to_edit is None:
        wait_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 **Checking Active UPI...**",
            parse_mode="Markdown",
            disable_notification=True
        )
    else:
        wait_msg = await msg_to_edit.edit_text("🔍 **Checking Active UPI...**", parse_mode="Markdown")

    accounts = get_user_accounts(int(chat_id))
    session_data = accounts.get(active_phone, {})
    active_upis = await fetch_active_upis(session_data)

    buttons = []
    if active_upis:
        for idx, item in enumerate(active_upis):
            cdata = f"pay_{target_amt}_{idx}"
            buttons.append([InlineKeyboardButton(f"✅ {item['label']}", callback_data=cdata)])
        context.user_data['temp_active_upis'] = active_upis
        await wait_msg.edit_text(
            f"Set Range: `{target_amt}`\n\n👇 **Active UPI select karein:**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
    else:
        fallback_upis = [
            {"wallet": "Amazon", "upi": "", "label": "Amazon", "mode": "amazon"},
            {"wallet": "Mobikwik", "upi": "", "label": "Mobikwik", "mode": "mobikwik"}
        ]
        context.user_data['temp_active_upis'] = fallback_upis
        buttons = [
            [
                InlineKeyboardButton("💳 Amazon", callback_data=f"pay_{target_amt}_0"),
                InlineKeyboardButton("💳 Mobikwik", callback_data=f"pay_{target_amt}_1")
            ]
        ]
        await wait_msg.edit_text(
            f"Set Range: `{target_amt}`\n\n👇 **Select UPI:**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )


async def quick_range_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_", 2)
    phone = parts[1] if len(parts) > 1 else ""
    target_amt = parts[2] if len(parts) > 2 else "1000"

    context.user_data['active_phone'] = phone
    context.user_data['state'] = ''
    await ask_upi_for_range(update.effective_chat.id, phone, target_amt, context, msg_to_edit=query.message)


async def handle_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    state = context.user_data.get('state', '')

    if (text.isdigit() and len(text) == 10) or state == 'WAIT_PHONE':
        if not text.isdigit() or len(text) != 10:
            await update.message.reply_text(
                "❌ **10-digit Phone Number** dalein:",
                parse_mode="Markdown",
                disable_notification=True
            )
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
            parse_mode="Markdown",
            disable_notification=True
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
            await update.message.reply_text("⚠️ Pehle `/start` karein.", disable_notification=True)
            return

    if not re.match(r'^\d+(\s*-\s*\d+)?$', text):
        await update.message.reply_text("Set Range", parse_mode="Markdown", disable_notification=True)
        return

    target_amt = text.replace(" ", "")
    context.user_data['state'] = ''
    await ask_upi_for_range(int(chat_id), active_phone, target_amt, context)


async def payment_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    target_amt = parts[1] if len(parts) > 1 else "1000"
    idx_int = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    chat_id = update.effective_chat.id
    phone = context.user_data.get('active_phone', '')
    if not phone:
        accounts = get_user_accounts(chat_id)
        if accounts:
            phone = list(accounts.keys())[0]

    active_upis = context.user_data.get('temp_active_upis', [
        {"wallet": "Amazon", "upi": "", "label": "Amazon", "mode": "amazon"},
        {"wallet": "Mobikwik", "upi": "", "label": "Mobikwik", "mode": "mobikwik"}
    ])
    selected_upi = active_upis[idx_int] if idx_int < len(active_upis) else active_upis[0]

    # Save preference for Repeat Order & /start quick button
    save_user_pref(chat_id, phone, target_amt, selected_upi)

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


# ---------------------------------------------------------------
# REPEAT ORDER HANDLER (1-CLICK RESTART FROM MATCH OR /start)
# ---------------------------------------------------------------
async def repeat_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🔄 Starting Repeat Order...")

    # Format: repeat_{phone}_{target_amt}_{mode}
    parts = query.data.split("_", 3)
    phone = parts[1] if len(parts) > 1 else ""
    target_amt = parts[2] if len(parts) > 2 else "1000"
    mode = parts[3] if len(parts) > 3 else "mobikwik"

    chat_id = update.effective_chat.id
    accounts = get_user_accounts(chat_id)
    if not phone or phone not in accounts:
        if accounts:
            phone = list(accounts.keys())[0]
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Account sync nahi hai. `/start` karein.",
                disable_notification=True
            )
            return

    context.user_data['active_phone'] = phone
    prefs = get_user_prefs(chat_id)
    saved_upi = prefs.get("selected_upi", {})

    if saved_upi and saved_upi.get("mode") == mode:
        selected_upi = saved_upi
    else:
        selected_upi = {
            "wallet": mode.capitalize(),
            "upi": "",
            "label": mode.capitalize(),
            "mode": mode
        }

    save_user_pref(chat_id, phone, target_amt, selected_upi)
    session_data = accounts.get(phone, {})

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🚀 **Auto-Buy Active (Repeat)**\n"
            f"📱 `{phone}` | 💰 `{target_amt}` | 💳 `{selected_upi['label']}`\n\n"
            f"⏱️ **Timer:** `00:00` | 🔄 **Scans:** `0`\n"
            f"🛑 Stop: `/stop`"
        ),
        parse_mode="Markdown",
        disable_notification=True
    )

    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
        await asyncio.sleep(1)

    running_tasks[chat_id] = {"active": True}
    asyncio.create_task(
        run_scanner_loop(chat_id, phone, session_data, target_amt, selected_upi, status_msg.message_id, context)
    )


async def single_tap(page, x, y):
    try:
        await page.touchscreen.tap(x, y)
    except Exception:
        try:
            await page.mouse.click(x, y)
        except Exception:
            pass


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


async def get_fake_input_value(page, field_label: str) -> str:
    return await page.evaluate(r"""
        (labelTxt) => {
            let items = Array.from(document.querySelectorAll('.max-min .md-input-item'));
            for (let item of items) {
                let r = item.getBoundingClientRect();
                if (r.left < 0 || r.width === 0) continue;
                let lbl = item.querySelector('.md-field-item-title');
                if (lbl && (lbl.innerText || '').trim() === labelTxt) {
                    let sp = item.querySelector('.md-input-item-fake span:not(.md-input-item-fake-placeholder)');
                    let raw = sp ? (sp.innerText || '') : '';
                    return raw.replace(/[^0-9]/g, '');
                }
            }
            return '';
        }
    """, field_label)


async def tap_keypad_delete(page):
    del_box = await page.evaluate(r"""
        () => {
            let dels = Array.from(document.querySelectorAll('.md-number-keyboard li.keyboard-operate-item.delete'));
            for (let d of dels) {
                let r = d.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                }
            }
            return null;
        }
    """)
    if del_box:
        await single_tap(page, del_box["x"], del_box["y"])
        return True
    return False


async def tap_keypad_digit(page, digit: str):
    key_box = await page.evaluate(r"""
        (dStr) => {
            let keys = Array.from(document.querySelectorAll('.md-number-keyboard li.keyboard-number-item'));
            for (let k of keys) {
                let r = k.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                    if ((k.innerText || '').trim() === dStr) {
                        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                    }
                }
            }
            return null;
        }
    """, digit)
    if key_box:
        await single_tap(page, key_box["x"], key_box["y"])
        return True
    return False


async def tap_keypad_ok(page):
    ok_box = await page.evaluate(r"""
        () => {
            let oks = Array.from(document.querySelectorAll('.md-number-keyboard li.keyboard-operate-item.confirm'));
            for (let ok of oks) {
                let r = ok.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                }
            }
            return null;
        }
    """)
    if ok_box:
        await single_tap(page, ok_box["x"], ok_box["y"])
        return True
    return False


async def type_on_mand_keyboard(page, field_label: str, value_str: str):
    box = await page.evaluate(r"""
        (labelTxt) => {
            let items = Array.from(document.querySelectorAll('.max-min .md-input-item'));
            for (let item of items) {
                let r = item.getBoundingClientRect();
                if (r.left < 0 || r.width === 0) continue;
                let lbl = item.querySelector('.md-field-item-title');
                if (lbl && (lbl.innerText || '').trim() === labelTxt) {
                    let fake = item.querySelector('.md-input-item-fake') || item;
                    let fr = fake.getBoundingClientRect();
                    return {x: fr.left + fr.width / 2, y: fr.top + fr.height / 2};
                }
            }
            return null;
        }
    """, field_label)

    if not box:
        return False

    await single_tap(page, box["x"], box["y"])
    await asyncio.sleep(0.5)

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
                            return {x: cr.left + cr.width / 2, y: cr.top + cr.height / 2};
                        }
                    }
                }
            }
            return null;
        }
    """, field_label)

    if clear_box:
        await single_tap(page, clear_box["x"], clear_box["y"])
        await asyncio.sleep(0.2)

    for _ in range(12):
        cur = await get_fake_input_value(page, field_label)
        if not cur:
            break
        await tap_keypad_delete(page)
        await asyncio.sleep(0.15)

    for _ in range(25):
        cur = await get_fake_input_value(page, field_label)
        if cur == value_str:
            break

        if not value_str.startswith(cur):
            await tap_keypad_delete(page)
            await asyncio.sleep(0.16)
        else:
            next_digit = value_str[len(cur)]
            await tap_keypad_digit(page, next_digit)
            await asyncio.sleep(0.22)

    await tap_keypad_ok(page)
    await asyncio.sleep(0.4)
    return True


async def set_min_max_via_keypad(page, min_val: float, max_val: float):
    min_s, max_s = str(int(min_val)), str(int(max_val))
    try:
        for _ in range(2):
            cur_min = await get_fake_input_value(page, "Min")
            if cur_min != min_s:
                await type_on_mand_keyboard(page, "Min", min_s)

            cur_max = await get_fake_input_value(page, "Max")
            if cur_max != max_s:
                await type_on_mand_keyboard(page, "Max", max_s)

            final_min = await get_fake_input_value(page, "Min")
            final_max = await get_fake_input_value(page, "Max")
            if final_min == min_s and final_max == max_s:
                break

        await trigger_refresh_arrows_or_search(page, use_search=True)
        return True
    except Exception:
        return False


async def trigger_refresh_arrows_or_search(page, use_search=False):
    try:
        target_box = await page.evaluate(r"""
            (preferSearch) => {
                if (preferSearch) {
                    let imgs = Array.from(document.querySelectorAll('.search-item .md-cell-item-right img'));
                    for (let img of imgs) {
                        let r = img.getBoundingClientRect();
                        if (r.left > 0 && r.width > 0) {
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
                        let tr = target.getBoundingClientRect();
                        return {x: tr.left + tr.width / 2, y: tr.top + tr.height / 2};
                    }
                }
                return null;
            }
        """, use_search)
        if target_box:
            await single_tap(page, target_box["x"], target_box["y"])
    except Exception:
        pass


async def select_active_wallet_in_popup(page, selected_upi: dict):
    target_mode = (selected_upi.get("mode") or "amazon").lower()
    coords = await page.evaluate(r"""
        (tMode) => {
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
    """, target_mode)

    if coords:
        await tap_point(page, coords["x"], coords["y"])
        return True
    return False


async def check_and_click_stay(page):
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
    b_text = await page.evaluate("() => document.body.innerText || ''")
    is_bank_page = (
        "Please pay in time" in b_text
        or "Upload Proof" in b_text
        or "Payment info" in b_text
        or "Cancel my order" in b_text
        or ("Account" in b_text and "IFSC" in b_text and "Bank" in b_text)
    )
    if is_bank_page:
        if await check_and_click_success_ok(page):
            await asyncio.sleep(0.5)
        return True
    return False


# ---------------------------------------------------------------
# FINAL MATCH ALERT (WITH SOUND + REPEAT ORDER BUTTON)
# ---------------------------------------------------------------
async def stop_and_send_bank_details(chat_id, page, phone, target_amt, matched_amt_str, selected_upi, timer_str, context):
    if chat_id in running_tasks:
        running_tasks[chat_id]["active"] = False
    await asyncio.sleep(0.6)
    await check_and_click_success_ok(page)
    await asyncio.sleep(0.4)

    shot_final = await page.screenshot(full_page=True)
    w_name = selected_upi.get("wallet") or selected_upi.get("mode", "UPI").capitalize()
    mode_str = selected_upi.get("mode", "mobikwik")

    repeat_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🔄 Repeat Order (₹{target_amt} | {w_name})",
                callback_data=f"repeat_{phone}_{target_amt}_{mode_str}"
            )
        ]
    ])

    # ONLY THIS MESSAGE HAS NOTIFICATION SOUND ENABLED (disable_notification=False)
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=shot_final,
        caption=(
            f"🎉 **Order Matched & Bank Details Opened!**\n"
            f"💰 **Amount:** `₹{matched_amt_str}`\n"
            f"💳 **UPI:** `{selected_upi['label']}`\n"
            f"⏱️ **Time:** `{timer_str}`\n"
            f"🛑 **Auto-Buy Stopped!**"
        ),
        reply_markup=repeat_markup,
        parse_mode="Markdown",
        disable_notification=False
    )


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
            if (!item) return false;
            let btn = item.querySelector('button');
            if (!btn) return false;
            item.scrollIntoView({behavior: 'instant', block: 'center'});
            btn.click();
            return true;
        }
    """, idx)
    return result


async def run_scanner_loop(chat_id, phone, session_data, target_amt, selected_upi, message_id, context):
    start_time = time.time()
    scan_count = 0
    retry_count = 0
    last_photo_msg_id = None

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

            await set_min_max_via_keypad(page, min_val, max_val)
            await asyncio.sleep(0.8)

            while running_tasks.get(chat_id, {}).get("active", False):
                scan_count += 1
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer_str = f"{mins:02d}:{secs:02d}"

                # 1. Immediate stop if Bank Details page is open
                if await is_bank_details_open(page):
                    await stop_and_send_bank_details(chat_id, page, phone, target_amt, target_amt, selected_upi, timer_str, context)
                    break

                # 2. Dismiss 'Stay' if order was taken by someone else
                if await check_and_click_stay(page):
                    retry_count += 1
                    await asyncio.sleep(0.6)

                # 3. Click 'OK' / 'Go pay' if success notice is showing, then stop if Bank Details open
                if await check_and_click_success_ok(page):
                    await asyncio.sleep(0.8)
                    if await is_bank_details_open(page):
                        await stop_and_send_bank_details(chat_id, page, phone, target_amt, target_amt, selected_upi, timer_str, context)
                        break

                scan_data = await get_matching_orders(page, min_val, max_val)
                matched_list = scan_data.get("matched", [])
                visible_prices = scan_data.get("prices", [])

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

                    if await check_and_click_stay(page):
                        retry_count += 1
                        await asyncio.sleep(0.6)
                        continue

                    # SCREENSHOT 1: Buy Clicked (SILENT)
                    try:
                        shot_buy = await page.screenshot()
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=shot_buy,
                            caption=f"📸 **Buy Clicked (₹{int(amt)})**",
                            parse_mode="Markdown",
                            disable_notification=True
                        )
                    except Exception:
                        pass

                    await select_active_wallet_in_popup(page, selected_upi)

                    outcome = "pending"
                    for _ in range(25):
                        await asyncio.sleep(0.35)

                        if await check_and_click_stay(page):
                            outcome = "stay_retry"
                            break

                        if await check_and_click_success_ok(page):
                            await asyncio.sleep(0.6)

                        if await is_bank_details_open(page):
                            outcome = "matched"
                            break

                    if outcome == "stay_retry":
                        retry_count += 1
                        await asyncio.sleep(0.6)
                        continue

                    if outcome == "matched":
                        order_matched_final = True
                        await stop_and_send_bank_details(
                            chat_id, page, phone, target_amt, str(int(amt)), selected_upi, timer_str, context
                        )
                        break

                if order_matched_final or not running_tasks.get(chat_id, {}).get("active", False):
                    break

                if await is_bank_details_open(page):
                    await stop_and_send_bank_details(chat_id, page, phone, target_amt, target_amt, selected_upi, timer_str, context)
                    break

                # LIVE SCREENSHOT EDIT (SILENT)
                if scan_count % 3 == 1 or scan_count == 1:
                    try:
                        current_shot = await page.screenshot()
                        unique_prices = sorted(list(set(int(p) for p in visible_prices)))
                        prices_str = ", ".join([f"₹{p}" for p in unique_prices[:10]]) if unique_prices else "None"
                        caption_text = (
                            f"🚀 **Auto-Buy Active**\n"
                            f"📱 `{phone}` | 💰 `{target_amt}` | 💳 `{selected_upi['label']}`\n\n"
                            f"⏱️ **Timer:** `{timer_str}` | 🔄 **Scans:** `{scan_count}` | 🔄 **Retries:** `{retry_count}`\n"
                            f"👀 **Prices:** `{prices_str}`\n"
                            f"🛑 Stop: `/stop`"
                        )

                        if last_photo_msg_id is None:
                            sent_msg = await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=current_shot,
                                caption=caption_text,
                                parse_mode="Markdown",
                                disable_notification=True
                            )
                            last_photo_msg_id = sent_msg.message_id
                        else:
                            try:
                                await context.bot.edit_message_media(
                                    chat_id=chat_id,
                                    message_id=last_photo_msg_id,
                                    media=InputMediaPhoto(media=current_shot, caption=caption_text, parse_mode="Markdown")
                                )
                            except Exception:
                                sent_msg = await context.bot.send_photo(
                                    chat_id=chat_id,
                                    photo=current_shot,
                                    caption=caption_text,
                                    parse_mode="Markdown",
                                    disable_notification=True
                                )
                                last_photo_msg_id = sent_msg.message_id
                    except Exception:
                        pass

                await trigger_refresh_arrows_or_search(page, use_search=(scan_count % 5 == 0))
                await asyncio.sleep(1.2)

            await browser.close()
    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Error: `{str(e)}`",
            parse_mode="Markdown",
            disable_notification=True
        )
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
    app.add_handler(CallbackQueryHandler(quick_range_callback, pattern="^qrange_"))
    app.add_handler(CallbackQueryHandler(payment_choice_callback, pattern="^pay_"))
    app.add_handler(CallbackQueryHandler(repeat_order_callback, pattern="^repeat_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_all_text))

    print("DMAPay Bot Live...")
    app.run_polling()


if __name__ == "__main__":
    main()
