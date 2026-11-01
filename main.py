import os, json, sqlite3, time, hmac, hashlib, threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from flask import Flask, jsonify, request, send_from_directory
import urllib.request, urllib.parse, re

# Bounded in-process caches and pending admin input; database schema is intentionally unchanged.
MEMBER_CACHE = {}
MEMBER_CACHE_LOCK = threading.RLock()
PENDING_ADMIN = {}
PENDING_ADMIN_LOCK = threading.RLock()
TELEGRAM_TIMEOUT = 12

def tg(method, params=None, timeout=TELEGRAM_TIMEOUT):
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN chưa được cấu hình')
    # Telegram expects nested Bot API fields (for example reply_markup) as JSON,
    # not Python's dict/list string representation. Keep scalar parameters unchanged.
    encoded_params = {
        key: (json.dumps(value, ensure_ascii=False, separators=(',', ':'))
              if isinstance(value, (dict, list)) else value)
        for key, value in (params or {}).items()
    }
    payload = urllib.parse.urlencode(encoded_params).encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',
        data=payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode('utf-8'))
    if not result.get('ok'):
        raise RuntimeError(result.get('description', 'Telegram API error'))
    return result.get('result')

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
WEBAPP_URL = os.getenv('WEBAPP_URL', '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
DB_PATH = os.getenv('DATABASE_PATH', str(BASE_DIR / 'jinny.db'))
DB_BACKEND = 'postgres' if DATABASE_URL else 'sqlite'

class CompatRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            try: return list(self.values())[key]
            except IndexError: raise KeyError(key)
        return super().__getitem__(key)

class CompatCursor:
    def __init__(self, cursor): self._cursor = cursor; self.rowcount = cursor.rowcount
    def _row(self, row): return CompatRow(row) if row is not None and isinstance(row, dict) else row
    def fetchone(self): return self._row(self._cursor.fetchone())
    def fetchall(self): return [self._row(x) for x in self._cursor.fetchall()]
    def __iter__(self): return iter(self.fetchall())

class PostgresCompat:
    def __init__(self, conn): self.conn = conn
    def execute(self, sql, params=()):
        import re
        sql=sql.strip()
        if sql.upper().startswith('PRAGMA '): return CompatCursor(self.conn.execute('SELECT 1'))
        ignored = bool(re.search(r'INSERT\s+OR\s+IGNORE\s+INTO', sql, re.I))
        sql=re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=re.I)
        if ignored and 'ON CONFLICT' not in sql.upper(): sql += ' ON CONFLICT DO NOTHING'
        sql=sql.replace('?', '%s')
        return CompatCursor(self.conn.execute(sql, params or ()))
    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip(): self.execute(statement)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

@contextmanager
def db():
    if DB_BACKEND == 'postgres':
        import psycopg
        from psycopg.rows import dict_row
        con = PostgresCompat(psycopg.connect(DATABASE_URL, connect_timeout=12, sslmode='require', row_factory=dict_row, prepare_threshold=None))
    else:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=30000')
        con.execute('PRAGMA synchronous=NORMAL')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        if DB_BACKEND == 'sqlite': con.execute('PRAGMA journal_mode=WAL')
        if DB_BACKEND == 'postgres':
            # Serialize schema init across Gunicorn workers and bot.py (they start concurrently);
            # concurrent CREATE TABLE IF NOT EXISTS with BIGSERIAL races on the *_id_seq name.
            # Transaction-level lock, released automatically on commit/rollback.
            con.execute('SELECT pg_advisory_xact_lock(%s)' % 724100001)
            schema = """
            CREATE TABLE IF NOT EXISTS groups_cfg(id BIGSERIAL PRIMARY KEY,chat_id TEXT UNIQUE NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS optional_links(id BIGSERIAL PRIMARY KEY,title TEXT NOT NULL,url TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(user_id BIGINT PRIMARY KEY,username TEXT,first_name TEXT,referrer_id BIGINT,qualified INTEGER NOT NULL DEFAULT 0,groups_verified INTEGER NOT NULL DEFAULT 0,unlocked INTEGER NOT NULL DEFAULT 0,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS referrals(referrer_id BIGINT NOT NULL,referred_id BIGINT UNIQUE NOT NULL,qualified_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,name TEXT NOT NULL,price TEXT NOT NULL,duration TEXT,description TEXT,image TEXT,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS keys_stock(id BIGSERIAL PRIMARY KEY,product_id TEXT NOT NULL,key_value TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS claims(id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,product_id TEXT,product_name TEXT NOT NULL,key_value TEXT NOT NULL,claimed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS migration_flags(flag TEXT PRIMARY KEY,completed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS admin_settings(setting TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '0');
            CREATE TABLE IF NOT EXISTS blocked_users(user_id BIGINT PRIMARY KEY,blocked_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_claim_user ON claims(user_id,id DESC);
            CREATE INDEX IF NOT EXISTS idx_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_stock_product ON keys_stock(product_id,id);
            CREATE INDEX IF NOT EXISTS idx_products_active ON products(active);
            """
            con.executescript(schema)
            con.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS groups_verified INTEGER NOT NULL DEFAULT 0')
            con.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS unlocked INTEGER NOT NULL DEFAULT 0')
            con.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP')
        else:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS groups_cfg(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT UNIQUE NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS optional_links(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,url TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,referrer_id INTEGER,qualified INTEGER NOT NULL DEFAULT 0,groups_verified INTEGER NOT NULL DEFAULT 0,unlocked INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS referrals(referrer_id INTEGER NOT NULL,referred_id INTEGER UNIQUE NOT NULL,qualified_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,name TEXT NOT NULL,price TEXT NOT NULL,duration TEXT,description TEXT,image TEXT,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS keys_stock(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id TEXT NOT NULL,key_value TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,product_id TEXT,product_name TEXT NOT NULL,key_value TEXT NOT NULL,claimed_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS migration_flags(flag TEXT PRIMARY KEY,completed_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS admin_settings(setting TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '0');
            CREATE TABLE IF NOT EXISTS blocked_users(user_id INTEGER PRIMARY KEY,blocked_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_claim_user ON claims(user_id,id DESC);
            CREATE INDEX IF NOT EXISTS idx_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_stock_product ON keys_stock(product_id,id);
            CREATE INDEX IF NOT EXISTS idx_products_active ON products(active);
            """)
            user_cols={r['name'] for r in con.execute('PRAGMA table_info(users)')}
            for name, declaration in [('groups_verified','INTEGER NOT NULL DEFAULT 0'),('unlocked','INTEGER NOT NULL DEFAULT 0'),('created_at',"TEXT DEFAULT CURRENT_TIMESTAMP")]:
                if name not in user_cols: con.execute(f'ALTER TABLE users ADD COLUMN {name} {declaration}')
        try: initial=json.loads(os.getenv('REQUIRED_GROUPS','[]') or '[]')
        except (TypeError,ValueError): initial=[]
        if isinstance(initial,list):
            for g in initial:
                if isinstance(g,dict) and g.get('chat_id') and g.get('url'):
                    con.execute('INSERT OR IGNORE INTO groups_cfg(chat_id,title,url) VALUES(?,?,?)',(str(g['chat_id']),str(g.get('title') or g['chat_id']),str(g['url'])))
        if con.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
            for p in [
                {'id':'basic','name':'Key Basic','price':'Miễn phí','duration':'','description':'Key hoàn toàn miễn phí 🔥'},
                {'id':'pro','name':'Key Pro','price':'Miễn phí','duration':'','description':'Key hoàn toàn miễn phí 🔥'},
                {'id':'vip','name':'Key VIP','price':'Miễn phí','duration':'','description':'Key hoàn toàn miễn phí 🔥'}]:
                con.execute('INSERT OR IGNORE INTO products(id,name,price,duration,description) VALUES(?,?,?,?,?)',(p['id'],p['name'],p['price'],p['duration'],p['description']))

init_db()

def groups():
    with db() as con: return [dict(x) for x in con.execute('SELECT id,chat_id,title,url FROM groups_cfg ORDER BY id')]

def optional_links():
    with db() as con: return [dict(x) for x in con.execute('SELECT id,title,url FROM optional_links ORDER BY id')]

def maintenance_enabled():
    with db() as con:
        row=con.execute("SELECT value FROM admin_settings WHERE setting='maintenance'").fetchone()
    return bool(row and str(row['value'])=='1')

def is_blocked(uid):
    with db() as con:
        return bool(con.execute('SELECT 1 FROM blocked_users WHERE user_id=?',(int(uid),)).fetchone())

def broadcast_worker(text, admin_chat):
    with db() as con: ids=[int(x['user_id']) for x in con.execute('SELECT user_id FROM users ORDER BY user_id').fetchall()]
    sent=failed=0
    for uid in ids:
        delivered=False
        for attempt in range(4):
            try:
                tg('sendMessage',{'chat_id':uid,'text':text},timeout=8); delivered=True; break
            except Exception as exc:
                # Telegram may briefly rate-limit broadcasts; honor its retry_after hint.
                match=re.search(r'retry after\s+(\d+)',str(exc),re.I)
                if match and attempt<3: time.sleep(min(int(match.group(1)),30)); continue
                break
        if delivered: sent+=1
        else: failed+=1
        time.sleep(0.05)  # Keep below Telegram's private-chat broadcast rate.
    try: admin_text(admin_chat,f'📣 Hoàn tất thông báo.\n✅ Gửi thành công: {sent}\n⚠️ Không gửi được: {failed}\n👥 Tổng: {len(ids)}')
    except Exception: pass

def valid_public_url(url):
    from urllib.parse import urlparse
    try:
        parsed=urlparse(url.strip())
        return parsed.scheme in ('https','http') and bool(parsed.netloc)
    except Exception:
        return False

def send_optional_links(chat_id):
    links=optional_links()
    if not links: return
    rows=[[{'text':'🔗 '+x['title'],'url':x['url']}] for x in links]
    tg('sendMessage',{'chat_id':chat_id,'text':'🔗 LIÊN KẾT BOT / NHÓM KHÁC (KHÔNG BẮT BUỘC)\nBạn có thể nhấn để mở liên kết. Các liên kết này không ảnh hưởng đến xác minh nhóm bắt buộc.','reply_markup':{'inline_keyboard':rows}})


def upsert_user(user, referrer_id=None):
    uid=int(user['id'])
    with db() as con:
        old=con.execute('SELECT referrer_id FROM users WHERE user_id=?',(uid,)).fetchone()
        if old:
            con.execute('UPDATE users SET username=?,first_name=? WHERE user_id=?',(user.get('username'),user.get('first_name',''),uid))
        else:
            if referrer_id == uid: referrer_id=None
            if referrer_id:
                inviter=con.execute('SELECT user_id FROM users WHERE user_id=?',(int(referrer_id),)).fetchone()
                if not inviter: referrer_id=None
            con.execute('INSERT INTO users(user_id,username,first_name,referrer_id) VALUES(?,?,?,?)',(uid,user.get('username'),user.get('first_name',''),referrer_id))


def validate_webapp_init_data(raw):
    if not BOT_TOKEN or not raw: return None
    try:
        pairs=dict(parse_qsl(raw,keep_blank_values=True)); received=pairs.pop('hash',''); auth=int(pairs.get('auth_date','0'))
        if not received or time.time()-auth>86400 or auth>time.time()+60: return None
        check='\n'.join(f'{k}={v}' for k,v in sorted(pairs.items()))
        secret=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest()
        expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,received): return None
        return json.loads(pairs.get('user','{}'))
    except Exception: return None


def referral_count(uid):
    with db() as con: return con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',(uid,)).fetchone()[0]


def bot_username():
    try: return tg('getMe').get('username','')
    except Exception: return ''


def referral_link(uid):
    name=bot_username()
    return f'https://t.me/{name}?start=ref_{uid}' if name else ''


def is_member(uid,g):
    m=tg('getChatMember',{'chat_id':g['chat_id'],'user_id':uid})
    return m.get('status') in ('creator','administrator','member') or (m.get('status')=='restricted' and m.get('is_member') is True)


def missing_groups(uid):
    cfg=groups(); signature=tuple((g['id'],g['chat_id']) for g in cfg); now=time.time()
    with MEMBER_CACHE_LOCK:
        cached=MEMBER_CACHE.get((int(uid),signature))
        if cached and now-cached[0] < 25:
            missing_ids=set(cached[1]); return [g for g in cfg if g['id'] in missing_ids]
    result=[]
    for g in cfg:
        try:
            if not is_member(uid,g): result.append(g)
        except Exception: result.append(g)
    with MEMBER_CACHE_LOCK:
        MEMBER_CACHE[(int(uid),signature)]=(now,tuple(g['id'] for g in result))
        # Bound memory when many unique users visit concurrently.
        if len(MEMBER_CACHE)>5000:
            for key in list(MEMBER_CACHE)[:1500]: MEMBER_CACHE.pop(key,None)
    return result


def qualify_referral(uid):
    inviter=None; inserted=False
    with db() as con:
        u=con.execute('SELECT referrer_id,qualified FROM users WHERE user_id=?',(uid,)).fetchone()
        if not u or u['qualified']: return
        inviter=u['referrer_id']
        con.execute('UPDATE users SET qualified=1,groups_verified=1 WHERE user_id=?',(uid,))
        if inviter and inviter != uid:
            inserted=con.execute('INSERT OR IGNORE INTO referrals(referrer_id,referred_id) VALUES(?,?)',(inviter,uid)).rowcount == 1
        if inviter and con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',(inviter,)).fetchone()[0]>=3:
            con.execute('UPDATE users SET unlocked=1 WHERE user_id=?',(inviter,))
    if inserted:
        try:
            with db() as con: who=con.execute('SELECT username,first_name FROM users WHERE user_id=?',(uid,)).fetchone()
            label='@'+who['username'] if who and who['username'] else (who['first_name'] if who else 'Người dùng')
            count=referral_count(inviter)
            tg('sendMessage',{'chat_id':inviter,'text':f'🎉 Có người được tính vào link giới thiệu của bạn!\n👤 {label}\n🆔 ID: {uid}\n📊 Tiến độ: {count}/3 người.\n'+('🎁 Bạn đã mở khóa Mini App!' if count>=3 else f'⏳ Còn thiếu {3-count} người nữa.')})
            if count>=3: send_app_button(inviter)
        except Exception: app.logger.exception('Referral notification failed')


def group_keyboard():
    rows=[[{'text':'📢 '+g['title'],'url':g['url']}] for g in groups()]
    rows.append([{'text':'✅ XÁC MINH','callback_data':'verify'}])
    return {'inline_keyboard':rows}


def send_prompt(chat_id,message_id=None):
    p={'chat_id':chat_id,'text':'👋 Để tiếp tục, hãy tham gia tất cả kênh/nhóm bắt buộc bên dưới, sau đó nhấn XÁC MINH.','reply_markup':group_keyboard()}
    if message_id:
        p['message_id']=message_id
        try: tg('editMessageText',p); return
        except Exception: p.pop('message_id',None)
    tg('sendMessage',p)


def send_app_button(chat_id):
    if not WEBAPP_URL:
        tg('sendMessage',{'chat_id':chat_id,'text':'Đã đủ điều kiện nhưng chủ bot chưa cấu hình WEBAPP_URL.'}); return
    tg('sendMessage',{'chat_id':chat_id,'text':'🎉 Bạn đã mở khóa Mini App!','reply_markup':{'inline_keyboard':[[{'text':'🚀 MỞ MINI APP','web_app':{'url':WEBAPP_URL}}]]}})


def send_referral_gate(chat_id,uid):
    count=referral_count(uid)
    if count>=3: send_app_button(chat_id); return
    link=referral_link(uid)
    tg('sendMessage',{'chat_id':chat_id,'text':f'✅ Bạn đã xác minh tham gia nhóm thành công!\n\nĐể mở khóa Mini App, bạn cần mời 3 người bạn mới tham gia bot.\n\n🔗 Link giới thiệu của bạn: {link or "Không lấy được link bot, hãy kiểm tra BOT_TOKEN."}\n📊 Tiến độ: {count}/3 người.\n⏳ Còn thiếu: {3-count} người.'})


def add_required_group_from_link(url):
    """Resolve a public t.me username to a Telegram chat without changing DB schema."""
    from urllib.parse import urlparse, unquote
    if not valid_public_url(url):
        raise ValueError('Link không hợp lệ. Hãy gửi link https://t.me/...')
    parsed=urlparse(url.strip())
    host=(parsed.netloc or '').lower().split(':')[0]
    if host not in ('t.me','www.t.me','telegram.me','www.telegram.me'):
        raise ValueError('Hãy gửi link Telegram dạng https://t.me/ten_nhom.')
    path=unquote(parsed.path.strip('/'))
    if not path or path.startswith('+') or path.startswith('joinchat/'):
        raise ValueError('Link mời riêng tư không cung cấp chat ID để bot kiểm tra thành viên. Hãy dùng link công khai dạng https://t.me/ten_nhom.')
    username=path.split('/')[0]
    if username in ('c', 's') or not re.fullmatch(r'[A-Za-z0-9_]{5,}', username):
        raise ValueError('Link này không phải link nhóm/kênh công khai có username.')
    chat=tg('getChat', {'chat_id':'@'+username})
    chat_id=str(chat.get('id'))
    title=chat.get('title') or chat.get('username') or username
    with db() as con:
        con.execute('INSERT INTO groups_cfg(chat_id,title,url) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,url=excluded.url',
                    (chat_id,title,url.strip()))
    return title, chat_id

def admin_menu(chat_id):
    tg('sendMessage',{'chat_id':chat_id,'text':'🛠 MENU QUẢN TRỊ','reply_markup':{'inline_keyboard':[
        [{'text':'📋 Quản lý nhóm/kênh','callback_data':'adm_groups'}],
        [{'text':'➕ Thêm nhóm/kênh','callback_data':'adm_add'}],
        [{'text':'🔗 Quản lý link bot/nhóm khác (không bắt buộc)','callback_data':'adm_links'}],
        [{'text':'➕ Thêm bot/nhóm bên ngoài','callback_data':'adm_link_add'}],
        [{'text':'✏️ Sửa nhóm/kênh','callback_data':'adm_edit'}],
        [{'text':'🗑 Xóa nhóm/kênh','callback_data':'adm_del'}],
        [{'text':'📊 Thống kê người dùng','callback_data':'adm_users'}],
        [{'text':'🎁 Thống kê giới thiệu','callback_data':'adm_refs'}],
        [{'text':'📣 Thông báo tất cả người dùng','callback_data':'adm_broadcast'}],
        [{'text':'🔒 Khóa tài khoản','callback_data':'adm_lock'}],
        [{'text':'👥 Danh sách người dùng','callback_data':'adm_user_list'}],
        [{'text':'🛠 Bảo trì bot','callback_data':'adm_maintenance'}],
        [{'text':'🤖 Trạng thái bot','callback_data':'adm_status'}]]}})


def admin_text(chat_id,text): tg('sendMessage',{'chat_id':chat_id,'text':text})


def admin_command(message):
    uid=int(message['from']['id']); chat=message['chat']['id']; parts=message.get('text','').split(maxsplit=1); cmd=parts[0].split('@')[0].lower(); arg=parts[1].strip() if len(parts)>1 else ''
    if uid not in ADMIN_IDS: return False
    if cmd=='/admin': admin_menu(chat); return True
    if cmd=='/addgroup':
        f=[x.strip() for x in arg.split('|')]
        if len(f)!=3 or not all(f): admin_text(chat,'Cú pháp: /addgroup @chat_id | Tên nhóm | https://t.me/link'); return True
        try:
            with db() as con: con.execute('INSERT INTO groups_cfg(chat_id,title,url) VALUES(?,?,?)',f)
            admin_text(chat,'✅ Đã thêm nhóm/kênh.')
        except Exception: admin_text(chat,'Không thể thêm nhóm (có thể chat_id đã tồn tại).')
        return True
    if cmd=='/editgroup':
        f=[x.strip() for x in arg.split('|')]
        if len(f)!=4: admin_text(chat,'Cú pháp: /editgroup ID | chat_id | Tên nhóm | link'); return True
        try:
            with db() as con: cur=con.execute('UPDATE groups_cfg SET chat_id=?,title=?,url=? WHERE id=?',(f[1],f[2],f[3],int(f[0])))
            admin_text(chat,'✅ Đã sửa.' if cur.rowcount else 'Không tìm thấy ID.')
        except Exception: admin_text(chat,'Dữ liệu không hợp lệ.')
        return True
    if cmd=='/delgroup':
        try:
            with db() as con: cur=con.execute('DELETE FROM groups_cfg WHERE id=?',(int(arg),))
            admin_text(chat,'🗑 Đã xóa.' if cur.rowcount else 'Không tìm thấy ID.')
        except Exception: admin_text(chat,'Cú pháp: /delgroup ID')
        return True
    if cmd=='/addlink':
        f=[x.strip() for x in arg.split('|',1)]
        if len(f)==1 and valid_public_url(f[0]):
            title=f[0].rstrip('/').split('/')[-1] or 'Mở liên kết'
            url=f[0]
        elif len(f)==2 and f[0] and valid_public_url(f[1]): title,url=f[0],f[1]
        else:
            admin_text(chat,'Hãy gửi trực tiếp một link https://t.me/... hoặc dùng /addlink Tên | URL.')
            return True
        with db() as con: con.execute('INSERT INTO optional_links(title,url) VALUES(?,?)',(title[:60],url))
        admin_text(chat,'✅ Đã thêm link. Link này là tùy chọn, không ảnh hưởng xác minh nhóm bắt buộc.')
        return True
    if cmd=='/editlink':
        f=[x.strip() for x in arg.split('|',2)]
        if len(f)!=3 or not f[1] or not valid_public_url(f[2]):
            admin_text(chat,'Cú pháp: /editlink ID | Tên nút | https://t.me/luxyffshare')
            return True
        try:
            with db() as con: cur=con.execute('UPDATE optional_links SET title=?,url=? WHERE id=?',(f[1][:60],f[2],int(f[0])))
            admin_text(chat,'✅ Đã cập nhật liên kết.' if cur.rowcount else 'Không tìm thấy ID liên kết.')
        except Exception: admin_text(chat,'Dữ liệu không hợp lệ.')
        return True
    if cmd=='/dellink':
        try:
            with db() as con: cur=con.execute('DELETE FROM optional_links WHERE id=?',(int(arg),))
            admin_text(chat,'🗑 Đã xóa liên kết.' if cur.rowcount else 'Không tìm thấy ID liên kết.')
        except Exception: admin_text(chat,'Cú pháp: /dellink ID')
        return True
    return False


def process_update(update):
    m=update.get('message')
    if m and m.get('chat',{}).get('type')=='private':
        user=m.get('from',{}); uid=int(user['id']); text=m.get('text','').strip()
        if uid not in ADMIN_IDS and maintenance_enabled():
            admin_text(m['chat']['id'],'🛠 Bot đang bảo trì, hãy quay lại sau nhé.'); return
        if uid not in ADMIN_IDS and is_blocked(uid):
            admin_text(m['chat']['id'],'🔒 Tài khoản của bạn đã bị khóa.'); return
        if uid in ADMIN_IDS and uid in PENDING_ADMIN and text and not text.startswith('/'):
            with PENDING_ADMIN_LOCK: mode=PENDING_ADMIN.pop(uid,None)
            if not mode: return
            if mode=='add_required_group':
                try:
                    title, chat_id=add_required_group_from_link(text)
                    admin_text(m['chat']['id'],f'✅ Đã thêm nhóm/kênh bắt buộc: {title}\n🆔 Chat ID: {chat_id}\nBot cần có quyền quản trị để xác minh thành viên.')
                except Exception as exc:
                    admin_text(m['chat']['id'],f'❌ Không thêm được nhóm/kênh: {exc}\nHãy gửi lại link công khai dạng https://t.me/ten_nhom.')
                    with PENDING_ADMIN_LOCK: PENDING_ADMIN[uid]=mode
                return
            if mode=='add_optional_link':
                if not valid_public_url(text):
                    admin_text(m['chat']['id'],'❌ Link không hợp lệ. Hãy gửi link bắt đầu bằng https:// hoặc http://.'); PENDING_ADMIN[uid]=mode; return
                title=text.rstrip('/').split('/')[-1] or 'Mở liên kết'
                with db() as con: con.execute('INSERT INTO optional_links(title,url) VALUES(?,?)',(title[:60],text))
                admin_text(m['chat']['id'],'✅ Đã thêm link thành công!'); return
            if mode=='broadcast':
                if len(text)>4000:
                    admin_text(m['chat']['id'],'❌ Nội dung quá dài. Vui lòng gửi tối đa 4000 ký tự.'); return
                admin_text(m['chat']['id'],'📣 Đã nhận nội dung. Bắt đầu gửi thông báo nền, bot vẫn tiếp tục nhận lệnh.')
                threading.Thread(target=broadcast_worker,args=(text,m['chat']['id']),daemon=True).start(); return
            if mode=='lock_user':
                if not text.isdigit():
                    admin_text(m['chat']['id'],'❌ ID không hợp lệ. Hãy gửi ID dạng số.'); return
                target=int(text)
                if target in ADMIN_IDS:
                    admin_text(m['chat']['id'],'❌ Không thể khóa tài khoản quản trị viên.'); return
                with db() as con: con.execute('INSERT INTO blocked_users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING',(target,))
                admin_text(m['chat']['id'],f'🔒 Đã khóa tài khoản ID {target}.'); return
        if text.startswith('/') and admin_command(m): return
        if text.startswith('/') and text.split()[0].split('@')[0]=='/start':
            args=text.split(maxsplit=1); token=args[1] if len(args)>1 else ''; ref=int(token[4:]) if token.startswith('ref_') and token[4:].isdigit() else None
            upsert_user(user,ref)
            send_optional_links(m['chat']['id'])
            if uid in ADMIN_IDS: send_app_button(m['chat']['id']); return
            if not groups(): admin_text(m['chat']['id'],'⚠️ Chủ bot chưa cấu hình nhóm bắt buộc.'); return
            miss=missing_groups(uid)
            if miss: send_prompt(m['chat']['id']); return
            qualify_referral(uid); send_referral_gate(m['chat']['id'],uid); return
    c=update.get('callback_query')
    if not c: return
    data=c.get('data',''); user=c.get('from',{}); uid=int(user.get('id',0)); msg=c.get('message',{}); chat=msg.get('chat',{}).get('id')
    if uid not in ADMIN_IDS and maintenance_enabled():
        tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Bot đang bảo trì, hãy quay lại sau nhé.','show_alert':True}); return
    if uid not in ADMIN_IDS and is_blocked(uid):
        tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Tài khoản của bạn đã bị khóa.','show_alert':True}); return
    if data=='verify':
        upsert_user(user)
        if uid in ADMIN_IDS: tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Admin được cấp quyền.'}); send_app_button(chat); return
        miss=missing_groups(uid)
        if miss:
            tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Còn thiếu: '+', '.join(g['title'] for g in miss),'show_alert':True}); send_prompt(chat,msg.get('message_id')); return
        qualify_referral(uid); tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Đã xác minh nhóm!'}); send_referral_gate(chat,uid); return
    if uid not in ADMIN_IDS:
        tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Bạn không có quyền.' ,'show_alert':True}); return
    if data=='adm_links':
        links=optional_links(); rows=[]
        for item in links:
            rows.append([{'text':f"🔗 {item['title']} (ID {item['id']})",'callback_data':f"adm_link_{item['id']}"}])
        rows.append([{'text':'➕ Thêm link bot/nhóm khác','callback_data':'adm_link_add'}])
        admin_text(chat,'🔗 QUẢN LÝ LINK BOT / NHÓM KHÁC\nĐây là các link tùy chọn, KHÔNG thuộc danh sách nhóm bắt buộc và không ảnh hưởng xác minh.')
        tg('sendMessage',{'chat_id':chat,'text':'Danh sách liên kết tùy chọn:','reply_markup':{'inline_keyboard':rows}})
    elif data=='adm_link_add':
        with PENDING_ADMIN_LOCK: PENDING_ADMIN[uid]='add_optional_link'
        admin_text(chat,'🔗 GỬI LINK BẠN MUỐN THÊM\nHãy gửi trực tiếp link bot/nhóm/kênh, ví dụ:\nhttps://t.me/luxyffshare\nBot sẽ tự tạo nút từ link bạn gửi. Không cần nhập lệnh.')
    elif data.startswith('adm_link_'):
        try:
            lid=int(data.rsplit('_',1)[1]); item=next((x for x in optional_links() if x['id']==lid),None)
            if item:
                admin_text(chat,f"ID: {item['id']}\nTên nút: {item['title']}\nLink: {item['url']}\n\nSửa: /editlink {item['id']} | Tên nút | URL\nXóa: /dellink {item['id']}")
        except Exception: admin_text(chat,'Không tìm thấy liên kết.')
    elif data=='adm_groups':
        gs=groups(); rows=[]
        for g in gs: rows.append([{'text':f"✏️ {g['title']} (ID {g['id']})",'callback_data':f"adm_show_{g['id']}"},{'text':'🗑 Xóa','callback_data':f"adm_del_{g['id']}"}])
        rows.append([{'text':'➕ Thêm nhóm/kênh','callback_data':'adm_add'}])
        tg('sendMessage',{'chat_id':chat,'text':'📋 DANH SÁCH NHÓM/KÊNH','reply_markup':{'inline_keyboard':rows}})
    elif data.startswith('adm_show_'):
        gid=int(data.rsplit('_',1)[1]); g=next((x for x in groups() if x['id']==gid),None)
        if g: admin_text(chat,f"ID: {g['id']}\nTên: {g['title']}\nChat ID: {g['chat_id']}\nLink: {g['url']}\n\nSửa: /editgroup {g['id']} | chat_id | Tên nhóm | link")
    elif data=='adm_add':
        with PENDING_ADMIN_LOCK: PENDING_ADMIN[uid]='add_required_group'
        admin_text(chat,'➕ THÊM NHÓM/KÊNH BẮT BUỘC\nGửi trực tiếp link nhóm hoặc kênh Telegram công khai, ví dụ:\nhttps://t.me/luxyffshare\n\nKhông cần nhập lệnh. Bot tự nhận diện từ link. Hãy cấp quyền quản trị cho bot để kiểm tra thành viên.')
    elif data=='adm_edit': admin_text(chat,'Dùng: /editgroup ID | chat_id | Tên nhóm | https://t.me/link')
    elif data=='adm_del':
        gs=groups(); rows=[[{'text':'🗑 '+g['title'],'callback_data':f"adm_del_{g['id']}"}] for g in gs]
        if not rows: rows=[[{'text':'(Chưa có nhóm/kênh)','callback_data':'adm_groups'}]]
        rows.append([{'text':'↩️ Quay lại quản trị','callback_data':'adm_back'}])
        tg('sendMessage',{'chat_id':chat,'text':'Chọn nhóm/kênh muốn xóa. Nhấn một nút là xóa ngay khỏi danh sách bắt buộc:','reply_markup':{'inline_keyboard':rows}})
    elif data=='adm_back': admin_menu(chat)
    elif data=='adm_users':
        with db() as con: n=con.execute('SELECT COUNT(*) FROM users').fetchone()[0]; unlocked=con.execute('SELECT COUNT(*) FROM users WHERE unlocked=1').fetchone()[0]
        admin_text(chat,f'👥 Tổng người dùng: {n}\n🔓 Đã mở khóa: {unlocked}')
    elif data=='adm_refs':
        with db() as con: n=con.execute('SELECT COUNT(*) FROM referrals').fetchone()[0]; top=con.execute('SELECT referrer_id,COUNT(*) n FROM referrals GROUP BY referrer_id ORDER BY n DESC LIMIT 10').fetchall()
        admin_text(chat,'🎁 Tổng lượt giới thiệu hợp lệ: '+str(n)+'\n'+ '\n'.join(f"{x['referrer_id']}: {x['n']}" for x in top))
    elif data=='adm_broadcast':
        with PENDING_ADMIN_LOCK: PENDING_ADMIN[uid]='broadcast'
        admin_text(chat,'📣 Hãy gửi nội dung thông báo cho tất cả người dùng. Bot sẽ gửi ở chế độ nền và tự thống kê số gửi thành công/thất bại.')
    elif data=='adm_lock':
        with PENDING_ADMIN_LOCK: PENDING_ADMIN[uid]='lock_user'
        admin_text(chat,'🔒 Hãy gửi ID người dùng cần khóa.')
    elif data=='adm_user_list' or data.startswith('adm_user_list_'):
        page=int(data.rsplit('_',1)[1]) if data!='adm_user_list' else 0
        page=max(0,page)
        with db() as con:
            rows=con.execute('SELECT user_id,username,first_name FROM users ORDER BY user_id DESC LIMIT 20 OFFSET ?',(page*20,)).fetchall()
            total=con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        lines=[f"{r['user_id']} · @{r['username']}" if r['username'] else f"{r['user_id']} · {r['first_name'] or 'Người dùng'}" for r in rows]
        nav=[]
        if page>0: nav.append({'text':'⬅️ Trước','callback_data':f'adm_user_list_{page-1}'})
        if (page+1)*20<total: nav.append({'text':'Tiếp ➡️','callback_data':f'adm_user_list_{page+1}'})
        markup={'inline_keyboard':[nav]} if nav else {'inline_keyboard':[[{'text':'↩️ Quản trị','callback_data':'adm_back'}]]}
        tg('sendMessage',{'chat_id':chat,'text':f'👥 DANH SÁCH NGƯỜI DÙNG\nTổng: {total} · Trang {page+1}\n\n'+('\n'.join(lines) if lines else 'Chưa có người dùng.'),'reply_markup':markup})
    elif data=='adm_maintenance':
        enabled=maintenance_enabled()
        tg('sendMessage',{'chat_id':chat,'text':'🛠 CHẾ ĐỘ BẢO TRÌ\nTrạng thái: '+('ĐANG BẬT' if enabled else 'ĐANG TẮT'),'reply_markup':{'inline_keyboard':[[{'text':'🔴 TẮT BẢO TRÌ' if enabled else '🟢 BẬT BẢO TRÌ','callback_data':'adm_maintenance_toggle'}],[{'text':'↩️ Quay lại','callback_data':'adm_back'}]]}})
    elif data=='adm_maintenance_toggle':
        enabled=not maintenance_enabled()
        with db() as con: con.execute("INSERT INTO admin_settings(setting,value) VALUES('maintenance',?) ON CONFLICT(setting) DO UPDATE SET value=excluded.value",('1' if enabled else '0',))
        tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Đã bật bảo trì.' if enabled else 'Đã tắt bảo trì.'})
        # Refresh the toggle screen with the new state.
        tg('sendMessage',{'chat_id':chat,'text':'🛠 CHẾ ĐỘ BẢO TRÌ\nTrạng thái: '+('ĐANG BẬT' if enabled else 'ĐANG TẮT'),'reply_markup':{'inline_keyboard':[[{'text':'🔴 TẮT BẢO TRÌ' if enabled else '🟢 BẬT BẢO TRÌ','callback_data':'adm_maintenance_toggle'}],[{'text':'↩️ Quay lại','callback_data':'adm_back'}]]}})
    elif data=='adm_status': admin_text(chat,f'🤖 Bot token: {bool(BOT_TOKEN)}\n🌐 Mini App: {bool(WEBAPP_URL)}\n👥 Nhóm bắt buộc: {len(groups())}\n🔗 Link tùy chọn: {len(optional_links())}')
    elif data.startswith('adm_del_'):
        try:
            gid=int(data.rsplit('_',1)[1])
            with db() as con: cur=con.execute('DELETE FROM groups_cfg WHERE id=?',(gid,))
            admin_text(chat,'🗑 Đã xóa nhóm.' if cur.rowcount else 'Không tìm thấy nhóm.')
        except Exception: admin_text(chat,'Không thể xóa nhóm.')
    tg('answerCallbackQuery',{'callback_query_id':c['id']})


def access_check(raw):
    user=validate_webapp_init_data(raw)
    if not user or not user.get('id'): return None,({'ok':False,'reason':'invalid_telegram','message':'Hãy mở Mini App từ bot Telegram.'},401)
    uid=int(user['id'])
    if uid not in ADMIN_IDS and maintenance_enabled():
        return None,({'ok':False,'reason':'maintenance','message':'🛠 Bot đang bảo trì, hãy quay lại sau nhé.'},503)
    if uid not in ADMIN_IDS and is_blocked(uid):
        return None,({'ok':False,'reason':'blocked','message':'🔒 Tài khoản của bạn đã bị khóa.'},403)
    if uid not in ADMIN_IDS:
        with db() as con: row=con.execute('SELECT unlocked FROM users WHERE user_id=?',(uid,)).fetchone()
        if not row: return None,({'ok':False,'reason':'not_started','message':'Bạn chưa xác minh trong bot Telegram.'},403)
        miss=missing_groups(uid)
        if miss: return None,({'ok':False,'reason':'groups','missing':[g['title'] for g in miss],'message':'Bạn chưa tham gia đủ nhóm bắt buộc. Hãy quay lại bot và xác minh.'},403)
        count=referral_count(uid)
        if count<3: return None,({'ok':False,'reason':'referrals','referral_count':count,'remaining':3-count,'message':'Bạn chưa mời đủ 3 người bạn. Hãy quay lại bot và mời thêm bạn bè để mở khóa Mini App nhé!'},403)
        with db() as con: con.execute('UPDATE users SET unlocked=1 WHERE user_id=?',(uid,))
    return user,None


@app.get('/')
def home(): return send_from_directory(BASE_DIR,'index.html')
@app.get('/health')
def health():
    try:
        with db() as con: pc=con.execute('SELECT COUNT(*) FROM products').fetchone()[0]; kc=con.execute('SELECT COUNT(*) FROM keys_stock').fetchone()[0]
        return jsonify(ok=True,app='Jinny Cheat free key',telegram_bot_configured=bool(BOT_TOKEN),required_groups=len(groups()),products=pc,stock_keys=kc,database=DB_BACKEND,webhook=False)
    except Exception as e: return jsonify(ok=False,error=str(e)),500

@app.post('/api/access')
def api_access():
    body=request.get_json(silent=True) or {}; user,error=access_check(body.get('initData',''))
    if error: return jsonify(**error[0]),error[1]
    uid=int(user['id'])
    return jsonify(ok=True,admin=uid in ADMIN_IDS,referral_count=referral_count(uid),required=3,profile={'id':uid,'first_name':user.get('first_name',''),'last_name':user.get('last_name',''),'username':user.get('username',''),'photo_url':user.get('photo_url','')})

@app.post('/api/products')
def api_products():
    body=request.get_json(silent=True) or {}; user,error=access_check(body.get('initData',''))
    if error: return jsonify(**error[0]),error[1]
    with db() as con:
        admin=int(user['id']) in ADMIN_IDS
        where='' if admin else 'WHERE p.active=1'
        rows=con.execute(f'SELECT p.*,COUNT(k.id) stock FROM products p LEFT JOIN keys_stock k ON k.product_id=p.id {where} GROUP BY p.id ORDER BY p.id').fetchall()
        return jsonify(ok=True,products=[{'id':r['id'],'name':r['name'],'price':r['price'],'duration':r['duration'] or '', 'desc':r['description'] or '', 'image':r['image'] or '', 'keys':[], 'stock':r['stock'],'active':bool(r['active'])} for r in rows])

@app.post('/api/admin/products')
def api_admin_products():
    body=request.get_json(silent=True) or {}; user,error=access_check(body.get('initData',''))
    if error: return jsonify(**error[0]),error[1]
    if int(user['id']) not in ADMIN_IDS: return jsonify(ok=False,message='Không có quyền quản trị.'),403
    action=body.get('action'); p=body.get('product') or {}
    with db() as con:
        if action=='save':
            pid=str(p.get('id') or ('p'+str(int(time.time()*1000))))[:80]
            if not str(p.get('name','')).strip(): return jsonify(ok=False,message='Tên sản phẩm là bắt buộc.'),400
            con.execute('INSERT INTO products(id,name,price,duration,description,image) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,price=excluded.price,duration=excluded.duration,description=excluded.description,image=CASE WHEN excluded.image IS NULL OR excluded.image=\'\' THEN products.image ELSE excluded.image END',(pid,str(p['name'])[:150],str(p.get('price') or 'Miễn phí')[:80],str(p.get('duration') or '')[:80],str(p.get('desc',''))[:1000],str(p.get('image',''))))
            return jsonify(ok=True,id=pid)
        if action=='set_active':
            con.execute('UPDATE products SET active=? WHERE id=?',(1 if p.get('active') else 0,str(p.get('id',''))))
            return jsonify(ok=True)
        if action=='delete': con.execute('DELETE FROM products WHERE id=?',(str(p.get('id','')),)); return jsonify(ok=True)
        if action=='list_keys':
            rows=con.execute('SELECT id,key_value FROM keys_stock WHERE product_id=? ORDER BY id',(str(p.get('id','')),)).fetchall()
            return jsonify(ok=True,keys=[dict(x) for x in rows])
        if action=='import_legacy':
            if con.execute("SELECT 1 FROM migration_flags WHERE flag='browser_localstorage_v1'").fetchone():
                return jsonify(ok=True,already_imported=True)
            legacy=body.get('products') or []
            imported_products=imported_keys=0
            if not isinstance(legacy,list): return jsonify(ok=False,message='Dữ liệu sản phẩm cũ không hợp lệ.'),400
            for item in legacy[:300]:
                if not isinstance(item,dict) or not item.get('id') or not item.get('name') or not item.get('price'): continue
                pid=str(item['id'])[:80]
                con.execute('INSERT INTO products(id,name,price,duration,description,image) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,price=excluded.price,duration=excluded.duration,description=excluded.description,image=CASE WHEN excluded.image IS NULL OR excluded.image='' THEN products.image ELSE excluded.image END',(pid,str(item['name'])[:150],str(item['price'])[:80],str(item.get('duration',''))[:80],str(item.get('desc',''))[:1000],str(item.get('image',''))))
                imported_products+=1
                keys=item.get('keys') or []
                if isinstance(keys,list):
                    for key_value in keys[:10000]:
                        key_value=str(key_value)[:500]
                        if not key_value: continue
                        exists=con.execute('SELECT 1 FROM keys_stock WHERE product_id=? AND key_value=?',(pid,key_value)).fetchone()
                        if not exists:
                            con.execute('INSERT INTO keys_stock(product_id,key_value) VALUES(?,?)',(pid,key_value)); imported_keys+=1
            con.execute("INSERT INTO migration_flags(flag) VALUES('browser_localstorage_v1')")
            return jsonify(ok=True,imported_products=imported_products,imported_keys=imported_keys,imported_claims=0)
        if action=='add_key':
            if not con.execute('SELECT 1 FROM products WHERE id=?',(str(p.get('id','')),)).fetchone(): return jsonify(ok=False,message='Không tìm thấy sản phẩm.'),404
            con.execute('INSERT INTO keys_stock(product_id,key_value) VALUES(?,?)',(str(p['id']),str(p.get('key',''))[:500])); return jsonify(ok=True)
        if action=='delete_key': con.execute('DELETE FROM keys_stock WHERE id=?',(int(p.get('key_id',0)),)); return jsonify(ok=True)
    return jsonify(ok=False,message='Thao tác không hợp lệ.'),400

@app.post('/api/claim')
def api_claim():
    body=request.get_json(silent=True) or {}; user,error=access_check(body.get('initData',''))
    if error: return jsonify(**error[0]),error[1]
    uid=int(user['id']); pid=str(body.get('productId',''))
    if uid not in ADMIN_IDS:
        with db() as con:
            date_sql = 'claimed_at::date=CURRENT_DATE' if DB_BACKEND == 'postgres' else "date(claimed_at)=date('now')"
            today=con.execute(f'SELECT COUNT(*) FROM claims WHERE user_id=? AND {date_sql}',(uid,)).fetchone()[0]
            if today>=2: return jsonify(ok=False,message='Bạn đã sử dụng hết giới hạn 2 key hôm nay.'),429
    try:
        with db() as con:
            p=con.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p: return jsonify(ok=False,message='Không tìm thấy sản phẩm.'),404
            lock_sql = ' FOR UPDATE SKIP LOCKED' if DB_BACKEND == 'postgres' else ''
            key=con.execute('SELECT id,key_value FROM keys_stock WHERE product_id=? ORDER BY id LIMIT 1'+lock_sql,(pid,)).fetchone()
            if not key: return jsonify(ok=False,message='Sản phẩm đã hết key.'),409
            con.execute('DELETE FROM keys_stock WHERE id=?',(key['id'],))
            con.execute('INSERT INTO claims(user_id,product_id,product_name,key_value) VALUES(?,?,?,?)',(uid,pid,p['name'],key['key_value']))
            return jsonify(ok=True,key=key['key_value'],productName=p['name'],price=p['price'])
    except Exception:
        app.logger.exception('Claim transaction failed'); return jsonify(ok=False,message='Không thể hoàn tất giao dịch, vui lòng thử lại.'),500

@app.post('/api/claim-history')
def api_claim_history():
    body=request.get_json(silent=True) or {}; user,error=access_check(body.get('initData',''))
    if error: return jsonify(**error[0]),error[1]
    with db() as con:
        rows=con.execute('SELECT product_id AS productId,product_name AS productName,key_value AS key,claimed_at AS claimedAt FROM claims WHERE user_id=? ORDER BY id DESC LIMIT 200',(int(user['id']),)).fetchall()
    return jsonify(ok=True,claims=[dict(x) for x in rows])

@app.get('/<path:filename>')
def assets(filename): return send_from_directory(BASE_DIR,filename)

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','3000')))
