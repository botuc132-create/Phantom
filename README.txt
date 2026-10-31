JINNY CHEAT FREE KEY — FLASK + TELEGRAM POLLING + SHARED DATABASE

RENDER SETTINGS
Build Command:
  pip install -r requirements.txt
Start Command:
  python run.py

The run.py supervisor starts Gunicorn (main:app) and exactly one bot.py polling worker. The bot removes any old webhook with deleteWebhook before getUpdates polling. Do not deploy another bot worker/service with the same BOT_TOKEN at the same time.

ENVIRONMENT VARIABLES
BOT_TOKEN       Required. Token from @BotFather.
WEBAPP_URL      Required. Full HTTPS Render URL of this service, e.g. https://your-service.onrender.com/ (set it to your actual URL).
ADMIN_IDS       Required for admin. Numeric Telegram IDs separated by commas, e.g. 123456789,987654321.
REQUIRED_GROUPS Optional JSON array, e.g. [{"chat_id":"@channel","title":"Channel","url":"https://t.me/channel"}]. Existing database groups are retained; this seeds only groups not already present when the DB is initialized.
DATABASE_PATH   Recommended: /var/data/jinny.db
PORT            Supplied by Render automatically.

PERSISTENCE
Attach a Render Persistent Disk to this Web Service at /var/data and set DATABASE_PATH=/var/data/jinny.db. The database stores groups, users, referral attribution and qualification, product catalogue, key stock and key claim history. If using PostgreSQL instead, this ZIP does not include a PostgreSQL adapter; do not set DATABASE_PATH to a PostgreSQL URL.

TELEGRAM SETUP
1. Add the bot as administrator to each required channel/group with permission to inspect members.
2. Configure each required group with /addgroup @chat_id | Group title | https://t.me/join_link. Private groups should use their numeric -100... chat ID and a valid invite URL.
3. Set WEBAPP_URL to this service's public HTTPS URL.
4. Ensure the bot is not running in another polling worker/service. bot.py removes the previous webhook automatically.
5. Use /start, join all listed groups and press XÁC MINH. Each referred account is counted only after successful group verification. Referral attribution is stored once and cannot be changed later.

ADMIN COMMANDS (ADMIN_IDS ONLY)
/admin
/addgroup @chat_id | Group title | https://t.me/join_link
/editgroup ID | chat_id | Group title | https://t.me/join_link
/delgroup ID
The admin menu includes group management, user/referral statistics and bot status. Website admin actions are also checked against signed Telegram initData and ADMIN_IDS; a browser-only password is not treated as authorization.

MINI APP
The existing visual design is retained. Product catalogue, key stock and claims now use server APIs and the shared database. Claims remove a key atomically on the server, with a maximum of two claims per Telegram account per UTC day (except ADMIN_IDS). Claim history is server-side and grouped by product. Direct access is rejected unless Telegram initData is valid and the user has joined required groups and qualified 3 invited users.

IMPORTANT MIGRATION NOTES
- On the first Mini App opening by an ADMIN_IDS account, the page attempts a one-time import of the old `bluekey_store_v2` product catalogue and key inventory from that same browser's LocalStorage into the database. This only works if the administrator opens the app in the same browser profile and same origin/domain where the old LocalStorage exists. If the domain/browser changed or that storage was cleared, re-enter products and keys manually.
- Old `jinny_guest_claims_v1` history is not imported because the old browser-only records do not reliably identify which Telegram account owns each record. New claims are recorded against the authenticated Telegram user in the database.
- Telegram does not provide a reliable way to prove three different real-world humans; the system counts unique Telegram user IDs, as requested.
- This package uses SQLite. A Render Persistent Disk is required for durable SQLite data. PostgreSQL is not supported by this source package without a database adapter migration.

DEPLOY FROM GITHUB
1. Extract the ZIP and upload/replace the project files in the root of your GitHub repository.
2. In Render, connect/redeploy that repository.
3. Set Build Command and Start Command above.
4. Add the environment variables and persistent disk.
5. Deploy, then open https://YOUR-SERVICE.onrender.com/health. Confirm ok=true, telegram_bot_configured=true, and required_groups matches your configured list.
6. Test /start, group verification, referral completion, Mini App access, admin operations, product/key creation and key claiming.
