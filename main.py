"""
FastAPI Backend - Web + Telegram Bot for Hotmail Rule Manager
"""

import os
import asyncio
import json
import threading
from datetime import datetime
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
import traceback

from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import requests


# ==================== ACCOUNT STORE ====================
ACCOUNTS_DB = 'accounts.json'

class AccountStore:
    @staticmethod
    def load():
        if os.path.exists(ACCOUNTS_DB):
            try:
                with open(ACCOUNTS_DB, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    @staticmethod
    def save(accounts):
        with open(ACCOUNTS_DB, 'w', encoding='utf-8') as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)

    @classmethod
    def upsert(cls, email: str, password: str, **kwargs):
        data = cls.load()
        for a in data:
            if a['email'].lower() == email.lower():
                a['password'] = password
                a.update(kwargs)
                cls.save(data)
                return
        rec = {'email': email, 'password': password, 'last_check': '',
               'rules_total': 0, 'rules_enabled': 0, 'status': ''}
        rec.update(kwargs)
        data.append(rec)
        cls.save(data)

    @classmethod
    def remove(cls, emails: List[str]):
        emails = {e.lower() for e in emails}
        data = [a for a in cls.load() if a['email'].lower() not in emails]
        cls.save(data)


# ==================== CONFIG ====================
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '')
PORT = int(os.getenv('PORT', '8000'))
CLIENT_ID = 'e9b154d0-7658-433b-bb25-6b8e0a8a7c59'
REDIRECT_URI = 'msauth://com.microsoft.outlooklite/fcg80qvoM1YMKJZibjBwQcDfOno%3D'
SCOPE = 'profile openid offline_access https://outlook.office.com/M365.Access'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
DEFAULT_DOMAIN = 'tm.cameyou.shop'


# ==================== PYDANTIC MODELS ====================
class AccountCreate(BaseModel):
    email: str
    password: str

class AccountBatchCreate(BaseModel):
    accounts: List[str]

class CheckRequest(BaseModel):
    emails: Optional[List[str]] = None
    auto_enable: bool = True
    auto_create: bool = True
    domain: str = DEFAULT_DOMAIN
    api: str = 'outlook'
    threads: int = 5

class CreateRulesRequest(BaseModel):
    accounts: List[str]
    rules: List[str]
    domain: str = DEFAULT_DOMAIN
    api: str = 'outlook'
    threads: int = 3


# ==================== GLOBAL STATE ====================
connected_ws = []
executor = ThreadPoolExecutor(max_workers=10)


# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    set_loop(asyncio.get_event_loop())
    if BOT_TOKEN:
        asyncio.create_task(start_telegram_bot())
    yield
    with _loop_lock:
        global _loop
        _loop = None


# ==================== FASTAPI APP ====================
app = FastAPI(title="Hotmail Rule Manager", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ==================== WEBSOCKET ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_ws.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        if websocket in connected_ws:
            connected_ws.remove(websocket)


# Global event loop for broadcasting
_loop = None
_loop_lock = threading.Lock()

def set_loop(loop):
    global _loop
    with _loop_lock:
        _loop = loop

def broadcast(data: dict):
    """Thread-safe broadcast"""
    global _loop
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            for ws in connected_ws:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_json(data), _loop)
                except Exception as e:
                    print(f"[WS ERROR] {e}")
        else:
            for ws in connected_ws:
                try:
                    asyncio.run(ws.send_json(data))
                except Exception as e:
                    print(f"[WS ERROR] {e}")


def broadcast_from_thread(data: dict):
    """Call from background thread - thread-safe broadcast"""
    global _loop
    with _loop_lock:
        if _loop is not None:
            for ws in connected_ws:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_json(data), _loop)
                except Exception as e:
                    print(f"[BROADCAST ERROR] {e}")


def log_worker(msg: str):
    """Log from worker thread"""
    print(f"[WORKER] {msg}")


# ==================== RULE ENGINE (CORE) ====================
def login(email: str, password: str) -> str:
    """Login to Microsoft and return access token"""
    import urllib.parse
    import re

    s = requests.Session()
    s.headers.update({'User-Agent': USER_AGENT})

    auth_url = (
        'https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize'
        f'?client_info=1&haschrome=1&login_hint={urllib.parse.quote(email)}'
        f'&response_type=code&client_id={CLIENT_ID}'
        f'&scope={urllib.parse.quote(SCOPE)}'
        f'&redirect_uri={urllib.parse.quote(REDIRECT_URI)}'
    )

    log_worker(f"[LOGIN] Step 1: Getting auth page for {email}")
    r = s.get(auth_url, timeout=30)
    log_worker(f"[LOGIN] Step 2: Got auth page, searching for urlPost/PPFT")

    url_match = (
        re.search(r'urlPost":"([^"]+)"', r.text) or
        re.search(r"urlPost:'([^']+)'", r.text) or
        re.search(r'(https://login\.live\.com/ppsecure/post\.srf\?[^"\\\']+)', r.text)
    )
    ppft_match = (
        re.search(r'name="PPFT"[^>]*value="([^"]+)"', r.text) or
        re.search(r"name='PPFT'[^>]*value='([^']+)'", r.text) or
        re.search(r'name=\\"PPFT\\".*?value=\\"([^\\]+?)\\"', r.text) or
        re.search(r'"sFT":"([^"]+)"', r.text)
    )

    if not url_match or not ppft_match:
        log_worker(f"[LOGIN] FAIL: Cannot find PPFT/urlPost in response")
        raise Exception('Cannot find PPFT/urlPost')

    url_post = url_match.group(1).replace('\\/', '/')
    ppft = ppft_match.group(1)
    log_worker(f"[LOGIN] Step 3: Posting credentials to {url_post[:50]}...")

    data = (
        f'i13=1&login={urllib.parse.quote(email)}&loginfmt={urllib.parse.quote(email)}'
        f'&type=11&LoginOptions=1&passwd={urllib.parse.quote(password)}'
        f'&ps=2&PPFT={urllib.parse.quote(ppft)}&PPSX=PassportR&NewUser=1'
        f'&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&i19=9960'
    )

    resp = s.post(url_post, data=data, allow_redirects=False, timeout=30,
                  headers={'Content-Type': 'application/x-www-form-urlencoded',
                           'Origin': 'https://login.live.com', 'Referer': r.url})
    log_worker(f"[LOGIN] Step 4: Got response, status={resp.status_code}")

    loc = resp.headers.get('Location', '')
    log_worker(f"[LOGIN] Step 5: Location header present: {bool(loc)}")

    # Check for error responses in Location header
    if 'error' in loc.lower():
        log_worker(f"[LOGIN] FAIL: Error in location: {loc[:100]}")
        raise Exception(f'Auth error: {loc[:100]}')

    m = re.search(r'code=([^&]+)', loc)

    if not m:
        body = resp.text.lower()
        log_worker(f"[LOGIN] FAIL: No code in location. Body preview: {body[:200]}")
        if 'incorrect' in body or 'wrong' in body:
            raise Exception('Sai mật khẩu')
        if 'verify' in body or 'identity' in body:
            raise Exception('Cần verify (2FA / identity)')
        if 'blocked' in body or 'suspended' in body:
            raise Exception('Account blocked/suspended')
        raise Exception('Không lấy được auth code')

    code = urllib.parse.unquote(m.group(1))
    log_worker(f"[LOGIN] Step 6: Got code, exchanging for token")

    rt = s.post(
        'https://login.microsoftonline.com/consumers/oauth2/v2.0/token',
        data={'client_info': '1', 'client_id': CLIENT_ID, 'redirect_uri': REDIRECT_URI,
              'grant_type': 'authorization_code', 'code': code, 'scope': SCOPE},
        timeout=30,
    )
    log_worker(f"[LOGIN] Step 7: Token response status: {rt.status_code}")

    if 'access_token' not in rt.text:
        log_worker(f"[LOGIN] FAIL: No access_token in response")
        raise Exception(f'Token fail: {rt.text[:200]}')

    token = rt.json()['access_token']
    log_worker(f"[LOGIN] SUCCESS for {email}")
    return token


def build_rules(email: str, redirect_domain: str, selected: List[str]) -> List[Dict]:
    """Build rule objects for selected rule types"""
    local = email.split('@', 1)[0]
    redirect_to = f'{local}@{redirect_domain}'
    sender = [{'emailAddress': {'name': 'Netflix', 'address': 'info@account.netflix.com'}}]
    redirect = [{'emailAddress': {'name': redirect_to, 'address': redirect_to}}]

    def mk(name: str, seq: int, conditions: dict) -> Dict:
        return {'displayName': name, 'sequence': seq, 'isEnabled': True,
                'conditions': conditions, 'actions': {'redirectTo': redirect}}

    rules_map = {
        'login_en': mk('Login code netflix', 1, {'subjectContains': ['Netflix: Your sign-in code']}),
        'login_vi': mk('Đăng nhập code netflix', 2, {'subjectContains': ['Netflix: Mã đăng nhập của bạn']}),
        'family_en': mk('Login code netflix family/ temporary', 3, {'subjectContains': ['Your Netflix temporary access code', 'family']}),
        'family_vi': mk('Mail hộ gia đình', 4, {'subjectContains': ['Mã truy cập Netflix tạm thời của bạn', 'gia đình', 'tạm thời']}),
        'verify_en': mk('First time verification', 5, {'subjectContains': ['Verification code. Expires in 15 minutes.']}),
        'verify_vi': mk('Xác minh lần đầu', 6, {'subjectContains': ['Mã xác minh. Hết hạn sau 15 phút.']}),
        'all_netflix': mk('netflix all', 7, {'fromAddresses': sender}),
    }
    return [rules_map[k] for k in selected if k in rules_map]


def _headers(token: str) -> dict:
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
            'Accept': 'application/json', 'User-Agent': USER_AGENT}


def _outlook_url(path: str = '') -> str:
    return f'https://outlook.office.com/api/v2.0/me/MailFolders/inbox/messagerules{path}'


def _graph_url(path: str = '') -> str:
    return f'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messageRules{path}'


def create_rule(token: str, rule: Dict, method: str = 'outlook') -> requests.Response:
    if method == 'outlook':
        rest = {'DisplayName': rule['displayName'], 'Sequence': rule['sequence'],
                'IsEnabled': rule['isEnabled'], 'Conditions': {}, 'Actions': {}}
        if 'fromAddresses' in rule['conditions']:
            rest['Conditions']['SenderContains'] = [x['emailAddress']['address'] for x in rule['conditions']['fromAddresses']]
        if 'subjectContains' in rule['conditions']:
            rest['Conditions']['SubjectContains'] = rule['conditions']['subjectContains']
        ak = 'redirectTo' if 'redirectTo' in rule['actions'] else 'forwardTo'
        rest_ak = 'RedirectTo' if ak == 'redirectTo' else 'ForwardTo'
        rest['Actions'][rest_ak] = [
            {'EmailAddress': {'Address': x['emailAddress']['address'], 'Name': x['emailAddress'].get('name', '')}}
            for x in rule['actions'][ak]
        ]
        rest['Actions']['StopProcessingRules'] = rule['actions'].get('stopProcessingRules', False)
        return requests.post(_outlook_url(), headers=_headers(token), json=rest, timeout=30)
    return requests.post(_graph_url(), headers=_headers(token), json=rule, timeout=30)


def list_rules(token: str, method: str = 'outlook') -> List[Dict]:
    url = _outlook_url() if method == 'outlook' else _graph_url()
    r = requests.get(url, headers=_headers(token), timeout=20)
    r.raise_for_status()
    return r.json().get('value', [])


def enable_rule(token: str, rule_id: str, method: str = 'outlook') -> requests.Response:
    if method == 'outlook':
        return requests.patch(_outlook_url(f'/{rule_id}'), headers=_headers(token), json={'IsEnabled': True}, timeout=20)
    return requests.patch(_graph_url(f'/{rule_id}'), headers=_headers(token), json={'isEnabled': True}, timeout=20)


# ==================== NETFLIX RULE DEFS (for check) ====================
NETFLIX_CHECK_DEFS = {
    'Login code netflix':      {'seq': 1, 'cond': {'subjectContains': ['Netflix: Your sign-in code']}},
    'Đăng nhập code netflix': {'seq': 2, 'cond': {'subjectContains': ['Netflix: Mã đăng nhập của bạn']}},
    'Login code netflix family/ temporary': {'seq': 3, 'cond': {'subjectContains': ['Your Netflix temporary access code', 'family']}},
    'Mail hộ gia đình':       {'seq': 4, 'cond': {'subjectContains': ['Mã truy cập Netflix tạm thời của bạn', 'gia đình', 'tạm thời']}},
    'First time verification': {'seq': 5, 'cond': {'subjectContains': ['Verification code. Expires in 15 minutes.']}},
    'Xác minh lần đầu':       {'seq': 6, 'cond': {'subjectContains': ['Mã xác minh. Hết hạn sau 15 phút.']}},
}


# ==================== WORKER FUNCTIONS ====================
def do_check_account(email: str, password: str, domain: str, api: str, auto_enable: bool, auto_create: bool) -> Dict:
    """Check one account - login, list rules, auto-enable, auto-create"""
    log_worker(f"[CHECK] Starting for {email}")
    result = {
        'email': email, 'status': 'PENDING', 'rules_total': 0,
        'rules_enabled': 0, 'rules_disabled': 0, 'fixed': 0, 'created': 0,
        'last_check': datetime.now().strftime('%Y-%m-%d %H:%M'), 'error': ''
    }

    try:
        log_worker(f"[CHECK] Logging in {email}")
        token = login(email, password)
        log_worker(f"[CHECK] Logged in, listing rules for {email}")
        rules = list_rules(token, api)
        result['rules_total'] = len(rules)

        existing = {}
        disabled = []
        for r in rules:
            name = r.get('DisplayName', r.get('displayName', '?'))
            enabled = r.get('IsEnabled', r.get('isEnabled', True))
            existing[name] = r
            if enabled:
                result['rules_enabled'] += 1
            else:
                result['rules_disabled'] += 1
                disabled.append((r.get('Id', r.get('id')), name))

        # Auto-enable disabled rules
        if disabled and auto_enable:
            for rid, name in disabled:
                rp = enable_rule(token, rid, api)
                if rp.status_code < 400:
                    result['fixed'] += 1
                    result['rules_enabled'] += 1
                    result['rules_disabled'] -= 1

        # Auto-create missing Netflix rules
        if auto_create:
            local = email.split('@')[0]
            redirect_to = f'{local}@{domain}'
            redirect = [{'emailAddress': {'name': redirect_to, 'address': redirect_to}}]

            for rule_name, rule_def in NETFLIX_CHECK_DEFS.items():
                if rule_name not in existing:
                    new_rule = {
                        'displayName': rule_name, 'sequence': rule_def['seq'],
                        'isEnabled': True, 'conditions': rule_def['cond'],
                        'actions': {'redirectTo': redirect},
                    }
                    rp = create_rule(token, new_rule, api)
                    if rp.status_code < 400:
                        result['created'] += 1
                        result['rules_enabled'] += 1

        # Determine status
        if result['rules_total'] == 0 and result['created'] == 0:
            result['status'] = 'NO_RULES'
        elif result['rules_disabled'] == 0 and result['created'] == 0:
            result['status'] = 'OK'
        elif result['fixed'] > 0 and result['rules_disabled'] == 0:
            result['status'] = 'FIXED'
        elif result['created'] > 0 and result['rules_disabled'] == 0:
            result['status'] = 'OK'
        else:
            result['status'] = 'PARTIAL'

        AccountStore.upsert(email, password,
            last_check=result['last_check'],
            rules_total=result['rules_total'],
            rules_enabled=result['rules_enabled'],
            status=result['status'])

    except Exception as e:
        result['status'] = 'LOGIN_FAILED'
        result['error'] = str(e)
        log_worker(f"[CHECK] EXCEPTION for {email}: {e}")
        AccountStore.upsert(email, password, last_check=result['last_check'], status='LOGIN_FAILED')

    broadcast_from_thread({'type': 'check_result', 'data': result})
    return result


def do_create_rules(email: str, password: str, rules: List[str], domain: str, api: str) -> Dict:
    """Create rules for one account"""
    log_worker(f"[CREATE] Starting for {email}")
    result = {'email': email, 'status': 'PENDING', 'rules_ok': 0, 'rules_total': 0, 'error': ''}

    try:
        log_worker(f"[CREATE] Logging in {email}")
        token = login(email, password)
        log_worker(f"[CREATE] Logged in, building rules for {email}")
        rule_list = build_rules(email, domain, rules)
        result['rules_total'] = len(rule_list)
        log_worker(f"[CREATE] Creating {len(rule_list)} rules for {email}")

        for rule in rule_list:
            r = create_rule(token, rule, api)
            if r.status_code < 400:
                result['rules_ok'] += 1
                log_worker(f"[CREATE] Rule OK: {rule['displayName']}")
            else:
                result['error'] = f"Rule fail: {r.status_code}"
                log_worker(f"[CREATE] Rule FAIL: {r.status_code} - {r.text[:100]}")

        if result['rules_ok'] == result['rules_total'] and result['rules_total'] > 0:
            result['status'] = 'SUCCESS'
        elif result['rules_ok'] > 0:
            result['status'] = 'PARTIAL'
        else:
            result['status'] = 'FAILED'

        if result['rules_ok'] > 0:
            AccountStore.upsert(email, password,
                last_check=datetime.now().strftime('%Y-%m-%d %H:%M'),
                rules_total=result['rules_ok'], rules_enabled=result['rules_ok'],
                status='OK')

    except Exception as e:
        result['status'] = 'LOGIN_FAILED'
        result['error'] = str(e)
        log_worker(f"[CREATE] EXCEPTION for {email}: {e}")

    broadcast_from_thread({'type': 'create_result', 'data': result})
    return result


# ==================== TELEGRAM BOT ====================
async def start_telegram_bot():
    if not BOT_TOKEN:
        print("⚠️ TELEGRAM_BOT_TOKEN not set")
        return

    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes
    except ImportError:
        print("⚠️ python-telegram-bot not installed")
        return

    async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🎬 *Hotmail Rule Manager*\n\n"
            "/status - System status\n"
            "/accounts - List accounts\n"
            "/check - Check all\n"
            "/add email:password - Add\n"
            "/delete email - Delete\n"
            "/stats - Statistics\n"
            "/help - Commands",
            parse_mode="Markdown")

    async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📋 *Commands*\n\n"
            "/start - Start\n"
            "/status - System status\n"
            "/accounts - List accounts\n"
            "/check - Check all accounts\n"
            "/check email - Check specific\n"
            "/add email:password - Add account\n"
            "/delete email - Delete\n"
            "/stats - Statistics\n"
            "/rules - Available rules",
            parse_mode="Markdown")

    async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        accounts = AccountStore.load()
        ok = sum(1 for a in accounts if a.get('status') == 'OK')
        failed = sum(1 for a in accounts if a.get('status') == 'LOGIN_FAILED')
        last = accounts[0].get('last_check', 'Never') if accounts else 'Never'
        await update.message.reply_text(
            f"📊 *Status*\n\nTotal: {len(accounts)}\nOK: {ok} ✅\nFailed: {failed} ❌\nLast: {last}",
            parse_mode="Markdown")

    async def accounts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        accounts = AccountStore.load()
        if not accounts:
            await update.message.reply_text("No accounts")
            return
        text = "📋 *Accounts*\n\n"
        for a in accounts[:20]:
            icon = "✅" if a.get('status') == 'OK' else "❌" if a.get('status') == 'LOGIN_FAILED' else "⚠️"
            text += f"{icon} {a['email']}\n"
        if len(accounts) > 20:
            text += f"\n...{len(accounts) - 20} more"
        await update.message.reply_text(text, parse_mode="Markdown")

    async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Checking...")
        accounts = AccountStore.load()
        if not accounts:
            await update.message.reply_text("No accounts")
            return

        def run():
            for acc in accounts:
                do_check_account(acc['email'], acc['password'], DEFAULT_DOMAIN, 'outlook', True, True)

        executor.submit(run)

    async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        accounts = AccountStore.load()
        total = len(accounts)
        ok = sum(1 for a in accounts if a.get('status') == 'OK')
        partial = sum(1 for a in accounts if a.get('status') == 'PARTIAL')
        failed = sum(1 for a in accounts if a.get('status') == 'LOGIN_FAILED')
        await update.message.reply_text(
            f"📈 *Stats*\n\nTotal: {total}\nOK: {ok}\nPartial: {partial}\nFailed: {failed}",
            parse_mode="Markdown")

    async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 1:
            await update.message.reply_text("Usage: /add email:password")
            return
        try:
            email, password = ctx.args[0].split(':', 1)
            AccountStore.upsert(email.strip(), password.strip())
            await update.message.reply_text(f"✅ Added: {email}")
            broadcast_from_thread({'type': 'account_added', 'email': email})
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 1:
            await update.message.reply_text("Usage: /delete email")
            return
        email = ctx.args[0]
        AccountStore.remove([email])
        await update.message.reply_text(f"🗑️ Deleted: {email}")
        broadcast_from_thread({'type': 'account_deleted', 'email': email})

    async def rules_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = "📜 *Rules*\n\n"
        for name in NETFLIX_CHECK_DEFS.keys():
            text += f"• {name}\n"
        await update.message.reply_text(text, parse_mode="Markdown")

    try:
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(CommandHandler("help", help_cmd))
        application.add_handler(CommandHandler("status", status_cmd))
        application.add_handler(CommandHandler("accounts", accounts_cmd))
        application.add_handler(CommandHandler("check", check_cmd))
        application.add_handler(CommandHandler("stats", stats_cmd))
        application.add_handler(CommandHandler("add", add_cmd))
        application.add_handler(CommandHandler("delete", delete_cmd))
        application.add_handler(CommandHandler("rules", rules_cmd))

        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("✅ Telegram bot started")
    except Exception as e:
        print(f"❌ Telegram bot error: {e}")


# ==================== API ROUTES ====================
@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/stats")
async def get_stats():
    accounts = AccountStore.load()
    return {
        "total": len(accounts),
        "ok": sum(1 for a in accounts if a.get('status') == 'OK'),
        "partial": sum(1 for a in accounts if a.get('status') in ('PARTIAL', 'FIXED')),
        "failed": sum(1 for a in accounts if a.get('status') == 'LOGIN_FAILED'),
        "last_check": accounts[0].get('last_check', 'Never') if accounts else 'Never'
    }


@app.get("/api/accounts")
async def list_accounts():
    return AccountStore.load()


@app.post("/api/accounts")
async def add_account(account: AccountCreate):
    AccountStore.upsert(account.email, account.password)
    broadcast({'type': 'account_added', 'email': account.email})
    return {"status": "ok", "email": account.email}


@app.post("/api/accounts/batch")
async def batch_add_accounts(batch: AccountBatchCreate):
    added = 0
    for line in batch.accounts:
        line = line.strip()
        if not line:
            continue
        if '|' in line and ':' not in line.split('|', 1)[0]:
            line = line.replace('|', ':', 1)
        if ':' in line:
            email, password = line.split(':', 1)
            AccountStore.upsert(email.strip(), password.strip())
            added += 1
    broadcast({'type': 'accounts_updated'})
    return {"added": added}


@app.delete("/api/accounts/{email}")
async def delete_account(email: str):
    AccountStore.remove([email])
    broadcast({'type': 'account_deleted', 'email': email})
    return {"status": "ok"}


@app.post("/api/check")
async def check_accounts(req: CheckRequest):
    accounts = AccountStore.load()
    if req.emails:
        accounts = [a for a in accounts if a['email'] in req.emails]

    if not accounts:
        raise HTTPException(status_code=400, detail="No accounts to check")

    def run():
        for acc in accounts:
            do_check_account(acc['email'], acc['password'],
                           req.domain, req.api, req.auto_enable, req.auto_create)

    executor.submit(run)
    return {"status": "started", "total": len(accounts)}


@app.post("/api/create")
async def create_rules(req: CreateRulesRequest):
    parsed = []
    for line in req.accounts:
        line = line.strip()
        if not line:
            continue
        if '|' in line and ':' not in line.split('|', 1)[0]:
            line = line.replace('|', ':', 1)
        if ':' in line:
            email, password = line.split(':', 1)
            parsed.append((email.strip(), password.strip()))

    if not parsed:
        raise HTTPException(status_code=400, detail="No valid accounts")

    def run():
        for email, password in parsed:
            do_create_rules(email, password, req.rules, req.domain, req.api)

    executor.submit(run)
    return {"status": "started", "total": len(parsed)}


@app.get("/api/rules")
async def get_rules():
    return [{"name": name, **defs} for name, defs in NETFLIX_CHECK_DEFS.items()]


# ==================== MAIN ====================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)