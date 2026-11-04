JINNY CHEAT FREE KEY — FLASK + TELEGRAM POLLING + SHARED DATABASE

RENDER SETTINGS
Build Command:
  pip install -r requirements.txt
Start Command:
  python run.py

The run.py supervisor starts Gunicorn (main:app) and exactly one bot.py polling worker. The polling worker processes different users concurrently (six workers), preserves message order for each user, and applies queue back-pressure during bursts. The bot removes any old webhook with deleteWebhook before getUpdates polling. Do not deploy another bot worker/service with the same BOT_TOKEN at the same time.

ENVIRONMENT VARIABLES
BOT_TOKEN       Required. Token from @BotFather.
WEBAPP_URL      Required. Full HTTPS Render URL of this service, e.g. https://your-service.onrender.com/ (set it to your actual URL).
ADMIN_IDS       Required for admin. Numeric Telegram IDs separated by commas, e.g. 123456789,987654321.
REQUIRED_GROUPS Optional JSON array, e.g. [{"chat_id":"@channel","title":"Channel","url":"https://t.me/channel"}]. Existing database groups are retained; this seeds only groups not already present when the DB is initialized.
DATABASE_URL    Recommended. Supabase PostgreSQL URI from Connect -> Direct connection -> Session pooler -> URI. Replace YOUR_PASSWORD locally; URL-encode special characters in the password.
DATABASE_PATH   Optional local SQLite fallback only when DATABASE_URL is not set.
PORT            Supplied by Render automatically.

DATABASE / PERSISTENCE
This version supports Supabase PostgreSQL through DATABASE_URL and creates all required tables/indexes automatically on first startup. No data folder or database file needs to be uploaded. On Render Free, use Supabase PostgreSQL for persistent storage because the local filesystem is ephemeral. Set DATABASE_URL in Render Environment to the full Supabase Session Pooler URI (port 5432), including the real database password. If the password contains special URL characters such as @, :, /, #, or %, percent-encode those characters before inserting it into the URI. Never commit DATABASE_URL or share it publicly. The project keeps SQLite as a local-development fallback only when DATABASE_URL is absent.

TELEGRAM SETUP
1. Add the bot as administrator to each required channel/group with permission to inspect members.
2. Admin mở /admin → “Thêm nhóm/kênh bắt buộc” → gửi trực tiếp link công khai dạng https://t.me/ten_nhom. Bot tự lấy chat ID và tên bằng getChat. Bot phải được thêm làm quản trị viên để xác minh thành viên. Link mời riêng tư dạng t.me/+... không thể tự suy ra chat ID chỉ từ link; hãy dùng username công khai của nhóm/kênh.
3. Set WEBAPP_URL to this service's public HTTPS URL.
4. Ensure the bot is not running in another polling worker/service. bot.py removes the previous webhook automatically.
5. Use /start, join all listed groups and press XÁC MINH. Each new referred account is counted once when they first start the bot. Every successful invite awards 10 points; 40 points can be exchanged for one key. Referral attribution is stored once and cannot be changed later.

ADMIN COMMANDS (ADMIN_IDS ONLY)
/admin
/addgroup @chat_id | Group title | https://t.me/join_link
/editgroup ID | chat_id | Group title | https://t.me/join_link
/delgroup ID
/addlink Button title | https://t.me/luxyffshare
/editlink ID | Button title | https://t.me/luxyffshare
/dellink ID
The admin menu has a separate one-tap “Thêm bot/nhóm bên ngoài” action and optional bot/group link manager. Tap it and send any valid HTTP/HTTPS URL; the bot creates a clickable link automatically. These optional links are never included in required-group verification or referral qualification. Required groups can be deleted from the quick-button list without typing an ID. The menu also includes required-group management, user/referral statistics and bot status. Website admin actions are also checked against signed Telegram initData and ADMIN_IDS; a browser-only password is not treated as authorization.

MINI APP
- On bot startup, the polling worker calls Telegram setChatMenuButton to configure a persistent “Mở Mini App” button beside the message composer in private chats. WEBAPP_URL must be a valid HTTPS Mini App URL.
- After group verification, the referral URL is shown as plain text and a native “SAO CHÉP LINK MỜI” button is provided when supported by Telegram.
The existing visual design is retained. Product catalogue, key stock and claims now use server APIs and the shared database. Claims remove a key atomically on the server, with a maximum of two claims per Telegram account in a rolling 24-hour window (except ADMIN_IDS). Claim history is server-side and grouped by product. Direct access is rejected unless Telegram initData is valid and the user has started the bot. Required-group verification and the old 3-invite gate are disabled. Each key claim costs 40 points, and users may claim at most two keys in a rolling 24-hour window (except ADMIN_IDS).

IMPORTANT MIGRATION NOTES
- PostgreSQL is selected automatically when DATABASE_URL is set. The application initializes its tables and indexes on startup; do not create a local data/ directory or jinny.db file for Supabase.
- Existing data in a previous SQLite jinny.db is not automatically copied to Supabase. Export/import existing products, key stock, groups, users and claims before switching if you need to preserve them. Do not delete the old SQLite file until migration is verified.
- On the first Mini App opening by an ADMIN_IDS account, the page attempts a one-time import of the old `bluekey_store_v2` product catalogue and key inventory from that same browser's LocalStorage into the database. This only works if the administrator opens the app in the same browser profile and same origin/domain where the old LocalStorage exists. If the domain/browser changed or that storage was cleared, re-enter products and keys manually.
- Old `jinny_guest_claims_v1` history is not imported because the old browser-only records do not reliably identify which Telegram account owns each record. New claims are recorded against the authenticated Telegram user in the database.
- Telegram does not provide a reliable way to prove three different real-world humans; the system counts unique Telegram user IDs, as requested.

DEPLOY FROM GITHUB
1. Extract the ZIP and upload/replace the project files in the root of your GitHub repository.
2. In Render, connect/redeploy that repository.
3. Set Build Command and Start Command above.
4. Add the environment variables and persistent disk.
5. Deploy, then open https://YOUR-SERVICE.onrender.com/health. Confirm ok=true, telegram_bot_configured=true, and required_groups matches your configured list.
6. Test /start, referral points, Mini App access, admin operations, product/key creation and key claiming.

ADMIN MINI APP — USER MANAGEMENT
- Admin panel now has a “Người dùng” tab with paginated user list and search by numeric Telegram ID or @username.
- User detail shows Telegram ID, username, stored profile photo when Telegram supplies one, profile link, group/referral state, key-claim history and successful invite history.
- Admin can block/unblock an account and enable/disable the per-user “skip invite requirement” switch. Blocking is checked by both bot messages/callbacks and Mini App API requests. Admin accounts cannot be blocked through this interface.
- Profile photos are populated when a user opens the Mini App; Telegram does not provide photo URLs for every user in every context.

LOAD / ABUSE CONTROLS
- Web API has a short-window per-authenticated-Telegram-user request throttle (with a transient IP fallback for invalid/missing initData); it does not persist visitor IP addresses.
- Bot update processing uses 12 concurrent workers, preserves ordering per Telegram user, and applies a bounded queue (512 pending updates). Gunicorn uses 2 workers × 12 threads to balance concurrency and memory usage.
- For a deployment serving 1,000–2,000 registered accounts, use PostgreSQL (DATABASE_URL), a Render instance with enough CPU/RAM, and only one bot polling worker for the token. This is a configuration target, not a guarantee of 1,000–2,000 simultaneous requests; actual capacity depends on hosting resources, Telegram API limits, database latency and traffic bursts. Do not use SQLite for a multi-worker production deployment.
- Multi-account detection cannot reliably prove distinct real-world people from Telegram IDs alone. IP/device fingerprint collection is intentionally not enabled; rate limits and referral uniqueness reduce automated abuse without storing sensitive tracking data.
