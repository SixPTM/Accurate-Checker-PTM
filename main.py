import os
import json
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ACCURATE_API_TOKEN = os.environ.get("ACCURATE_API_TOKEN")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ACCURATE_BASE_URL = "https://account.accurate.id/api"

conversation_history = {}

SYSTEM_PROMPT = """Kamu adalah asisten keuangan untuk perusahaan Print Master yang membantu tim admin mengecek invoice, hutang piutang, dan kinerja admin melalui Accurate Online.

Kamu berbicara dalam Bahasa Indonesia yang ramah dan profesional.

Ketika kamu mendapat data dari Accurate Online, analisa dan jelaskan dengan jelas dan ringkas. Format angka dalam Rupiah dengan pemisah titik (contoh: Rp 1.500.000).

Untuk status invoice:
- OPEN = Belum dibayar
- PARTIAL = Dibayar sebagian  
- PAID = Lunas
- VOID = Dibatalkan

Selalu jawab singkat, padat, dan mudah dimengerti. Gunakan emoji yang relevan untuk memperjelas informasi."""


def send_message(chat_id, text):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)


def accurate_headers():
    return {
        "Authorization": f"Bearer {ACCURATE_API_TOKEN}",
        "Content-Type": "application/json"
    }


def get_db_list():
    try:
        r = requests.get(
            f"{ACCURATE_BASE_URL}/db-list.do?fields=id,alias,host",
            headers=accurate_headers(),
            timeout=15
        )
        print(f"[DB LIST] status={r.status_code} body={r.text[:300]}")
        return r.json()
    except Exception as e:
        print(f"[DB LIST ERROR] {e}")
        return None


def open_db(db_id):
    try:
        r = requests.post(
            f"{ACCURATE_BASE_URL}/open-db.do",
            headers=accurate_headers(),
            data={"id": db_id},
            timeout=15
        )
        print(f"[OPEN DB] status={r.status_code} body={r.text[:300]}")
        return r.json()
    except Exception as e:
        print(f"[OPEN DB ERROR] {e}")
        return None


def get_invoices(host, session_id, keyword=None, status=None):
    try:
        params = {
            "fields": "number,customerName,dueDate,grandTotal,remainingAmount,status,hasAttachment",
            "sp.pageSize": 20,
            "sp.page": 1
        }
        if keyword:
            params["filter.keywords"] = keyword
        if status:
            params["filter.status"] = status

        headers = accurate_headers()
        headers["X-Session-ID"] = session_id

        r = requests.get(
            f"https://{host}/api/customer-invoice/list.do",
            headers=headers,
            params=params,
            timeout=15
        )
        print(f"[INVOICE] status={r.status_code} body={r.text[:300]}")
        return r.json()
    except Exception as e:
        print(f"[INVOICE ERROR] {e}")
        return None


def get_accurate_data(query):
    # Step 1: Get DB list
    db_data = get_db_list()
    if not db_data:
        return "❌ Gagal koneksi ke Accurate API."
    
    if not db_data.get("s"):
        return f"❌ Accurate error: {db_data.get('d', 'Unknown error')}"

    db_list = db_data.get("d", [])
    if not db_list:
        return "❌ Tidak ada database ditemukan."

    db = db_list[0]
    db_id = db.get("id")

    # Step 2: Open DB
    session_data = open_db(db_id)
    if not session_data or not session_data.get("s"):
        return f"❌ Gagal buka database: {str(session_data)[:200]}"

    session_id = session_data.get("session")
    host = session_data.get("host")

    if not session_id or not host:
        return f"❌ Session tidak valid: {str(session_data)[:200]}"

    # Step 3: Query invoice
    query_lower = query.lower()

    if any(w in query_lower for w in ["belum lunas", "belum bayar", "outstanding", "jatuh tempo", "unpaid"]):
        data = get_invoices(host, session_id, status="OPEN")
        if data and data.get("s") and "d" in data:
            invoices = data["d"]
            if not invoices:
                return "✅ Tidak ada invoice yang belum lunas saat ini."
            result = f"📋 *Invoice Belum Lunas ({len(invoices)} invoice):*\n\n"
            for inv in invoices[:10]:
                result += f"• *{inv.get('number', '-')}*\n"
                result += f"  👤 {inv.get('customerName', '-')}\n"
                result += f"  💰 Sisa: Rp {inv.get('remainingAmount', 0):,.0f}\n"
                result += f"  📅 Jatuh tempo: {inv.get('dueDate', '-')}\n"
                result += f"  📎 Bukti bayar: {'Ada ✅' if inv.get('hasAttachment') else 'Tidak ada ❌'}\n\n"
            return result
        return f"❌ Gagal ambil invoice: {str(data)[:200]}"

    elif any(w in query_lower for w in ["rekap", "semua", "daftar", "list", "total", "omset"]):
        data = get_invoices(host, session_id)
        if data and data.get("s") and "d" in data:
            invoices = data["d"]
            if not invoices:
                return "📋 Tidak ada invoice ditemukan."
            total_open = sum(1 for i in invoices if i.get("status") == "OPEN")
            total_paid = sum(1 for i in invoices if i.get("status") == "PAID")
            total_partial = sum(1 for i in invoices if i.get("status") == "PARTIAL")
            result = f"📊 *Rekap Invoice Print Master:*\n\n"
            result += f"✅ Lunas: {total_paid} invoice\n"
            result += f"⚠️ Sebagian: {total_partial} invoice\n"
            result += f"❌ Belum lunas: {total_open} invoice\n"
            result += f"📋 Total: {len(invoices)} invoice\n"
            return result
        return f"❌ Gagal ambil data: {str(data)[:200]}"

    else:
        data = get_invoices(host, session_id, keyword=query)
        if data and data.get("s") and "d" in data:
            invoices = data["d"]
            if not invoices:
                return f"🔍 Tidak ditemukan invoice: *{query}*"
            result = f"🔍 *Hasil pencarian '{query}':*\n\n"
            for inv in invoices[:5]:
                status_map = {"OPEN": "❌ Belum Lunas", "PAID": "✅ Lunas", "PARTIAL": "⚠️ Sebagian", "VOID": "🚫 Batal"}
                status = status_map.get(inv.get("status", ""), inv.get("status", "-"))
                result += f"📄 *{inv.get('number', '-')}*\n"
                result += f"  👤 {inv.get('customerName', '-')}\n"
                result += f"  💰 Total: Rp {inv.get('grandTotal', 0):,.0f}\n"
                result += f"  💳 Sisa: Rp {inv.get('remainingAmount', 0):,.0f}\n"
                result += f"  📅 Jatuh tempo: {inv.get('dueDate', '-')}\n"
                result += f"  📌 Status: {status}\n"
                result += f"  📎 Bukti bayar: {'Ada ✅' if inv.get('hasAttachment') else 'Tidak ada ❌'}\n\n"
            return result
        return f"❌ Gagal cari invoice: {str(data)[:200]}"


def ask_claude(chat_id, user_message, accurate_data=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    content = user_message
    if accurate_data:
        content = f"Data dari Accurate Online:\n{accurate_data}\n\nPertanyaan user: {user_message}"

    conversation_history[chat_id].append({"role": "user", "content": content})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": conversation_history[chat_id]
        }
    )
    reply = r.json()["content"][0]["text"]
    conversation_history[chat_id].append({"role": "assistant", "content": reply})
    return reply


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if "message" not in data:
        return "ok", 200

    message = data["message"]
    chat_id = message["chat"]["id"]

    if "text" not in message:
        send_message(chat_id, "Maaf, saya hanya bisa memproses pesan teks.")
        return "ok", 200

    user_text = message["text"]
    user_name = message["from"].get("first_name", "")

    if user_text == "/start":
        send_message(chat_id,
            f"Halo {user_name}! 👋\n\n"
            "Saya *Accurate Checker Bot* untuk *Print Master*\n\n"
            "Yang bisa saya bantu:\n"
            "✅ Cek status invoice tertentu\n"
            "✅ Cek invoice belum lunas\n"
            "✅ Rekap semua invoice\n"
            "✅ Cek bukti bayar sudah ada atau belum\n\n"
            "Contoh pertanyaan:\n"
            "• _\"Cek invoice belum lunas\"_\n"
            "• _\"Rekap semua invoice\"_\n"
            "• _\"Cari invoice PT Maju\"_"
        )
        return "ok", 200

    if user_text == "/reset":
        conversation_history[chat_id] = []
        send_message(chat_id, "✅ Percakapan direset!")
        return "ok", 200

    requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})

    try:
        accurate_data = get_accurate_data(user_text)
        reply = ask_claude(chat_id, user_text, accurate_data)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, f"⚠️ Error: {str(e)[:100]}")
        print(f"[MAIN ERROR] {e}")

    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot - Print Master ✅", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
