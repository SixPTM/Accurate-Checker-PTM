import os
import hmac
import hashlib
import base64
import datetime
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


def make_timestamp():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    return now.strftime("%d/%m/%Y %H:%M:%S")


def make_signature(timestamp):
    sig = hmac.new(
        ACCURATE_SIGNATURE_SECRET.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return base64.b64encode(sig).decode("utf-8")


def accurate_headers():
    timestamp = make_timestamp()
    signature = make_signature(timestamp)
    return {
        "Authorization": f"Bearer {ACCURATE_API_TOKEN}",
        "X-Api-Timestamp": timestamp,
        "X-Api-Signature": signature,
        "Content-Type": "application/json"
    }


def get_token_info():
    try:
        r = requests.post(
            f"{ACCURATE_BASE_URL}/api-token.do",
            headers=accurate_headers(),
            timeout=15
        )
        print(f"[TOKEN INFO] {r.status_code} {r.text[:500]}")
        return r.json()
    except Exception as e:
        print(f"[TOKEN INFO ERROR] {e}")
        return None


def get_invoices(host, keyword=None, status=None):
    try:
        params = {
            
            "sp.pageSize": 20,
            "sp.page": 1
        }
        if keyword:
            params["filter.keywords"] = keyword
        if status:
            params["filter.status"] = status

        if not host.startswith("http"):
            host = f"https://{host}"

        r = requests.get(
            f"{host}/accurate/api/sales-invoice/list.do",
            headers=accurate_headers(),
            params=params,
            timeout=15,
            allow_redirects=True
        )
        print(f"[INVOICE] {r.status_code} {r.text[:500]}")
        return r.json()
    except Exception as e:
        print(f"[INVOICE ERROR] {e}")
        return None


def get_accurate_data(query):
    token_info = get_token_info()
    if not token_info or not token_info.get("s"):
        err = token_info.get("d", "Koneksi gagal") if token_info else "Koneksi gagal"
        return f"Token Error: {err}"

    d = token_info.get("d", {})
    host = d.get("database", d.get("data usaha", {})).get("host", "")
    if not host:
        return f"Host tidak ditemukan: {str(token_info)[:200]}"

    print(f"[HOST] {host}")

    q = query.lower()

    if any(w in q for w in ["belum lunas", "belum bayar", "outstanding", "jatuh tempo", "unpaid"]):
        data = get_invoices(host, status="OPEN")
        if data and data.get("s"):
            invoices = data.get("d", [])
            if not invoices:
                return "Tidak ada invoice belum lunas."
            result = f"Invoice Belum Lunas ({len(invoices)}):\n\n"
            for inv in invoices[:10]:
                result += f"- {inv.get('number','-')} | {inv.get('customer.name', inv.get('customerName','-'))}\n"
                result += f"  Sisa: Rp {inv.get('remainingAmount',0):,.0f}\n"
                result += f"  Jatuh tempo: {inv.get('dueDate','-')}\n"
                result += f"  Bukti bayar: {'Ada' if inv.get('hasAttachment') else 'Tidak ada'}\n\n"
            return result
        return f"Gagal ambil data: {str(data)[:200]}"

    elif any(w in q for w in ["rekap", "semua", "daftar", "list", "total", "omset", "hari ini", "transaksi"]):
        data = get_invoices(host)
        if data and data.get("s"):
            invoices = data.get("d", [])
            o = sum(1 for i in invoices if i.get("statusName") in ["Belum Lunas", "Open", "OPEN"] or i.get("status") == "OPEN")
            p = sum(1 for i in invoices if i.get("statusName") in ["Lunas", "Paid", "PAID"] or i.get("status") == "PAID")
            pt = sum(1 for i in invoices if i.get("statusName") in ["Sebagian", "Partial", "PARTIAL"] or i.get("status") == "PARTIAL")
            total_nilai = sum(inv.get("grandTotal", 0) or 0 for inv in invoices)
            result = f"Rekap Invoice Print Master:\n\n"
            result += f"Total invoice: {len(invoices)}\n"
            result += f"Lunas: {p}\nSebagian: {pt}\nBelum Lunas: {o}\n"
            if total_nilai > 0:
                result += f"Total nilai: Rp {total_nilai:,.0f}\n"
            result += f"\nStatus yang ditemukan: {set(i.get('statusName','?') for i in invoices[:5])}"
            return result
        return f"Gagal ambil data: {str(data)[:200]}"

    else:
        data = get_invoices(host, keyword=query)
        if data and data.get("s"):
            invoices = data.get("d", [])
            if not invoices:
                return f"Tidak ada invoice: {query}"
            result = f"Hasil pencarian '{query}':\n\n"
            sm = {"OPEN": "Belum Lunas", "PAID": "Lunas", "PARTIAL": "Sebagian", "VOID": "Batal"}
            for inv in invoices[:5]:
                result += f"- {inv.get('number','-')} | {inv.get('customerName','-')}\n"
                result += f"  Total: Rp {inv.get('grandTotal',0):,.0f} | {sm.get(inv.get('status',''),'-')}\n"
                result += f"  Jatuh tempo: {inv.get('dueDate','-')}\n\n"
            return result
        return f"Gagal cari invoice: {str(data)[:200]}"


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
    if "message" not in data:
        return "ok", 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    if "text" not in message:
        send_message(chat_id, "Maaf, hanya bisa proses teks.")
        return "ok", 200

    user_text = message["text"]
    user_name = message["from"].get("first_name", "")

    if user_text == "/start":
        send_message(chat_id, f"Halo {user_name}! Saya Accurate Checker Bot Print Master.\n\nContoh pertanyaan:\n- Cek invoice belum lunas\n- Rekap semua invoice\n- Cari invoice PT Maju")
        return "ok", 200

    if user_text == "/reset":
        conversation_history[chat_id] = []
        send_message(chat_id, "Percakapan direset!")
        return "ok", 200

    requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})

    try:
        accurate_data = get_accurate_data(user_text)
        reply = ask_claude(chat_id, user_text, accurate_data)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)[:100]}")
        print(f"[ERROR] {e}")

    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
