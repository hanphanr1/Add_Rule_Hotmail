# Hotmail Rule Manager - Web + Telegram Bot

A full-featured web application for managing Netflix forwarding rules on Hotmail/Outlook accounts, with Telegram bot integration for remote control and notifications.

## Features

- **Web Interface**: Responsive dashboard with real-time status updates
- **Telegram Bot**: Control and monitor from anywhere
- **Auto Management**: Auto re-enable disabled rules, auto create missing rules
- **Multi-threaded**: Process multiple accounts simultaneously
- **24/7 Running**: Deploy on Railway for always-on availability

## Quick Start (Local)

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py

# Open browser
# http://localhost:8000
```

## Deploy on Railway

### 1. Connect Repository
- Push this code to GitHub
- Go to [Railway](https://railway.app)
- Connect your GitHub repository

### 2. Configure Environment Variables
In Railway dashboard, add these variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `PORT` | Yes | `8000` |
| `TELEGRAM_BOT_TOKEN` | No | Your Telegram bot token from @BotFather |
| `TELEGRAM_ADMIN_ID` | No | Your Telegram chat ID for admin notifications |

### 3. Deploy
- Railway auto-detects Python and deploys
- App runs on port 8000

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show start message |
| `/help` | Show all commands |
| `/status` | System status |
| `/accounts` | List all accounts |
| `/check` | Check all accounts |
| `/check email` | Check specific account |
| `/add email:password` | Add account |
| `/delete email` | Delete account |
| `/stats` | Show statistics |
| `/rules` | Show available rules |
| `/create email:pass rule1,rule2` | Create rules |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/stats` | Get statistics |
| `GET` | `/api/accounts` | List all accounts |
| `POST` | `/api/accounts` | Add single account |
| `POST` | `/api/accounts/batch` | Batch import accounts |
| `DELETE` | `/api/accounts/{email}` | Delete account |
| `POST` | `/api/check` | Check accounts |
| `POST` | `/api/create` | Create rules |
| `GET` | `/api/rules` | Get available rules |

## Web Interface

- **Create Rules Tab**: Paste accounts and select rules to create
- **Manage Tab**: View all accounts, check status, auto-manage
- **Real-time Updates**: WebSocket for live log and status
- **Responsive Design**: Works on desktop and mobile

## Netflix Rules

1. Login code (EN) - "Netflix: Your sign-in code"
2. Login code (VI) - "Netflix: Mã đăng nhập của bạn"
3. Family / Temp (EN) - "Your Netflix temporary access code"
4. Hộ gia đình (VI) - "Mã truy cập Netflix tạm thời"
5. Verification (EN) - "Verification code. Expires in 15 minutes."
6. Xác minh (VI) - "Mã xác minh. Hết hạn sau 15 phút."
7. All Netflix (optional) - All emails from @account.netflix.com

## Files

```
├── main.py         # FastAPI backend + Telegram bot
├── engine.py      # Rule engine core logic
├── index.html     # Web frontend
├── requirements.txt
├── Dockerfile
├── railway.json
└── README.md
```

## Configuration

Default redirect domain: `tm.cameyou.shop`
- Rule: `abc@hotmail.com` → `netflix-abc@tm.cameyou.shop`

To change, edit in web UI or pass `domain` in API request.