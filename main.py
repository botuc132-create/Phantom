import os, json, sqlite3, time, hmac, hashlib, threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
WEBAPP_URL = os.getenv('WEBAPP_URL', '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
DB_PATH = os.getenv('DATABASE_PATH', '/var/data/jinny.db')
if not Path(DB_PATH).parent.exists():
    DB_PATH = str(BASE_DIR / 'jinny.db')


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
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
        con.executescript('''
        CREATE TABLE IF NOT EXISTS groups_cfg(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT UNIQUE NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,referrer_id INTEGER,qualified INTEGER NOT NULL DEFAULT 0,groups_verified INTEGER NOT NULL DEFAULT 0,unlocked INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS referrals(referrer_id INTEGER NOT NULL,referred_id INTEGER UNIQUE NOT NULL,qualified_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,name TEXT NOT NULL,price TEXT NOT NULL,duration TEXT,description TEXT,image TEXT,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS keys_stock(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id TEXT NOT NULL,key_value TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,product_id TEXT,product_name TEXT NOT NULL,key_value TEXT NOT NULL,claimed_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS migration_flags(flag TEXT PRIMARY KEY,completed_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_claim_user ON claims(user_id,id DESC);
        CREATE INDEX IF NOT EXISTS idx_referrer ON referrals(referrer_id);
        ''')
        # Safe additive migration for databases created by earlier ZIP versions.
        user_cols={r['name'] for r in con.execute('PRAGMA table_info(users)')}
        for name, declaration in [('groups_verified','INTEGER NOT NULL DEFAULT 0'),('unlocked','INTEGER NOT NULL DEFAULT 0'),('created_at',"TEXT DEFAULT CURRENT_TIMESTAMP")]:
            if name not in user_cols: con.execute(f'ALTER TABLE users ADD COLUMN {name} {declaration}')
        try:
            initial = json.loads(os.getenv('REQUIRED_GROUPS', '[]') or '[]')
        except (TypeError, ValueError):
            initial = []
        if isinstance(initial, list):
            for g in initial:
                if isinstance(g, dict) and g.get('chat_id') and g.get('url'):
                    con.execute('INSERT OR IGNORE INTO groups_cfg(chat_id,title,url) VALUES(?,?,?)',(str(g['chat_id']),str(g.get('title') or g['chat_id']),str(g['url'])))
        # Migrate existing browser starter catalogue into DB only when no catalogue exists.
        if con.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
            for p in [
                {'id':'basic','name':'Key Basic','price':'29.000đ','duration':'7 ngày','description':'Gói key cơ bản, phù hợp cho nhu cầu sử dụng cá nhân.'},
                {'id':'pro','name':'Key Pro','price':'79.000đ','duration':'30 ngày','description':'Gói nâng cao dành cho người cần thời gian sử dụng dài hơn.'},
                {'id':'vip','name':'Key VIP','price':'199.000đ','duration':'90 ngày','description':'Gói dài hạn với quyền lợi hỗ trợ ưu tiên.'}]:
                con.execute('INSERT OR IGNORE INTO products(id,name,price,duration,description) VALUES(?,?,?,?,?)',(p['id'],p['name'],p['price'],p['duration'],p['description']))

init_db()


def tg(method, payload=None, timeout=20):
    import urllib.request
    if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN chưa được cấu hình')
    req=urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',data=json.dumps(payload or {}).encode(),headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=timeout) as r: data=json.loads(r.read().decode())
    if not data.get('ok'): raise RuntimeError(data.get('description','Telegram API error'))
    return data.get('result')


def groups():
    with db() as con: return [dict(x) for x in con.execute('SELECT id,chat_id,title,url FROM groups_cfg ORDER BY id')]


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
    result=[]
    for g in groups():
        try:
            if not is_member(uid,g): result.append(g)
        except Exception: result.append(g)
    return result


def qualify_referral(uid):
    inviter=None; inserted=False
    with db() as con:
        u=con.execute('SELECT referrer_id,qualified FROM users WHERE user_id=?',(uid,)).fetchone()
        if not u or u['qualified']: return
        inviter=u['referrer_id']
        con.execute('UPDATE users SET qualified=1,groups_verified=1 WHERE user_id=?',(uid,))
        if inviter and inviter != uid:
            con.execute('INSERT OR IGNORE INTO referrals(referrer_id,referred_id) VALUES(?,?)',(inviter,uid))
            inserted=con.execute('SELECT changes()').fetchone()[0] == 1
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


def admin_menu(chat_id):
    tg('sendMessage',{'chat_id':chat_id,'text':'🛠 MENU QUẢN TRỊ','reply_markup':{'inline_keyboard':[
        [{'text':'📋 Quản lý nhóm/kênh','callback_data':'adm_groups'}],
        [{'text':'➕ Thêm nhóm/kênh','callback_data':'adm_add'}],
        [{'text':'✏️ Sửa nhóm/kênh','callback_data':'adm_edit'}],
        [{'text':'🗑 Xóa nhóm/kênh','callback_data':'adm_del'}],
        [{'text':'📊 Thống kê người dùng','callback_data':'adm_users'}],
        [{'text':'🎁 Thống kê giới thiệu','callback_data':'adm_refs'}],
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
        except sqlite3.IntegrityError: admin_text(chat,'chat_id đã tồn tại.')
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
    return False


def process_update(update):
    m=update.get('message')
    if m and m.get('chat',{}).get('type')=='private' and m.get('text','').startswith('/'):
        user=m.get('from',{}); uid=int(user['id']); text=m['text']
        if admin_command(m): return
        if text.split()[0].split('@')[0]=='/start':
            args=text.split(maxsplit=1); token=args[1] if len(args)>1 else ''; ref=int(token[4:]) if token.startswith('ref_') and token[4:].isdigit() else None
            upsert_user(user,ref)
            if uid in ADMIN_IDS: send_app_button(m['chat']['id']); return
            if not groups(): admin_text(m['chat']['id'],'⚠️ Chủ bot chưa cấu hình nhóm bắt buộc.'); return
            miss=missing_groups(uid)
            if miss: send_prompt(m['chat']['id']); return
            qualify_referral(uid); send_referral_gate(m['chat']['id'],uid); return
    c=update.get('callback_query')
    if not c: return
    data=c.get('data',''); user=c.get('from',{}); uid=int(user.get('id',0)); msg=c.get('message',{}); chat=msg.get('chat',{}).get('id')
    if data=='verify':
        upsert_user(user)
        if uid in ADMIN_IDS: tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Admin được cấp quyền.'}); send_app_button(chat); return
        miss=missing_groups(uid)
        if miss:
            tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Còn thiếu: '+', '.join(g['title'] for g in miss),'show_alert':True}); send_prompt(chat,msg.get('message_id')); return
        qualify_referral(uid); tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Đã xác minh nhóm!'}); send_referral_gate(chat,uid); return
    if uid not in ADMIN_IDS:
        tg('answerCallbackQuery',{'callback_query_id':c['id'],'text':'Bạn không có quyền.' ,'show_alert':True}); return
    if data=='adm_groups':
        gs=groups(); rows=[]
        for g in gs: rows.append([{'text':f"✏️ {g['title']} (ID {g['id']})",'callback_data':f"adm_show_{g['id']}"},{'text':'🗑 Xóa','callback_data':f"adm_del_{g['id']}"}])
        rows.append([{'text':'➕ Thêm nhóm/kênh','callback_data':'adm_add'}])
        tg('sendMessage',{'chat_id':chat,'text':'📋 DANH SÁCH NHÓM/KÊNH','reply_markup':{'inline_keyboard':rows}})
    elif data.startswith('adm_show_'):
        gid=int(data.rsplit('_',1)[1]); g=next((x for x in groups() if x['id']==gid),None)
        if g: admin_text(chat,f"ID: {g['id']}\nTên: {g['title']}\nChat ID: {g['chat_id']}\nLink: {g['url']}\n\nSửa: /editgroup {g['id']} | chat_id | Tên nhóm | link")
    elif data=='adm_add': admin_text(chat,'Dùng: /addgroup @chat_id | Tên nhóm | https://t.me/link\nBot cần quyền admin để kiểm tra thành viên.')
    elif data=='adm_edit': admin_text(chat,'Dùng: /editgroup ID | chat_id | Tên nhóm | https://t.me/link')
    elif data=='adm_del': admin_text(chat,'Dùng: /delgroup ID')
    elif data=='adm_users':
        with db() as con: n=con.execute('SELECT COUNT(*) FROM users').fetchone()[0]; unlocked=con.execute('SELECT COUNT(*) FROM users WHERE unlocked=1').fetchone()[0]
        admin_text(chat,f'👥 Tổng người dùng: {n}\n🔓 Đã mở khóa: {unlocked}')
    elif data=='adm_refs':
        with db() as con: n=con.execute('SELECT COUNT(*) FROM referrals').fetchone()[0]; top=con.execute('SELECT referrer_id,COUNT(*) n FROM referrals GROUP BY referrer_id ORDER BY n DESC LIMIT 10').fetchall()
        admin_text(chat,'🎁 Tổng lượt giới thiệu hợp lệ: '+str(n)+'\n'+ '\n'.join(f"{x['referrer_id']}: {x['n']}" for x in top))
    elif data=='adm_status': admin_text(chat,f'🤖 Bot token: {bool(BOT_TOKEN)}\n🌐 Mini App: {bool(WEBAPP_URL)}\n👥 Nhóm bắt buộc: {len(groups())}')
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
        return jsonify(ok=True,app='Jinny Cheat free key',telegram_bot_configured=bool(BOT_TOKEN),required_groups=len(groups()),products=pc,stock_keys=kc,database=DB_PATH,webhook=False)
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
        rows=con.execute('SELECT p.*,COUNT(k.id) stock FROM products p LEFT JOIN keys_stock k ON k.product_id=p.id WHERE p.active=1 GROUP BY p.id ORDER BY p.rowid').fetchall()
        return jsonify(ok=True,products=[{'id':r['id'],'name':r['name'],'price':r['price'],'duration':r['duration'] or 'Không giới hạn','desc':r['description'] or '', 'image':r['image'] or '', 'keys':[], 'stock':r['stock']} for r in rows])

@app.post('/api/admin/products')
def api_admin_products():
    body=request.get_json(silent=True) or {}; user,error=access_check(body.get('initData',''))
    if error: return jsonify(**error[0]),error[1]
    if int(user['id']) not in ADMIN_IDS: return jsonify(ok=False,message='Không có quyền quản trị.'),403
    action=body.get('action'); p=body.get('product') or {}
    with db() as con:
        if action=='save':
            pid=str(p.get('id') or ('p'+str(int(time.time()*1000))))[:80]
            if not p.get('name') or not p.get('price'): return jsonify(ok=False,message='Tên và giá là bắt buộc.'),400
            con.execute('INSERT INTO products(id,name,price,duration,description,image) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,price=excluded.price,duration=excluded.duration,description=excluded.description,image=CASE WHEN excluded.image IS NULL OR excluded.image=\'\' THEN products.image ELSE excluded.image END',(pid,str(p['name'])[:150],str(p['price'])[:80],str(p.get('duration',''))[:80],str(p.get('desc',''))[:1000],str(p.get('image',''))))
            return jsonify(ok=True,id=pid)
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
            today=con.execute("SELECT COUNT(*) FROM claims WHERE user_id=? AND date(claimed_at)=date('now')",(uid,)).fetchone()[0]
            if today>=2: return jsonify(ok=False,message='Bạn đã sử dụng hết giới hạn 2 key hôm nay.'),429
    try:
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            p=con.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p: return jsonify(ok=False,message='Không tìm thấy sản phẩm.'),404
            key=con.execute('SELECT id,key_value FROM keys_stock WHERE product_id=? ORDER BY id LIMIT 1',(pid,)).fetchone()
            if not key: return jsonify(ok=False,message='Sản phẩm đã hết key.'),409
            con.execute('DELETE FROM keys_stock WHERE id=?',(key['id'],))
            con.execute('INSERT INTO claims(user_id,product_id,product_name,key_value) VALUES(?,?,?,?)',(uid,pid,p['name'],key['key_value']))
            return jsonify(ok=True,key=key['key_value'],productName=p['name'],price=p['price'])
    except sqlite3.Error:
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
