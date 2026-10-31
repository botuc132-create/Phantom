JINNY CHEAT FREE KEY - RENDER + TELEGRAM GROUP GATE + REFERRAL

1) DEPLOY
- Upload all files in this ZIP to the ROOT of the GitHub repository (replace the old main.py and index.html).
- Render service must run: gunicorn --bind 0.0.0.0:$PORT main:app
- Build command: pip install -r requirements.txt

2) RENDER ENVIRONMENT VARIABLES
BOT_TOKEN = token from @BotFather
WEBAPP_URL = https://YOUR-APP.onrender.com/
WEBHOOK_SECRET = random secret string, 20-50 characters, letters/numbers
ADMIN_IDS = your numeric Telegram user ID; multiple IDs separated by commas
REQUIRED_GROUPS = JSON list used to seed the initial group list, for example:
[{"chat_id":"@your_channel","title":"Announcement channel","url":"https://t.me/your_channel"}]
DATABASE_PATH = /var/data/jinny.db

Replace the example chat_id/title/url with your real channel/group. Add the bot as administrator in every required channel/group so getChatMember can check membership.

3) IMPORTANT: PERSISTENT DATABASE
The bot stores group changes, referral attribution, qualified referrals and claim history in SQLite.
To keep these after a Render deploy/restart, attach a persistent disk to the Web Service:
- Render service > Settings > Disks > Add Disk
- Name: jinny-data
- Mount path: /var/data
- Size: choose an available size
- Keep DATABASE_PATH=/var/data/jinny.db
If your Render plan does not support persistent disks, use a persistent PostgreSQL database and migrate the database layer before relying on referral counts. Without persistent storage, local SQLite data may be lost on redeploy/restart.

4) TELEGRAM BOT FLOW
- /start: records a referral code (if present), then checks required groups.
- Missing groups: shows join buttons and a Verify button.
- After group verification: counts this account as a qualified referral for its inviter (once per Telegram ID).
- Each user receives their own deep link: https://t.me/YOUR_BOT?start=ref_USERID
- Mini App opens only after 3 qualified invited Telegram accounts.
- The Mini App also validates Telegram initData on the server and checks group membership + 3 referrals. Opening the public web URL directly does not unlock the app.
- A referrer receives a Telegram message with the referred user's @username (if available), Telegram ID and progress toward 3.

5) ADMIN BOT COMMANDS
ADMIN_IDS must contain your own numeric Telegram user ID.
- /admin: opens the admin menu.
- Menu > Quản lý nhóm: shows groups and IDs; the trash button deletes a group.
- Add: /addgroup @chat_id | Group title | https://t.me/join_link
- Edit: /editgroup ID | chat_id | Group title | https://t.me/join_link
- Delete: /delgroup ID
The group ID is the numeric ID shown in the management list. For private groups use the -100... chat ID and a valid invite link.

6) MINI APP UI CHANGES
- Removed the key-store slogan, description paragraph, and 24/7/Safety/Fast statistics.
- Header uses Telegram avatar, display name, username and numeric ID when opened inside Telegram.
- The green header button is now claim history, grouped by product.
- Support links point to https://t.me/huybuwin.
- Claim history is stored by Telegram user on the server, as well as locally on the device.

7) LIMITATIONS TO KNOW
- Referral counting prevents counting the same Telegram ID more than once and does not count self-referrals. It cannot guarantee that one human cannot create multiple Telegram accounts; Telegram does not expose a reliable real-person identity check.
- Existing product list and key inventory in this original project are still stored in browser LocalStorage. That means stock/product changes are not yet shared between all visitors/devices. Do not assume this ZIP has migrated key inventory to a server database.
- The history API records claims made by the current Mini App code. Old local-only history remains visible on the same browser/device but is not automatically imported into the server database.

8) TEST
Open https://YOUR-APP.onrender.com/health
Check telegram_bot_configured=true and required_groups has the expected count.
Then message /start, /admin in private chat with the bot.
