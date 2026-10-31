import json
import os
import secrets
import threading
import urllib.request
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
try:
    REQUIRED_GROUPS = json.loads(os.getenv("REQUIRED_GROUPS", "[]"))
    if not isinstance(REQUIRED_GROUPS, list):
        REQUIRED_GROUPS = []
except json.JSONDecodeError:
    REQUIRED_GROUPS = []

def tg(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN chưa được cấu hình")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result.get("result")

def keyboard():
    rows = []
    for i, group in enumerate(REQUIRED_GROUPS):
        url = str(group.get("url", "")).strip()
        if url:
            rows.append([{"text": "📢 " + str(group.get("title", f"Nhóm {i+1}")), "url": url}])
    rows.append([{"text": "✅ Tôi đã tham gia - Xác minh", "callback_data": "verify"}])
    return {"inline_keyboard": rows}

def is_member(user_id, group):
    result = tg("getChatMember", {"chat_id": group["chat_id"], "user_id": user_id})
    status = result.get("status")
    return status in ("creator", "administrator", "member") or (
        status == "restricted" and result.get("is_member") is True
    )

def missing_groups(user_id):
    missing = []
    for group in REQUIRED_GROUPS:
        try:
            if not is_member(user_id, group):
                missing.append(group)
        except Exception:
            # Không kiểm tra được thì không cho qua.
            missing.append(group)
    return missing

def send_prompt(chat_id, message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": "👋 Để mở Mini App, bạn cần tham gia đầy đủ các nhóm bên dưới rồi nhấn Xác minh.",
        "reply_markup": keyboard(),
    }
    if message_id:
        payload["message_id"] = message_id
        tg("editMessageText", payload)
    else:
        tg("sendMessage", payload)

def send_app_button(chat_id):
    if not WEBAPP_URL:
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ Đã xác minh. Chủ bot chưa cấu hình WEBAPP_URL trên Render."})
        return
    tg("sendMessage", {
        "chat_id": chat_id,
        "text": "🎉 Bạn đã tham gia đủ nhóm. Bấm nút bên dưới để mở Mini App.",
        "reply_markup": {"inline_keyboard": [[{
            "text": "🚀 MỞ MINI APP", "web_app": {"url": WEBAPP_URL}
        }]]}
    })

def process_update(update):
    message = update.get("message")
    if message and message.get("text", "").split()[:1] == ["/start"]:
        chat = message.get("chat", {})
        user_id = message.get("from", {}).get("id")
        if chat.get("type") != "private":
            return
        if user_id in ADMIN_IDS:
            send_app_button(chat["id"])
        elif not REQUIRED_GROUPS:
            tg("sendMessage", {"chat_id": chat["id"], "text": "⚠️ Chủ bot chưa cấu hình danh sách nhóm bắt buộc trên Render."})
        elif missing_groups(user_id):
            send_prompt(chat["id"])
        else:
            send_app_button(chat["id"])
        return

    callback = update.get("callback_query")
    if callback and callback.get("data") == "verify":
        user_id = callback.get("from", {}).get("id")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if user_id in ADMIN_IDS:
            tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Admin được bỏ qua xác minh."})
            send_app_button(chat_id)
            return
        missing = missing_groups(user_id)
        if missing:
            names = ", ".join(str(g.get("title", "Nhóm")) for g in missing)
            tg("answerCallbackQuery", {
                "callback_query_id": callback["id"],
                "text": "Bạn chưa tham gia đủ: " + names,
                "show_alert": True
            })
            send_prompt(chat_id, message.get("message_id"))
        else:
            tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Xác minh thành công!"})
            send_app_button(chat_id)

@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/health")
def health():
    return jsonify(ok=True, app="Jinny Cheat free key", hosting="Render",
                   telegram_bot_configured=bool(BOT_TOKEN), required_groups=len(REQUIRED_GROUPS))

@app.post("/telegram/webhook")
def telegram_webhook():
    if not WEBHOOK_SECRET:
        return "Webhook secret not configured", 503
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(supplied, WEBHOOK_SECRET):
        return "Forbidden", 403
    try:
        process_update(request.get_json(silent=True) or {})
    except Exception:
        app.logger.exception("Telegram update failed")
    return jsonify(ok=True)

@app.get("/<path:filename>")
def assets(filename):
    return send_from_directory(BASE_DIR, filename)

def configure_webhook():
    if not (BOT_TOKEN and WEBHOOK_SECRET and PUBLIC_URL):
        app.logger.warning("Webhook chưa bật: cần BOT_TOKEN, WEBHOOK_SECRET, RENDER_EXTERNAL_URL.")
        return
    try:
        tg("setWebhook", {
            "url": PUBLIC_URL + "/telegram/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"]
        })
        app.logger.info("Telegram webhook configured.")
    except Exception:
        app.logger.exception("Không thể cấu hình Telegram webhook.")

if os.getenv("DISABLE_WEBHOOK_SETUP", "").lower() not in ("1", "true", "yes"):
    threading.Thread(target=configure_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "3000")))
