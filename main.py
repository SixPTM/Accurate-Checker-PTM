import os
import hmac
import hashlib
import base64
import datetime
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ACCURATE_API_TOKEN = os.environ.get("ACCURATE_API_TOKEN")
ACCURATE_SIGNATURE_SECRET = os.environ.get("ACCURATE_SIGNATURE_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ACCURATE_BASE_URL = "https://account.accurate.id/api"

conversation_history = {}

SYSTEM_PROMPT = """Kamu adalah asisten keuangan dan operasional untuk perusahaan Print Master yang membantu tim admin mengecek invoice, hutang piutang, kinerja admin, dan data produk melalui Accurate Online.
Kamu berbicara dalam Bahasa Indonesia yang ramah dan profesional.
Format angka dalam Rupiah (contoh: Rp 1.500.000).
Status invoice: statusName Lunas=sudah bayar, Belum Lunas=belum bayar.
Jawab singkat, padat, gunakan emoji.
Kamu BISA membantu:
- Cek invoice dan status pembayaran
- Rekap penjualan dan piutang
- Harga beli, harga jual, dan stok produk dari Accurate Online
- Customer yang sering order
Jika data tersedia, analisa dan tampilkan dengan jelas. Jika tidak ada nominal, tetap tampilkan info yang ada."""


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
    if inv.get("_customerName") and inv["_customerName"] != "-":
        return inv["_customerName"]
    for field in ["retailWpName", "customerName", "toName"]:
        val = inv.get(field)
        if val and str(val).strip() and str(val).strip() != "-":
            return str(val).strip()
    customer = inv.get("customer")
    if isinstance(customer, dict):
        name = customer.get("name") or customer.get("customerName")
        if name and str(name).strip():
            return str(name).strip()
    if isinstance(customer, str) and customer.strip():
        return customer.strip()
    return "-"


def extract_grand_total(inv):
    if inv.get("_totalAmount"):
        return float(inv["_totalAmount"])
    for field in ["totalAmount", "subTotal", "salesAmount"]:
        val = inv.get(field)
        if val is not None and val != 0:
            return float(val)
    return 0.0


def extract_outstanding(inv):
    if "_outstanding" in inv:
        return float(inv["_outstanding"])
    val = inv.get("outstanding")
    if val is not None and val != 0:
        return float(val)
    return 0.0


def fetch_customer_name(host, inv):
    try:
        if not host.startswith("http"):
            host = f"https://{host}"
        r = requests.get(
            f"{host}/accurate/api/sales-invoice/detail.do",
            headers=accurate_headers(),
            params={"id": inv["id"]},
            timeout=10
        )
        detail = r.json().get("d", {})
        customer = detail.get("customer")
        name = (
            detail.get("retailWpName") or
            detail.get("customerName") or
            detail.get("toName") or
            (customer.get("name") if isinstance(customer, dict) else None) or
            "-"
        )
        inv["_customerName"] = str(name).strip() if name else "-"
        outstanding = detail.get("outstanding")
        inv["_outstanding"] = float(outstanding) if outstanding is not None else 0.0
        total = detail.get("totalAmount") or detail.get("subTotal") or 0
        inv["_totalAmount"] = float(total)
    except Exception as e:
        print(f"[ENRICH ERROR] id={inv.get('id')} {e}")
        inv["_customerName"] = "-"
        inv["_outstanding"] = 0.0
        inv["_totalAmount"] = 0.0
    return inv


def enrich_with_customer_names(host, invoices, max_workers=10):
    print(f"[ENRICH] Fetching {len(invoices)} invoices...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_customer_name, host, inv): inv for inv in invoices}
        for future in as_completed(futures):
            future.result()
    print(f"[ENRICH] Done.")
    return invoices


def get_invoices(host, page_size=50, status=None, date_from=None, date_to=None, keyword=None):
    try:
        if not host.startswith("http"):
            host = f"https://{host}"
        fields = ",".join([
            "id", "number", "transDate", "transDateView",
            "dueDate", "dueDateView", "statusName",
            "retailWpName", "toName", "customerName",
            "totalAmount", "subTotal", "outstanding",
            "attachmentExist", "masterSalesmanName",
            "branchName", "lastPaymentDate", "lastPaymentDateView"
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
        print(f"[INVOICE] {r.status_code} {r.text[:500]}")
        data = r.json()
        if data.get("s") and data.get("d"):
            sample = data["d"][0] if data["d"] else {}
            print(f"[SAMPLE INVOICE] {sample}")
        return data
    except Exception as e:
        print(f"[INVOICE ERROR] {e}")
        return None


def get_invoice_detail(host, invoice_id):
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


def get_accurate_data(query, chat_id=None):
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

    # DEBUG
    if "debug" in q or "field" in q:
        data = get_invoices(host, page_size=1)
        if data and data.get("s") and data.get("d"):
            inv_id = data["d"][0].get("id")
            detail = get_invoice_detail(host, inv_id)
            d_data = detail.get("d", {}) if detail else {}
            return f"Fields tersedia:\n{list(d_data.keys())}\n\nSample:\n{str(d_data)[:800]}"
        return "Gagal ambil data debug."

    # ============================================================
    # URUTAN PENTING: spesifik dulu, baru umum
    # ============================================================

    # 1. Belum lunas hari ini / siapa yang belum bayar hari ini
    is_hari_ini = any(w in q for w in ["hari ini", "tadi", "sekarang"])
    is_belum = any(w in q for w in ["belum lunas", "belum bayar", "belum", "siapa"])
    is_detail_query = any(w in q for w in ["siapa", "nama", "atas nama", "list", "daftar"])

    if is_belum and (is_hari_ini or is_detail_query):
        data = get_invoices(host, page_size=100, date_from=today_str, date_to=today_str, status="OPEN")
        if data and data.get("s"):
            invoices = data.get("d", [])
            sp = data.get("sp", {})
            total_belum = sp.get("rowCount", len(invoices))
            invoices = enrich_with_customer_names(host, invoices, max_workers=10)
            total_nilai = sum(extract_grand_total(i) for i in invoices)
            result = f"Invoice Belum Lunas Hari Ini ({today_str}):\n"
            result += f"Total: {total_belum} invoice | Nilai: Rp {total_nilai:,.0f}\n\n"
            for inv in invoices[:20]:
                nama = extract_customer_name(inv)
                total = extract_grand_total(inv)
                result += f"- {inv.get('number','-')} | {nama} | Rp {total:,.0f}\n"
                result += f"  Tempo: {inv.get('dueDate','-')}\n"
            return result
        return f"Gagal: {str(data)[:200]}"

    # 2. Piutang / belum lunas semua periode (background thread)
    elif any(w in q for w in ["belum lunas", "belum bayar", "piutang", "outstanding", "jatuh tempo", "unpaid"]):
        date_from = None
        date_to = None
        label_period = "Semua Periode"

        if "juni" in q or "june" in q:
            date_from = "01/06/2026"
            date_to = "30/06/2026"
            label_period = "Juni 2026"
        elif "mei" in q or "may" in q:
            date_from = "01/05/2026"
            date_to = "31/05/2026"
            label_period = "Mei 2026"
        elif "bulan ini" in q:
            date_from = month_start
            date_to = today_str
            label_period = "Bulan Ini"

        def hitung_piutang(chat_id, host, date_from, date_to, label_period):
            try:
                total_nilai = 0.0
                total_invoice = 0
                sample_invoices = []
                page = 1
                h = host if host.startswith("http") else f"https://{host}"
                while True:
                    params = {
                        "fields": "id,number,transDate,dueDate,statusName,totalAmount,subTotal,retailWpName",
                        "sp.pageSize": 200,
                        "sp.page": page,
                        "sp.sort": "transDate",
                        "sp.sortOrder": "DESC",
                        "filter.status": "OPEN"
                    }
                    if date_from:
                        params["filter.transDate.op"] = "BETWEEN"
                        params["filter.transDate.val[0]"] = date_from
                        params["filter.transDate.val[1]"] = date_to or date_from
                    r = requests.get(
                        f"{h}/accurate/api/sales-invoice/list.do",
                        headers=accurate_headers(),
                        params=params,
                        timeout=30
                    )
                    data = r.json()
                    if not data.get("s"):
                        break
                    page_invoices = data.get("d", [])
                    sp = data.get("sp", {})
                    if page == 1:
                        total_invoice = sp.get("rowCount", 0)
                        sample_invoices = page_invoices[:5]
                    for inv in page_invoices:
                        val = inv.get("totalAmount") or inv.get("subTotal") or 0
                        total_nilai += float(val)
                    total_pages = sp.get("pageCount", 1)
                    print(f"[BG PIUTANG] {page}/{total_pages} Rp {total_nilai:,.0f}")
                    if page >= total_pages:
                        break
                    page += 1

                result = f"✅ Selesai dihitung!\n\n"
                result += f"Piutang Belum Lunas - {label_period}:\n"
                result += f"Total invoice: {total_invoice}\n"
                result += f"Total nilai: Rp {total_nilai:,.0f}\n"
                result += f"\n⚠️ Nilai adalah total invoice. Jika ada partial payment, angka bisa sedikit berbeda dari Accurate.\n"
                if sample_invoices:
                    result += f"\nContoh invoice:\n"
                    for inv in sample_invoices:
                        nama = inv.get("retailWpName") or "-"
                        total = float(inv.get("totalAmount") or inv.get("subTotal") or 0)
                        result += f"- {inv.get('number','-')} | {nama} | Rp {total:,.0f}\n"
                send_message(chat_id, result)
            except Exception as e:
                send_message(chat_id, f"❌ Gagal hitung piutang: {str(e)[:100]}")
                print(f"[BG PIUTANG ERROR] {e}")

        t = threading.Thread(target=hitung_piutang, args=(chat_id, host, date_from, date_to, label_period))
        t.daemon = True
        t.start()
        return f"⏳ Sedang menghitung piutang {label_period}...\nData ada 27.000+ invoice, butuh waktu 1-2 menit. Saya kirim hasilnya setelah selesai ya!"

    # 3. Penjualan hari ini
    elif any(w in q for w in ["omset hari ini", "penjualan hari ini", "transaksi hari ini", "invoice hari ini"]):
        data = get_invoices(host, page_size=100, date_from=today_str, date_to=today_str)
        if data and data.get("s"):
            invoices = data.get("d", [])
            sp = data.get("sp", {})
            total_all = sp.get("rowCount", len(invoices))
            invoices = enrich_with_customer_names(host, invoices, max_workers=10)
            total = sum(extract_grand_total(inv) for inv in invoices)
            lunas_list = [i for i in invoices if "lunas" in (i.get("statusName") or "").lower() and "belum" not in (i.get("statusName") or "").lower()]
            belum_list = [i for i in invoices if "belum" in (i.get("statusName") or "").lower()]
            result = f"Penjualan Hari Ini ({today_str}):\n\n"
            result += f"Total invoice: {total_all} | Lunas: {len(lunas_list)} | Belum Lunas: {len(belum_list)}\n"
            if total > 0:
                result += f"Total nilai: Rp {total:,.0f}\n"
            if belum_list:
                result += f"\nInvoice Belum Lunas:\n"
                for inv in belum_list:
                    nama = extract_customer_name(inv)
                    result += f"- {inv.get('number','-')} | {nama} | Rp {extract_grand_total(inv):,.0f}\n"
            if lunas_list:
                result += f"\nInvoice Lunas (10 terbaru):\n"
                for inv in lunas_list[:10]:
                    nama = extract_customer_name(inv)
                    result += f"- {inv.get('number','-')} | {nama} | Rp {extract_grand_total(inv):,.0f}\n"
            return result
        return f"Gagal: {str(data)[:200]}"

    # 4. Penjualan bulan ini / Juni / customer sering order
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
            total_all = sp.get("rowCount", len(invoices))
            invoices = enrich_with_customer_names(host, invoices, max_workers=10)
            total_val = sum(extract_grand_total(inv) for inv in invoices)
            customer_count = {}
            customer_total = {}
            for inv in invoices:
                nama = extract_customer_name(inv)
                customer_count[nama] = customer_count.get(nama, 0) + 1
                customer_total[nama] = customer_total.get(nama, 0) + extract_grand_total(inv)
            top_customers = sorted(customer_count.items(), key=lambda x: x[1], reverse=True)[:5]
            result = f"Rekap Penjualan {label}:\n\n"
            result += f"Total invoice: {total_all}\n"
            if total_val > 0:
                result += f"Nilai (100 sample): Rp {total_val:,.0f}\n"
            if top_customers and top_customers[0][0] != "-":
                result += f"\nTop Customer Paling Sering Order:\n"
                for i, (nama, count) in enumerate(top_customers, 1):
                    total_cust = customer_total.get(nama, 0)
                    result += f"{i}. {nama} - {count} invoice"
                    if total_cust > 0:
                        result += f" (Rp {total_cust:,.0f})"
                    result += "\n"
            else:
                result += "\n⚠️ Nama customer belum terbaca.\n"
            return result
        return f"Gagal ambil data: {str(data)[:200]}"

    # 5. Rekap semua
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

    # 6. Cari produk / item
    elif any(w in q for w in ["harga beli", "harga jual", "modal", "hpp", "harga produk", "stok",
                               "produk", "item", "barang", "vinyl", "stiker", "banner", "kertas",
                               "tinta", "quantac", "bahan", "material", "cek harga", "harga"]):
        keyword = query
        for w in ["berapa", "harga", "beli", "jual", "modal", "hpp", "produk", "item", "barang",
                  "stok", "cek", "info", "ok", "tolong", "nya", "itu", "harganya"]:
            keyword = keyword.replace(w, " ").strip()
        keyword = " ".join(keyword.split()) or query  # hapus spasi dobel
        try:
            if not host.startswith("http"):
                host = f"https://{host}"
            r = requests.get(
                f"{host}/accurate/api/item/list.do",
                headers=accurate_headers(),
                params={
                    "fields": "id,no,name,unitPrice,purchasePrice,availableStock,unit,description",
                    "sp.pageSize": 10,
                    "sp.page": 1,
                    "filter.keywords": keyword
                },
                timeout=15
            )
            data = r.json()
            print(f"[ITEM] {r.status_code} {r.text[:500]}")
            if data.get("s") and data.get("d"):
                items = data["d"]
                result = f"Produk '{keyword}':\n\n"
                for item in items[:5]:
                    result += f"📦 {item.get('name', '-')}\n"
                    result += f"   Kode: {item.get('no', '-')}\n"
                    beli = item.get("purchasePrice") or 0
                    jual = item.get("unitPrice") or 0
                    stok = item.get("availableStock")
                    if beli:
                        result += f"   Harga Beli: Rp {float(beli):,.0f}\n"
                    if jual:
                        result += f"   Harga Jual: Rp {float(jual):,.0f}\n"
                    if stok is not None:
                        result += f"   Stok: {stok} {item.get('unit','')}\n"
                    result += "\n"
                return result
            else:
                return f"Produk '{keyword}' tidak ditemukan.\nCoba ketik nama produk lebih spesifik."
        except Exception as e:
            print(f"[ITEM ERROR] {e}")
            return f"Gagal cek produk: {str(e)[:100]}"

    # 7. Cari invoice spesifik
    else:
        data = get_invoices(host, keyword=query, page_size=10)
        if data and data.get("s"):
            invoices = data.get("d", [])
            if not invoices:
                return f"Tidak ada invoice ditemukan untuk: {query}"
            invoices = enrich_with_customer_names(host, invoices, max_workers=10)
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
            "- Penjualan hari ini\n"
            "- Invoice belum lunas hari ini (siapa saja)\n"
            "- Total piutang / belum lunas semua periode\n"
            "- Rekap penjualan bulan Juni\n"
            "- Customer paling sering order\n"
            "- Cek harga / stok produk\n"
            "- Cari invoice per customer"
        )
        return "ok", 200
    if user_text == "/reset":
        conversation_history[chat_id] = []
        send_message(chat_id, "Percakapan direset!")
        return "ok", 200
    requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    try:
        accurate_data = get_accurate_data(user_text, chat_id=chat_id)
        reply = ask_claude(chat_id, user_text, accurate_data)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)[:100]}")
        print(f"[ERROR] {e}")
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot OK", 200


@app.route("/debug-invoice", methods=["GET"])
def debug_invoice():
    try:
        token_info = get_token_info()
        if not token_info or not token_info.get("s"):
            return {"error": "Gagal koneksi Accurate"}, 500
        d = token_info.get("d", {})
        host = d.get("database", d.get("data usaha", {})).get("host", "")
        if not host.startswith("http"):
            host = f"https://{host}"
        r1 = requests.get(
            f"{host}/accurate/api/sales-invoice/list.do",
            headers=accurate_headers(),
            params={"sp.pageSize": 1, "sp.page": 1},
            timeout=15
        )
        list_data = r1.json()
        inv_id = list_data.get("d", [{}])[0].get("id") if list_data.get("d") else None
        if not inv_id:
            return {"error": "Tidak ada invoice"}, 404
        r2 = requests.get(
            f"{host}/accurate/api/sales-invoice/detail.do",
            headers=accurate_headers(),
            params={"id": inv_id},
            timeout=15
        )
        detail = r2.json().get("d", {})
        return {"invoice_id": inv_id, "all_fields": detail}
    except Exception as e:
        return {"error": str(e)}, 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
