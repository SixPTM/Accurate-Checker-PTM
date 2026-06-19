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
Status invoice: statusName Lunas=sudah bayar, Belum Lunas=belum bayar.
Jawab singkat, padat, gunakan emoji.
Jika data tersedia, analisa dan tampilkan dengan jelas. Jika tidak ada nominal, tetap tampilkan info yang ada seperti jumlah invoice dan status."""


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
        return r.json()
    except Exception as e:
        print(f"[TOKEN ERROR] {e}")
        return None


def extract_customer_name(inv):
    """Ambil nama customer dari berbagai kemungkinan struktur field Accurate Online."""
    name = inv.get("customerName")
    if name and str(name).strip() and name != "-":
        return str(name).strip()
    customer = inv.get("customer")
    if isinstance(customer, dict):
        name = customer.get("name") or customer.get("customerName")
        if name and str(name).strip():
            return str(name).strip()
    if isinstance(customer, str) and customer.strip():
        return customer.strip()
    # Coba field alternatif lain
    for field in ["custName", "buyerName", "toName", "name"]:
        val = inv.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return "-"


def extract_grand_total(inv):
    """Ambil grandTotal dari berbagai kemungkinan nama field."""
    for field in ["grandTotal", "total", "totalAmount", "amount", "grandTotalOrigCurr"]:
        val = inv.get(field)
        if val:
            return val
    return 0


def get_invoices(host, page_size=50, status=None, date_from=None, date_to=None, keyword=None):
    try:
        if not host.startswith("http"):
            host = f"https://{host}"

        # Coba berbagai kemungkinan nama field customer di Accurate Online
        fields = ",".join([
            "id", "number", "transDate", "dueDate", "statusName",
            "customerName", "custName", "customer.name", "customer",
            "grandTotal", "totalAmount", "remainingAmount", "hasAttachment"
        ])

        params = {
            "fields": fields,
            "sp.pageSize": page_size,
            "sp.page": 1,
            "sp.sort": "transDate",
            "sp.sortOrder": "DESC"
        }
        if status:
            params["filter.status"] = status
        if date_from:
            params["filter.transDate.op"] = "BETWEEN"
            params["filter.transDate.val[0]"] = date_from
            params["filter.transDate.val[1]"] = date_to or date_from
        if keyword:
            params["filter.keywords"] = keyword

        r = requests.get(
            f"{host}/accurate/api/sales-invoice/list.do",
            headers=accurate_headers(),
            params=params,
            timeout=15,
            allow_redirects=True
        )
        print(f"[INVOICE] {r.status_code} {r.text[:1000]}")
        data = r.json()

        if data.get("s") and data.get("d"):
            sample = data["d"][0] if data["d"] else {}
            print(f"[FIELDS AVAILABLE] {list(sample.keys())}")
            print(f"[SAMPLE INVOICE] {sample}")
        return data
    except Exception as e:
        print(f"[INVOICE ERROR] {e}")
        return None


def get_invoice_detail(host, invoice_id):
    """Ambil detail satu invoice untuk cek field yang tersedia."""
    try:
        if not host.startswith("http"):
            host = f"https://{host}"
        r = requests.get(
            f"{host}/accurate/api/sales-invoice/detail.do",
            headers=accurate_headers(),
            params={"id": invoice_id},
            timeout=15
        )
        data = r.json()
        print(f"[DETAIL FIELDS] {list(data.get('d', {}).keys())}")
        print(f"[DETAIL SAMPLE] {str(data.get('d', {}))[:500]}")
        return data
    except Exception as e:
        print(f"[DETAIL ERROR] {e}")
        return None


def get_accurate_data(query):
    token_info = get_token_info()
    if not token_info or not token_info.get("s"):
        return "Gagal koneksi ke Accurate."

    d = token_info.get("d", {})
    host = d.get("database", d.get("data usaha", {})).get("host", "")
    if not host:
        return "Host tidak ditemukan."

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    today_str = now.strftime("%d/%m/%Y")
    month_start = now.replace(day=1).strftime("%d/%m/%Y")
    q = query.lower()

    # DEBUG: cek field detail invoice pertama
    if "debug" in q or "field" in q:
        data = get_invoices(host, page_size=1)
        if data and data.get("s") and data.get("d"):
            inv_id = data["d"][0].get("id")
            detail = get_invoice_detail(host, inv_id)
            d_data = detail.get("d", {}) if detail else {}
            return f"Fields tersedia di detail invoice:\n{list(d_data.keys())}\n\nSample:\n{str(d_data)[:800]}"
        return "Gagal ambil data debug."

    # Cek invoice belum lunas / piutang
    if any(w in q for w in ["belum lunas", "belum bayar", "piutang", "outstanding", "jatuh tempo", "unpaid"]):
        data = get_invoices(host, page_size=50, status="OPEN")
        if data and data.get("s"):
            invoices = data.get("d", [])
            sp = data.get("sp", {})
            total_all = sp.get("rowCount", len(invoices))
            if not invoices:
                return "Tidak ada invoice belum lunas."
            result = f"Invoice Belum Lunas (menampilkan {len(invoices)} dari {total_all}):\n\n"
            for inv in invoices[:15]:
                nama = extract_customer_name(inv)
                sisa = inv.get("remainingAmount") or extract_grand_total(inv)
                result += f"- {inv.get('number','-')} | {nama}\n"
                result += f"  Sisa: Rp {sisa:,.0f} | Tempo: {inv.get('dueDate','-')}\n"
                result += f"  Bukti: {'Ada' if inv.get('hasAttachment') else 'Tidak ada'}\n\n"
            return result
        return f"Gagal: {str(data)[:200]}"

    # Omset / penjualan hari ini
    elif any(w in q for w in ["omset hari ini", "penjualan hari ini", "transaksi hari ini", "invoice hari ini"]):
        data = get_invoices(host, page_size=100, date_from=today_str, date_to=today_str)
        if data and data.get("s"):
            invoices = data.get("d", [])
            total = sum(extract_grand_total(inv) for inv in invoices)
            lunas = sum(1 for i in invoices if "lunas" in (i.get("statusName") or "").lower() and "belum" not in (i.get("statusName") or "").lower())
            belum = sum(1 for i in invoices if "belum" in (i.get("statusName") or "").lower())
            result = f"Penjualan Hari Ini ({today_str}):\n\n"
            result += f"Jumlah invoice: {len(invoices)}\n"
            result += f"Lunas: {lunas} | Belum: {belum}\n"
            if total > 0:
                result += f"Total nilai: Rp {total:,.0f}\n"
            if invoices:
                result += "\nDetail:\n"
                for inv in invoices[:10]:
                    nama = extract_customer_name(inv)
                    result += f"- {inv.get('number','-')} | {nama} | {inv.get('statusName','-')}\n"
            return result
        return f"Gagal: {str(data)[:200]}"

    # Omset / penjualan bulan ini atau Juni
    elif any(w in q for w in ["omset", "penjualan", "bulan ini", "bulan juni", "juni", "customer", "sering order"]):
        if "juni" in q or "june" in q:
            date_from = "01/06/2026"
            date_to = "30/06/2026"
            label = "Juni 2026"
        else:
            date_from = month_start
            date_to = today_str
            label = "Bulan Ini"
        data = get_invoices(host, page_size=100, date_from=date_from, date_to=date_to)
        if data and data.get("s"):
            invoices = data.get("d", [])
            sp = data.get("sp", {})
            total_val = sum(extract_grand_total(inv) for inv in invoices)

            customer_count = {}
            customer_total = {}
            for inv in invoices:
                nama = extract_customer_name(inv)
                customer_count[nama] = customer_count.get(nama, 0) + 1
                customer_total[nama] = customer_total.get(nama, 0) + extract_grand_total(inv)

            top_customers = sorted(customer_count.items(), key=lambda x: x[1], reverse=True)[:5]

            result = f"Rekap Penjualan {label}:\n\n"
            result += f"Total invoice: {len(invoices)} (dari {sp.get('rowCount', '?')} total)\n"
            if total_val > 0:
                result += f"Total nilai: Rp {total_val:,.0f}\n"

            if top_customers and top_customers[0][0] != "-":
                result += f"\nTop Customer Paling Sering Order:\n"
                for i, (nama, count) in enumerate(top_customers, 1):
                    total_cust = customer_total.get(nama, 0)
                    result += f"{i}. {nama} - {count} invoice"
                    if total_cust > 0:
                        result += f" (Rp {total_cust:,.0f})"
                    result += "\n"
            else:
                result += "\n⚠️ Nama customer belum terbaca. Kirim pesan 'debug field' untuk cek.\n"
            return result
        return f"Gagal: {str(data)[:200]}"

    # Rekap semua
    elif any(w in q for w in ["rekap", "semua", "daftar", "list", "total"]):
        data = get_invoices(host, page_size=50)
        if data and data.get("s"):
            invoices = data.get("d", [])
            sp = data.get("sp", {})
            o = sum(1 for i in invoices if "belum" in (i.get("statusName") or "").lower())
            p = sum(1 for i in invoices if "lunas" in (i.get("statusName") or "").lower() and "belum" not in (i.get("statusName") or "").lower())
            total_val = sum(extract_grand_total(inv) for inv in invoices)
            result = f"Rekap Invoice Print Master ({len(invoices)} dari {sp.get('rowCount','?')} total):\n\n"
            result += f"Lunas: {p} | Belum Lunas: {o}\n"
            if total_val > 0:
                result += f"Total nilai: Rp {total_val:,.0f}\n"
            return result
        return f"Gagal: {str(data)[:200]}"

    # Cari invoice spesifik
    else:
        data = get_invoices(host, keyword=query, page_size=10)
        if data and data.get("s"):
            invoices = data.get("d", [])
            if not invoices:
                return f"Tidak ada invoice ditemukan untuk: {query}"
            result = f"Hasil pencarian '{query}':\n\n"
            for inv in invoices[:5]:
                nama = extract_customer_name(inv)
                total = extract_grand_total(inv)
                result += f"- {inv.get('number','-')} | {nama}\n"
                result += f"  Total: Rp {total:,.0f} | {inv.get('statusName','-')}\n"
                result += f"  Tanggal: {inv.get('transDate','-')}\n\n"
            return result
        return f"Tidak ditemukan: {query}"


def ask_claude(chat_id, user_message, accurate_data=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    content = f"Data dari Accurate Online:\n{accurate_data}\n\nPertanyaan user: {user_message}" if accurate_data else user_message
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
        send_message(chat_id,
            f"Halo {user_name}! Saya Accurate Checker Bot Print Master.\n\n"
            "Yang bisa saya bantu:\n"
            "- Cek invoice belum lunas\n"
            "- Omset / penjualan hari ini\n"
            "- Rekap penjualan bulan Juni\n"
            "- Customer paling sering order bulan ini\n"
            "- Cari invoice per customer\n"
            "- Rekap semua invoice"
        )
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
