import os
import hmac
import hashlib
import time
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ACCURATE_API_TOKEN = os.environ.get("ACCURATE_API_TOKEN")
ACCURATE_SIGNATURE_SECRET = os.environ.get("ACCURATE_SIGNATURE_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ACCURATE_BASE_URL = "https://account.accurate.id/api"

conversation_history = {}

SYSTEM_PROMPT = """Kamu adalah asisten keuangan untuk perusahaan Print Master yang membantu tim admin mengecek invoice, hutang piutang, dan kinerja admin melalui Accurate Online.
Kamu berbicara dalam Bahasa Indonesia yang ramah dan profesional.
Format angka dalam Rupiah (contoh: Rp 1.500.000).
Status invoice: OPEN=Belum bayar, PARTIAL=Sebagian, PAID=Lunas, VOID=Batal.
Jawab singkat, padat, gunakan emoji."""


def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
    })


def make_signature(timestamp):
    """Generate signature: HMAC-SHA256(secret, token:timestamp)"""
    message = f"{ACCURATE_API_TOKEN}:{timestamp}"
    return hmac.new(
        ACCURATE_SIGNATURE_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def accurate_headers():
    timestamp = str(int(time.time() * 1000))
    signature = make_signature(timestamp)
    return {
        "Authorization": f"Bearer {ACCURATE_API_TOKEN}",
        "X-Api-Signature": signature,
        "X-Api-Timestamp": timestamp,
        "Content-Type": "application/json"
    }


def get_db_list():
    try:
        r = requests.get(
            f"{ACCURATE_BASE_URL}/db-list.do?fields=id,alias,host",
            headers=accurate_headers(), timeout=15
        )
        print(f"[DB] {r.status_code} {r.text[:400]}")
        return r.json()
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return None


def open_db(db_id):
    try:
        r = requests.post(
            f"{ACCURATE_BASE_URL}/open-db.do",
            headers=accurate_headers(),
            data={"id": db_id}, timeout=15
        )
        print(f"[OPEN DB] {r.status_code} {r.text[:400]}")
        return r.json()
    except Exception as e:
        print(f"[OPEN DB ERROR] {e}")
        return None


def get_invoices(host, session_id, keyword=None, status=None):
    try:
        params = {
            "fields": "number,customerName,dueDate,grandTotal,remainingAmount,status,hasAttachment",
            "sp.pageSize": 20, "sp.page": 1
        }
        if keyword: params["filter.keywords"] = keyword
        if status: params["filter.status"] = status

        h = accurate_headers()
        h["X-Session-ID"] = session_id

        r = requests.get(
            f"https://{host}/api/customer-invoice/list.do",
            headers=h, params=params, timeout=15
        )
        print(f"[INVOICE] {r.status_code} {r.text[:400]}")
        return r.json()
    except Exception as e:
        print(f"[INVOICE ERROR] {e}")
        return None


def get_accurate_data(query):
    db_data = get_db_list()
    if not db_data or not db_data.get("s"):
        err = db_data.get("d") if db_data else "Koneksi gagal"
        return f"❌ DB Error: {err}"

    db = db_data["d"][0]
    db_id = db.get("id")

    session_data = open_db(db_id)
    if not session_data or not session_data.get("s"):
        return f"❌ Open DB Error: {str(session_data)[:200]}"

    session_id = session_data.get("session")
    host = session_data.get("host")
    if not session_id or not host:
        return f"❌ Session invalid: {str(session_data)[:200]}"

    q = query.lower()

    if any(w in q for w in ["belum lunas", "belum bayar", "outstanding", "jatuh tempo"]):
        data = get_invoices(host, session_id, status="OPEN")
        if data and data.get("s"):
            invoices = data.get("d", [])
            if not invoices:
                return "✅ Tidak ada invoice belum lunas."
            result = f"📋 *Invoice Belum Lunas ({len(invoices)}):*\n\n"
            for inv in invoices[:10]:
                result += f"• *{inv.get('number','-')}* — {inv.get('customerName','-')}\n"
                result += f"  💰 Sisa: Rp {inv.get('remainingAmount',0):,.0f}\n"
                result += f"  📅 {inv.get('dueDate','-')} | 📎 {'✅' if inv.get('hasAttachment') else '❌'}\n\n"
            return result
        return f"❌ {str(data)[:200]}"

    elif any(w in q for w in ["rekap", "semua", "daftar", "list", "total", "omset"]):
        data = get_invoices(host, session_id)
        if data and data.get("s"):
            invoices = data.get("d", [])
            o = sum(1 for i in invoices if i.get("status") == "OPEN")
            p = sum(1 for i in invoices if i.get("status") == "PAID")
            pt = sum(1 for i in invoices if i.get("status") == "PARTIAL")
            return (f"📊 *Rekap Invoice Print Master:*\n\n"
                    f"✅ Lunas: {p}\n⚠️ Sebagian: {pt}\n❌ Belum: {o}\n📋 Total: {len(invoices)}")
        return f"❌ {str(data)[:200]}"

    else:
        data = get_invoices(host, session_id, keyword=query)
        if data and data.get("s"):
            invoices = data.get("d", [])
            if not invoices:
                return f"🔍 Tidak ada invoice: *{query}*"
            result = f"🔍 *Hasil '{query}':*\n\n"
            sm = {"OPEN": "❌ Belum", "PAID": "✅ Lunas", "PARTIAL": "⚠️ Sebagian", "VOID": "🚫 Batal"}
            for inv in invoices[:5]:
                result += f"📄 *{inv.get('number','-')}* — {inv.get('customerName','-')}\n"
                result += f"  💰 Rp {inv.get('grandTotal',0):,.0f} | {sm.get(inv.get('status',''),'-')}\n"
                result += f"  📅 {inv.get('dueDate','-')} | 📎 {'✅' if inv.get('hasAttachment') else '❌'}\n\n"
            return result
        return f"❌ {str(data)[:200]}"


def ask_claude(chat_id, user_message, accurate_data=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    content = f"Data Accurate:\n{accurate_data}\n\nPertanyaan: {user_message}" if accurate_data else user_message
    conversation_history[chat_id].append({"role": "user", "content": content})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 1024, "system": SYSTEM_PROMPT, "messages": conversation_history[chat_id]}
    )
    reply = r.json()["content"][0]["text"]
    conversation_history[chat_id].append({"role": "assistant", "content": reply})
    return reply


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if "message" not in data: return "ok", 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    if "text" not in message:
        send_message(chat_id, "Maaf, hanya bisa proses teks.")
        return "ok", 200

    user_text = message["text"]
    user_name = message["from"].get("first_name", "")

    if user_text == "/start":
        send_message(chat_id, f"Halo {user_name}! 👋\n\nSaya *Accurate Checker Bot* Print Master\n\nContoh:\n• _\"Cek invoice belum lunas\"_\n• _\"Rekap semua invoice\"_\n• _\"Cari invoice PT Maju\"_")
        return "ok", 200

    if user_text == "/reset":
        conversation_history[chat_id] = []
        send_message(chat_id, "✅ Reset!")
        return "ok", 200

    requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})

    try:
        accurate_data = get_accurate_data(user_text)
        reply = ask_claude(chat_id, user_text, accurate_data)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, f"⚠️ Error: {str(e)[:100]}")
        print(f"[ERROR] {e}")

    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot ✅", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
