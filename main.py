import json, os, secrets, sqlite3, threading, urllib.request, hmac, hashlib, time
from pathlib import Path
from urllib.parse import parse_qsl
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
WEBAPP_URL = os.getenv('WEBAPP_URL', '').strip()
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()
PUBLIC_URL = os.getenv('RENDER_EXTERNAL_URL', '').strip().rstrip('/')
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
DB_PATH = os.getenv('DATABASE_PATH', '/var/data/jinny.db')
if not Path(DB_PATH).parent.exists():
    DB_PATH = str(BASE_DIR / 'jinny.db')


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute('CREATE TABLE IF NOT EXISTS groups_cfg (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, referrer_id INTEGER, qualified INTEGER NOT NULL DEFAULT 0)')
        con.execute('CREATE TABLE IF NOT EXISTS referrals (referrer_id INTEGER NOT NULL, referred_id INTEGER UNIQUE NOT NULL, qualified_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        con.execute('CREATE TABLE IF NOT EXISTS claims (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id TEXT, product_name TEXT NOT NULL, key_value TEXT NOT NULL, claimed_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        count = con.execute('SELECT COUNT(*) FROM groups_cfg').fetchone()[0]
        if count == 0:
            try:
                initial = json.loads(os.getenv('REQUIRED_GROUPS', '[]'))
            except Exception:
                initial = []
            for g in initial if isinstance(initial, list) else []:
                if g.get('chat_id') and g.get('url'):
                    con.execute('INSERT OR IGNORE INTO groups_cfg(chat_id,title,url) VALUES(?,?,?)',
                                (str(g['chat_id']), str(g.get('title') or g['chat_id']), str(g['url'])))

init_db()


def tg(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN chưa được cấu hình')
    req = urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',
        data=json.dumps(payload or {}).encode(), headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get('ok'):
        raise RuntimeError(result.get('description', 'Telegram API error'))
    return result.get('result')


def groups():
    with db() as con:
        return [dict(x) for x in con.execute('SELECT id,chat_id,title,url FROM groups_cfg ORDER BY id').fetchall()]


def upsert_user(user, referrer_id=None):
    uid = int(user['id'])
    username = user.get('username')
    first_name = user.get('first_name', '')
    with db() as con:
        old = con.execute('SELECT referrer_id FROM users WHERE user_id=?', (uid,)).fetchone()
        if old:
            con.execute('UPDATE users SET username=?, first_name=? WHERE user_id=?', (username, first_name, uid))
        else:
            if referrer_id == uid:
                referrer_id = None
            con.execute('INSERT INTO users(user_id,username,first_name,referrer_id) VALUES(?,?,?,?)',
                        (uid, username, first_name, referrer_id))


def validate_webapp_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop('hash', '')
        auth_date = int(pairs.get('auth_date', '0'))
        if not received_hash or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
            return None
        check_string = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        return json.loads(pairs.get('user', '{}'))
    except Exception:
        return None


def referral_count(uid):
    with db() as con:
        return con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?', (uid,)).fetchone()[0]


def bot_username():
    try:
        return tg('getMe').get('username', '')
    except Exception:
        return ''

BOT_USERNAME = ''


def referral_link(uid):
    global BOT_USERNAME
    if not BOT_USERNAME:
        BOT_USERNAME = bot_username()
    return f'https://t.me/{BOT_USERNAME}?start=ref_{uid}' if BOT_USERNAME else ''


def qualify_referral(uid):
    with db() as con:
        user = con.execute('SELECT referrer_id,qualified FROM users WHERE user_id=?', (uid,)).fetchone()
        if not user or user['qualified']:
            return
        con.execute('UPDATE users SET qualified=1 WHERE user_id=?', (uid,))
        inviter = user['referrer_id']
        if inviter and inviter != uid:
            con.execute('INSERT OR IGNORE INTO referrals(referrer_id,referred_id) VALUES(?,?)', (inviter, uid))
            inserted = con.execute('SELECT changes()').fetchone()[0]
        else:
            inserted = 0
    if inviter and inserted:
        try:
            with db() as con:
                who = con.execute('SELECT username,first_name FROM users WHERE user_id=?', (uid,)).fetchone()
            label = ('@' + who['username']) if who and who['username'] else (who['first_name'] if who else 'Người dùng')
            count = referral_count(inviter)
            tg('sendMessage', {'chat_id': inviter, 'text': f'🎉 Có người tham gia qua link giới thiệu của bạn!\n👤 {label}\n🆔 ID: {uid}\n📊 Tiến độ: {count}/3 người.\n' + (f'🎁 Bạn đã đủ điều kiện mở Mini App.' if count >= 3 else f'⏳ Còn thiếu {3-count} người nữa.')})
        except Exception:
            app.logger.exception('Không gửi được thông báo giới thiệu')


def is_member(user_id, group):
    result = tg('getChatMember', {'chat_id': group['chat_id'], 'user_id': user_id})
    status = result.get('status')
    return status in ('creator','administrator','member') or (status == 'restricted' and result.get('is_member') is True)


def missing_groups(user_id):
    missing=[]
    for group in groups():
        try:
            if not is_member(user_id, group): missing.append(group)
        except Exception:
            missing.append(group)
    return missing


def group_keyboard():
    rows=[]
    for g in groups():
        if g['url']: rows.append([{'text':'📢 '+g['title'], 'url':g['url']}])
    rows.append([{'text':'✅ Tôi đã tham gia - Xác minh','callback_data':'verify'}])
    return {'inline_keyboard':rows}


def send_prompt(chat_id, message_id=None):
    payload={'chat_id':chat_id,'text':'👋 Để tiếp tục, bạn cần tham gia đầy đủ các kênh/nhóm bên dưới rồi nhấn Xác minh.','reply_markup':group_keyboard()}
    if message_id:
        payload['message_id']=message_id
        try: tg('editMessageText',payload)
        except Exception: tg('sendMessage',{k:v for k,v in payload.items() if k!='message_id'})
    else: tg('sendMessage',payload)


def send_referral_gate(chat_id, user_id):
    count=referral_count(user_id)
    link=referral_link(user_id)
    remaining=max(0,3-count)
    text=f'✅ Bạn đã tham gia đủ nhóm.\n\n🎁 Để mở Mini App, hãy mời 3 người bạn mới tham gia bot bằng link riêng của bạn.\n\n🔗 Link giới thiệu: {link or "Chưa lấy được link bot, hãy kiểm tra BOT_TOKEN."}\n📊 Tiến độ: {count}/3 người.\n⏳ Còn thiếu: {remaining} người.'
    if count>=3:
        send_app_button(chat_id)
    else:
        tg('sendMessage',{'chat_id':chat_id,'text':text,'reply_markup':{'inline_keyboard':[[{'text':'🔗 Mở link giới thiệu','url':link}] ]} if link else None})


def send_app_button(chat_id):
    if not WEBAPP_URL:
        tg('sendMessage',{'chat_id':chat_id,'text':'✅ Đã đủ điều kiện. Chủ bot chưa cấu hình WEBAPP_URL trên Render.'})
        return
    tg('sendMessage',{'chat_id':chat_id,'text':'🎉 Bạn đã đủ điều kiện! Bấm nút bên dưới để mở Mini App.',
        'reply_markup':{'inline_keyboard':[[{'text':'🚀 MỞ MINI APP','web_app':{'url':WEBAPP_URL}}]]}})


def admin_menu(chat_id):
    tg('sendMessage',{'chat_id':chat_id,'text':'🛠 MENU QUẢN TRỊ\n\nChọn chức năng bên dưới. Các lệnh thêm/sửa/xóa nhóm được hướng dẫn trong từng mục.',
        'reply_markup':{'inline_keyboard':[[{'text':'📋 Quản lý nhóm','callback_data':'adm_groups'}],
        [{'text':'➕ Hướng dẫn thêm nhóm','callback_data':'adm_add'}],
        [{'text':'✏️ Hướng dẫn sửa nhóm','callback_data':'adm_edit'}],
        [{'text':'🗑 Hướng dẫn xóa nhóm','callback_data':'adm_del'}]]}})


def admin_groups(chat_id):
    gs=groups(); rows=[]
    for g in gs:
        rows.append([{'text':f"✏️ {g['title']} ({g['chat_id']})",'callback_data':f"adm_show_{g['id']}"}, {'text':'🗑','callback_data':f"adm_del_{g['id']}"}])
    rows.append([{'text':'➕ Thêm nhóm','callback_data':'adm_add'}])
    tg('sendMessage',{'chat_id':chat_id,'text':'📋 DANH SÁCH NHÓM BẮT BUỘC\nChọn nhóm để xem lệnh sửa/xóa.' if gs else 'Chưa có nhóm nào. Bấm Thêm nhóm để bắt đầu.', 'reply_markup':{'inline_keyboard':rows}})


def admin_text(chat_id, text):
    tg('sendMessage',{'chat_id':chat_id,'text':text})


def handle_admin_command(message):
    chat_id=message['chat']['id']; parts=message.get('text','').split(maxsplit=1); cmd=parts[0].split('@')[0].lower()
    arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='/admin': admin_menu(chat_id); return True
    if cmd=='/addgroup':
        fields=[x.strip() for x in arg.split('|')]
        if len(fields)!=3 or not all(fields): admin_text(chat_id,'Cú pháp:\n/addgroup @chat_id | Tên nhóm | https://t.me/link'); return True
        try:
            with db() as con: con.execute('INSERT INTO groups_cfg(chat_id,title,url) VALUES(?,?,?)',tuple(fields))
            admin_text(chat_id,'✅ Đã thêm nhóm bắt buộc.')
        except sqlite3.IntegrityError: admin_text(chat_id,'⚠️ chat_id này đã tồn tại.')
        return True
    if cmd=='/editgroup':
        fields=[x.strip() for x in arg.split('|')]
        if len(fields)!=4: admin_text(chat_id,'Cú pháp:\n/editgroup ID | chat_id | Tên nhóm | https://t.me/link\nXem ID bằng /admin → Quản lý nhóm.'); return True
        try:
            gid=int(fields[0])
            with db() as con: cur=con.execute('UPDATE groups_cfg SET chat_id=?,title=?,url=? WHERE id=?',(fields[1],fields[2],fields[3],gid))
            admin_text(chat_id,'✅ Đã cập nhật nhóm.' if cur.rowcount else 'Không tìm thấy nhóm có ID đó.')
        except Exception: admin_text(chat_id,'Dữ liệu không hợp lệ.');
        return True
    if cmd=='/delgroup':
        try:
            gid=int(arg)
            with db() as con: cur=con.execute('DELETE FROM groups_cfg WHERE id=?',(gid,))
            admin_text(chat_id,'🗑 Đã xóa nhóm.' if cur.rowcount else 'Không tìm thấy nhóm.')
        except Exception: admin_text(chat_id,'Cú pháp: /delgroup ID')
        return True
    return False


def process_update(update):
    message=update.get('message')
    if message and message.get('text','').startswith('/'):
        chat=message.get('chat',{}); user=message.get('from',{}); uid=int(user.get('id',0)); text=message.get('text','')
        if chat.get('type')!='private': return
        if uid in ADMIN_IDS and handle_admin_command(message): return
        if text.split()[0].split('@')[0]=='/start':
            args=text.split(maxsplit=1); payload=args[1].strip() if len(args)>1 else ''
            ref=None
            if payload.startswith('ref_') and payload[4:].isdigit(): ref=int(payload[4:])
            upsert_user(user,ref)
            if uid in ADMIN_IDS: send_app_button(chat['id']); return
            if not groups(): admin_text(chat['id'],'⚠️ Chủ bot chưa cấu hình nhóm bắt buộc.'); return
            missing=missing_groups(uid)
            if missing: send_prompt(chat['id']); return
            qualify_referral(uid)
            send_referral_gate(chat['id'],uid)
            return
    callback=update.get('callback_query')
    if callback:
        data=callback.get('data',''); user=callback.get('from',{}); uid=int(user.get('id',0)); msg=callback.get('message',{}); chat_id=msg.get('chat',{}).get('id')
        if data=='verify':
            upsert_user(user)
            if uid in ADMIN_IDS:
                tg('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Admin được bỏ qua xác minh.'}); send_app_button(chat_id); return
            missing=missing_groups(uid)
            if missing:
                names=', '.join(g['title'] for g in missing)
                tg('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Bạn chưa tham gia đủ: '+names,'show_alert':True})
                send_prompt(chat_id,msg.get('message_id')); return
            qualify_referral(uid)
            tg('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Đã xác minh nhóm!'})
            send_referral_gate(chat_id,uid); return
        if uid in ADMIN_IDS:
            if data=='adm_groups': admin_groups(chat_id)
            elif data=='adm_add': admin_text(chat_id,'➕ Thêm nhóm bằng lệnh:\n/addgroup @chat_id | Tên nhóm | https://t.me/link\nBot phải là admin của nhóm/kênh để kiểm tra thành viên.')
            elif data=='adm_edit': admin_text(chat_id,'✏️ Sửa nhóm bằng lệnh:\n/editgroup ID | chat_id | Tên nhóm | https://t.me/link\nXem ID bằng /admin → Quản lý nhóm → chọn nhóm.')
            elif data=='adm_del': admin_text(chat_id,'🗑 Xóa nhóm bằng lệnh:\n/delgroup ID')
            elif data.startswith('adm_show_'):
                gid=int(data.rsplit('_',1)[1]); g=next((x for x in groups() if x['id']==gid),None)
                if g: tg('sendMessage',{'chat_id':chat_id,'text':f"ID: {g['id']}\nTên: {g['title']}\nChat ID: {g['chat_id']}\nLink: {g['url']}\n\nSửa bằng: /editgroup {g['id']} | chat_id | Tên nhóm | link",'reply_markup':{'inline_keyboard':[[{'text':'✏️ Hướng dẫn sửa','callback_data':'adm_edit'}]]}})
            elif data.startswith('adm_del_'):
                gid=int(data.rsplit('_',1)[1])
                with db() as con: cur=con.execute('DELETE FROM groups_cfg WHERE id=?',(gid,))
                admin_text(chat_id,'🗑 Đã xóa nhóm.' if cur.rowcount else 'Không tìm thấy nhóm.')
            tg('answerCallbackQuery',{'callback_query_id':callback['id']})


@app.get('/')
def home(): return send_from_directory(BASE_DIR,'index.html')

@app.get('/health')
def health(): return jsonify(ok=True,app='Jinny Cheat free key',hosting='Render',telegram_bot_configured=bool(BOT_TOKEN),required_groups=len(groups()),database=DB_PATH)

@app.post('/telegram/webhook')
def telegram_webhook():
    if not WEBHOOK_SECRET: return 'Webhook secret not configured',503
    supplied=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
    if not secrets.compare_digest(supplied,WEBHOOK_SECRET): return 'Forbidden',403
    try: process_update(request.get_json(silent=True) or {})
    except Exception: app.logger.exception('Telegram update failed')
    return jsonify(ok=True)

def webapp_user_with_access(init_data):
    user = validate_webapp_init_data(init_data)
    if not user or not user.get('id'):
        return None, {'ok':False,'reason':'invalid_telegram','message':'Hãy mở Mini App từ bot Telegram.'}, 401
    uid = int(user['id'])
    if uid in ADMIN_IDS:
        return user, None, 200
    missing = missing_groups(uid)
    if missing:
        return None, {'ok':False,'reason':'groups','missing':[g['title'] for g in missing], 'message':'Bạn chưa tham gia đủ nhóm. Quay lại bot và nhấn Xác minh.'}, 403
    qualify_referral(uid)
    count = referral_count(uid)
    if count < 3:
        return None, {'ok':False,'reason':'referrals','referral_count':count,'remaining':3-count,'message':f'Bạn cần mời thêm {3-count} người bạn tham gia bot bằng link giới thiệu.'}, 403
    return user, None, 200


@app.post('/api/access')
def api_access():
    body = request.get_json(silent=True) or {}
    user, error, status = webapp_user_with_access(body.get('initData', ''))
    if error:
        return jsonify(**error), status
    uid = int(user['id'])
    return jsonify(ok=True, admin=uid in ADMIN_IDS, referral_count=referral_count(uid), required=3)


@app.post('/api/claim-history')
def api_claim_history():
    body = request.get_json(silent=True) or {}
    user, error, status = webapp_user_with_access(body.get('initData', ''))
    if error:
        return jsonify(**error), status
    uid = int(user['id'])
    action = body.get('action', 'list')
    with db() as con:
        if action == 'add':
            claim = body.get('claim') or {}
            if not claim.get('productName') or not claim.get('key'):
                return jsonify(ok=False, message='Thiếu thông tin giao dịch.'), 400
            con.execute('INSERT INTO claims(user_id,product_id,product_name,key_value,claimed_at) VALUES(?,?,?,?,?)',
                        (uid, str(claim.get('productId','')), str(claim['productName'])[:150], str(claim['key'])[:500], str(claim.get('claimedAt',''))[:50]))
            return jsonify(ok=True)
        rows = con.execute('SELECT product_id AS productId,product_name AS productName,key_value AS key,claimed_at AS claimedAt FROM claims WHERE user_id=? ORDER BY id DESC LIMIT 200',(uid,)).fetchall()
        return jsonify(ok=True, claims=[dict(x) for x in rows])

@app.get('/<path:filename>')
def assets(filename): return send_from_directory(BASE_DIR,filename)


def configure_webhook():
    if not (BOT_TOKEN and WEBHOOK_SECRET and PUBLIC_URL):
        app.logger.warning('Webhook chưa bật: cần BOT_TOKEN, WEBHOOK_SECRET, RENDER_EXTERNAL_URL.'); return
    try:
        tg('setWebhook',{'url':PUBLIC_URL+'/telegram/webhook','secret_token':WEBHOOK_SECRET,'allowed_updates':['message','callback_query']})
        app.logger.info('Telegram webhook configured.')
    except Exception: app.logger.exception('Không thể cấu hình Telegram webhook.')

if os.getenv('DISABLE_WEBHOOK_SETUP','').lower() not in ('1','true','yes'):
    threading.Thread(target=configure_webhook,daemon=True).start()

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','3000')))
