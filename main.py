import os
import json
import hmac
import hashlib
import time
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ACCURATE_API_TOKEN = os.environ.get("ACCURATE_API_TOKEN")
ACCURATE_SIGNATURE_SECRET = os.environ.get("ACCURATE_SIGNATURE_SECRET")

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


def generate_signature(timestamp):
    """Generate X-Api-Signature untuk Accurate API"""
    message = f"{ACCURATE_API_TOKEN}{timestamp}"
    signature = hmac.new(
        ACCURATE_SIGNATURE_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature


def get_accurate_headers():
    """Headers dengan signature untuk Accurate API"""
    timestamp = str(int(time.time() * 1000))
    signature = generate_signature(timestamp)
    return {
        "Authorization": f"Bearer {ACCURATE_API_TOKEN}",
        "X-Api-Signature": signature,
        "X-Api-Timestamp": timestamp,
        "Content-Type": "application/json"
    }


def send_message(chat_id, text):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)


def get_db_info():
    """Ambil database list"""
    try:
        response = requests.get(
            f"{ACCURATE_BASE_URL}/db-list.do?fields=id,alias,host",
            headers=get_accurate_headers(),
            timeout=10
        )
        print(f"DB List status: {response.status_code}")
        print(f"DB List response: {response.text[:500]}")
        
        data = response.json()
        
        if isinstance(data, dict) and "d" in data:
            db_list = data["d"]
            if isinstance(db_list, list) and len(db_list) > 0:
                return db_list[0]
        return None
    except Exception as e:
        print(f"Error get database: {e}")
        return None


def open_db(db_id):
    """Buka database dan dapat session"""
    try:
        response = requests.post(
            f"{ACCURATE_BASE_URL}/open-db.do",
            headers=get_accurate_headers(),
            json={"id": db_id},
            timeout=10
        )
        print(f"Open DB status: {response.status_code}")
        print(f"Open DB response: {response.text[:500]}")
        
        return response.json()
    except Exception as e:
        print(f"Error open database: {e}")
        return None


def get_invoices(host, session_id, keyword=None, status=None):
    """Ambil daftar invoice"""
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

        headers = get_accurate_headers()
        headers["X-Session-ID"] = session_id

        response = requests.get(
            f"https://{host}/api/customer-invoice/list.do",
            headers=headers,
            params=params,
            timeout=10
        )
        print(f"Invoice status: {response.status_code}")
        print(f"Invoice response: {response.text[:500]}")
        return response.json()
    except Exception as e:
        print(f"Error get invoices: {e}")
        return None


def get_accurate_data(query):
    """Fungsi utama ambil data dari Accurate"""
    db_info = get_db_info()
    if not db_info:
        return "❌ Tidak bisa ambil daftar database Accurate."

    db_id = db_info.get("id")
    if not db_id:
        return "❌ Tidak bisa menemukan ID database."

    session_data = open_db(db_id)
    if not session_data:
        return "❌ Tidak bisa membuka database Accurate."

    session_id = session_data.get("session")
    host = session_data.get("host")

    if not session_id or not host:
        return f"❌ Session tidak valid. Response: {str(session_data)[:200]}"

    query_lower = query.lower()

    if any(word in query_lower for word in ["belum lunas", "belum bayar", "outstanding", "jatuh tempo", "unpaid"]):
        data = get_invoices(host, session_id, status="OPEN")
        if data and "d" in data:
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
        return f"❌ Gagal mengambil data. Response: {str(data)[:200]}"

    elif any(word in query_lower for word in ["rekap", "semua", "daftar", "list", "total"]):
        data = get_invoices(host, session_id)
        if data and "d" in data:
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
        return f"❌ Gagal mengambil data. Response: {str(data)[:200]}"

    else:
        data = get_invoices(host, session_id, keyword=query)
        if data and "d" in data:
            invoices = data["d"]
            if not invoices:
                return f"🔍 Tidak ditemukan invoice dengan kata kunci: *{query}*"
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
        return f"❌ Gagal mencari invoice. Response: {str(data)[:200]}"


def ask_claude(chat_id, user_message, accurate_data=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    content = user_message
    if accurate_data:
        content = f"Data dari Accurate Online:\n{accurate_data}\n\nPertanyaan user: {user_message}"

    conversation_history[chat_id].append({"role": "user", "content": content})

    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    response = requests.post(
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

    data = response.json()
    reply = data["content"][0]["text"]
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

    requests.post(f"{TELEGRAM_API}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing"
    })

    try:
        accurate_data = get_accurate_data(user_text)
        reply = ask_claude(chat_id, user_text, accurate_data)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, f"⚠️ Error: {str(e)[:100]}")
        print(f"Error: {e}")

    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot - Print Master ✅", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
