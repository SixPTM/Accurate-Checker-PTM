import os
import hmac
import hashlib
import base64
import datetime
import threading
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ACCURATE_API_TOKEN = os.environ.get("ACCURATE_API_TOKEN")
ACCURATE_SIGNATURE_SECRET = os.environ.get("ACCURATE_SIGNATURE_SECRET", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_B64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")

def _load_google_creds_info():
    """Ambil isi kredensial JSON, dari base64 (diutamakan) atau JSON langsung."""
    if GOOGLE_CREDENTIALS_B64:
        decoded = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
        return json.loads(decoded)
    if GOOGLE_CREDENTIALS_JSON:
        return json.loads(GOOGLE_CREDENTIALS_JSON)
    raise RuntimeError("Kredensial Google belum diset")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ACCURATE_BASE_URL = "https://account.accurate.id/api"

conversation_history = {}

def make_timestamp():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    return now.strftime("%d/%m/%Y %H:%M:%S")

def make_signature(timestamp):
    sig = hmac.new(ACCURATE_SIGNATURE_SECRET.encode("utf-8"), timestamp.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")

def accurate_headers():
    timestamp = make_timestamp()
    return {
        "Authorization": f"Bearer {ACCURATE_API_TOKEN}",
        "X-Api-Timestamp": timestamp,
        "X-Api-Signature": make_signature(timestamp),
        "Content-Type": "application/json"
    }

def get_host():
    try:
        r = requests.post(f"{ACCURATE_BASE_URL}/api-token.do", headers=accurate_headers(), timeout=15)
        d = r.json().get("d", {})
        host = d.get("database", d.get("data usaha", {})).get("host", "")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    except Exception as e:
        print(f"[HOST ERROR] {e}")
        return None

def _send_one(chat_id, text):
    # Coba kirim dengan Markdown dulu
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code == 200:
            return
        print(f"[SEND MARKDOWN FAIL] {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[SEND ERROR markdown] {e}")
    # Kalau Markdown gagal, kirim ulang sebagai teks biasa (tanpa parse_mode)
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10)
        print(f"[SEND PLAIN] status={r.status_code} {r.text[:200] if r.status_code != 200 else 'ok'}")
    except Exception as e:
        print(f"[SEND ERROR plain] {e}")


def send_message(chat_id, text):
    if not text or not text.strip():
        text = "Maaf, tidak ada jawaban yang bisa ditampilkan."
    # Telegram batasi 4096 karakter per pesan. Pecah per baris jika kepanjangan.
    LIMIT = 3800
    if len(text) <= LIMIT:
        _send_one(chat_id, text)
        return
    chunk = ""
    for line in text.split("\n"):
        # Kalau satu baris saja sudah melebihi limit, potong paksa
        while len(line) > LIMIT:
            if chunk:
                _send_one(chat_id, chunk)
                chunk = ""
            _send_one(chat_id, line[:LIMIT])
            line = line[LIMIT:]
        if len(chunk) + len(line) + 1 > LIMIT:
            _send_one(chat_id, chunk)
            chunk = line
        else:
            chunk = chunk + "\n" + line if chunk else line
    if chunk:
        _send_one(chat_id, chunk)

def send_file_to_telegram(chat_id, file_bytes, filename, caption=""):
    try:
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        if ext in ["jpg", "jpeg", "png", "gif"]:
            r = requests.post(f"{TELEGRAM_API}/sendPhoto", files={"photo": (filename, file_bytes)}, data={"chat_id": chat_id, "caption": caption}, timeout=30)
        else:
            r = requests.post(f"{TELEGRAM_API}/sendDocument", files={"document": (filename, file_bytes)}, data={"chat_id": chat_id, "caption": caption}, timeout=30)
        print(f"[SEND FILE] {filename} status={r.status_code}")
    except Exception as e:
        print(f"[SEND FILE ERROR] {e}")

# ============================================================
# ACCURATE API TOOLS
# ============================================================

def tool_get_invoices(host, params):
    try:
        fields = "id,number,transDate,transDateView,dueDate,dueDateView,statusName,retailWpName,totalAmount,subTotal,outstanding,attachmentExist,masterSalesmanName"
        api_params = {
            "fields": fields,
            "sp.pageSize": params.get("page_size", 50),
            "sp.page": 1,
            "sp.sort": "transDate",
            "sp.sortOrder": "DESC"
        }
        keyword = params.get("keyword") or params.get("customer_name")
        status = params.get("status")
        if keyword and status:
            api_params["filter.status"] = status
        elif status:
            api_params["filter.status"] = status
        elif keyword:
            api_params["filter.keywords"] = keyword
        if params.get("date_from"):
            api_params["filter.transDate.op"] = "BETWEEN"
            api_params["filter.transDate.val[0]"] = params["date_from"]
            api_params["filter.transDate.val[1]"] = params.get("date_to", params["date_from"])

        r = requests.get(f"{host}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=api_params, timeout=15)
        data = r.json()

        if keyword and status and data.get("d"):
            kw_lower = keyword.lower()
            data["d"] = [inv for inv in data["d"] if kw_lower in (inv.get("retailWpName") or "").lower()]

        print(f"[TOOL invoices] count={len(data.get('d',[]))} total={data.get('sp',{}).get('rowCount',0)}")

        if data.get("s") and data.get("d"):
            invoices = data["d"]
            def enrich(inv):
                detail = None
                for attempt in range(3):
                    try:
                        r2 = requests.get(f"{host}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=12)
                        d = r2.json()
                        if d.get("s") and d.get("d"):
                            detail = d["d"]
                            break
                    except Exception:
                        pass
                if detail is None:
                    inv["customerName"] = "(gagal baca)"
                    return
                customer = detail.get("customer")
                if isinstance(customer, dict): cname = customer.get("name")
                elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else str(customer[0])
                else: cname = None
                inv["customerName"] = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                inv["outstanding"] = detail.get("primeOwing") or 0
                inv["totalAmount"] = _resolve_nilai_invoice(detail) or inv.get("totalAmount") or 0
            # Enrich semua invoice yang terambil bila jumlahnya wajar (mis. harian/mingguan).
            # Kalau terlalu banyak (>120), enrich 50 pertama saja agar tidak lama/timeout.
            target = invoices if len(invoices) <= 120 else invoices[:50]
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(enrich, target))

        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_invoice_detail(host, invoice_id):
    try:
        r = requests.get(f"{host}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": invoice_id}, timeout=15)
        return json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_items(host, keyword, page_size=20):
    try:
        keyword_lower = keyword.lower()
        keywords = keyword_lower.split()
        matched = []
        page = 1
        while True:
            r = requests.get(f"{host}/accurate/api/item/list.do", headers=accurate_headers(),
                params={"fields": "id,no,name,unitPrice,purchasePrice,availableStock,unit,buyPrice,lastPurchasePrice", "sp.pageSize": 100, "sp.page": page}, timeout=15)
            data = r.json()
            if not data.get("s"): break
            items = data.get("d", [])
            sp = data.get("sp", {})
            for item in items:
                name = (item.get("name") or "").lower()
                no = (item.get("no") or "").lower()
                if all(kw in name or kw in no for kw in keywords):
                    matched.append(item)
            if len(matched) >= page_size or page >= sp.get("pageCount", 1): break
            page += 1

        def enrich_item(item):
            try:
                r2 = requests.get(f"{host}/accurate/api/item/detail.do", headers=accurate_headers(), params={"id": item["id"]}, timeout=10)
                detail = r2.json().get("d", {})
                item["availableStock"] = detail.get("balance") or detail.get("availableStock") or 0
                item["unitPrice"] = detail.get("unitPrice") or item.get("unitPrice") or 0
                item["purchasePrice"] = detail.get("purchasePrice") or item.get("purchasePrice") or 0
                # Harga modal/HPP rata-rata per unit ada di balanceUnitCost (purchasePrice biasanya 0)
                item["hargaModal"] = detail.get("balanceUnitCost") or detail.get("defStandardCost") or 0
                item["unit"] = detail.get("unit") or item.get("unit") or ""
                print(f"[ITEM STOCK] {item['name']} balance={detail.get('balance')} modal={item['hargaModal']}")
            except Exception as e:
                print(f"[ITEM ENRICH ERROR] {e}")

        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(enrich_item, matched[:page_size]))

        print(f"[TOOL items] keyword='{keyword}' matched={len(matched)}")
        if matched: print(f"[TOOL items sample] {matched[0]}")
        return json.dumps({"s": True, "d": matched[:page_size], "sp": {"rowCount": len(matched)}}, ensure_ascii=False)
    except Exception as e:
        print(f"[TOOL items ERROR] {e}")
        return json.dumps({"error": str(e)})


def tool_get_attachment(host, chat_id, invoice_number):
    try:
        h = host if host.startswith("http") else f"https://{host}"
        r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
            params={"fields": "id,number,attachmentExist,statusName,retailWpName", "sp.pageSize": 10, "filter.keywords": invoice_number}, timeout=15)
        invoices = r.json().get("d", [])
        if not invoices: return json.dumps({"error": f"Invoice {invoice_number} tidak ditemukan"})
        inv = invoices[0]
        if not inv.get("attachmentExist"): return json.dumps({"error": f"Invoice {invoice_number} tidak memiliki attachment"})

        r3 = requests.get(f"{h}/accurate/api/attachment/list.do", headers=accurate_headers(),
            params={"transactionId": inv["id"], "transactionType": "SALES_INVOICE"}, timeout=15)
        att_data = r3.json()
        print(f"[ATTACHMENT LIST] {r3.status_code} {r3.text[:300]}")
        attachments = [a for a in att_data.get("d", []) if isinstance(a, dict)]

        if not attachments:
            r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=15)
            detail = r2.json().get("d", {})
            attachments = [a for a in (detail.get("attachments") or detail.get("attachment") or []) if isinstance(a, dict)]

        if not attachments: return json.dumps({"error": "Attachment tidak bisa diambil via API. Cek langsung di Accurate Online."})

        sent = 0
        for att in attachments[:3]:
            att_id = att.get("id") or att.get("attachmentId")
            att_name = att.get("name") or att.get("fileName") or f"bukti_{invoice_number}"
            dl = requests.get(f"{h}/accurate/api/attachment/download.do", headers=accurate_headers(), params={"id": att_id}, timeout=30)
            if dl.status_code == 200 and dl.content:
                send_file_to_telegram(chat_id, dl.content, att_name, f"Bukti bayar {invoice_number}")
                sent += 1
        if sent > 0: return json.dumps({"success": True, "sent": sent})
        return json.dumps({"error": "Gagal download attachment"})
    except Exception as e:
        print(f"[ATTACHMENT ERROR] {e}")
        return json.dumps({"error": str(e)})


def tool_get_sales_per_item(host, chat_id, keyword, date_from, date_to):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            kw_lower = keyword.lower()
            lock = threading.Lock()
            item_qty = {}
            item_total = {}
            found_invoices = []

            all_ids = []
            page = 1
            while True:
                params = {"fields": "id,number", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_ids.extend(data.get("d", []))
                sp = data.get("sp", {})
                print(f"[SALES ITEM BG] page {page}/{sp.get('pageCount',1)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            def scan_invoice(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    items = detail.get("detailItem", [])
                    if not isinstance(items, list): return
                    for item in items:
                        if not isinstance(item, dict): continue
                        item_obj = item.get("item", {})
                        if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                        name = item.get("itemName") or (item_obj.get("name") if isinstance(item_obj, dict) else None) or "-"
                        if kw_lower in name.lower():
                            qty = float(item.get("quantity") or item.get("qty") or 0)
                            amount = float(item.get("amount") or item.get("totalAmount") or item.get("unitPrice", 0) * qty or 0)
                            print(f"[ITEM FOUND] {name} qty={qty} amount={amount}")
                            with lock:
                                item_qty[name] = item_qty.get(name, 0) + qty
                                item_total[name] = item_total.get(name, 0) + amount
                                if inv["number"] not in found_invoices: found_invoices.append(inv["number"])
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(scan_invoice, all_ids))

            if not item_qty:
                send_message(chat_id, f"❌ Produk '{keyword}' tidak ditemukan di invoice {date_from} - {date_to}")
                return

            total_qty = sum(item_qty.values())
            total_val = sum(item_total.values())
            msg = f"✅ *Penjualan '{keyword}' ({date_from} - {date_to})*\n\n"
            msg += f"Total qty: {total_qty:,.0f} pcs | Nilai: Rp {total_val:,.0f}\n"
            msg += f"Ditemukan di: {len(found_invoices)} invoice\n\n*Per varian:*\n"
            for name, qty in sorted(item_qty.items(), key=lambda x: x[1], reverse=True):
                val = item_total.get(name, 0)
                msg += f"• {name}: {qty:,.0f} pcs (Rp {val:,.0f})\n"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Error: {str(e)[:100]}")
            print(f"[SALES ITEM BG ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_top_products_background(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            lock = threading.Lock()
            product_qty = {}
            product_total = {}
            product_invoices = {}

            all_invoices = []
            page = 1
            while True:
                params = {"fields": "id,number", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_invoices.extend(data.get("d", []))
                sp = data.get("sp", {})
                print(f"[TOP PRODUCTS] loading page {page}/{sp.get('pageCount',1)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            send_message(chat_id, f"⏳ Scanning {len(all_invoices)} invoice untuk rekap semua produk {label}...\nEstimasi: 5-10 menit. Hasilnya saya kirim nanti ya!")

            def scan_invoice(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    items = detail.get("detailItem", [])
                    if not isinstance(items, list): return
                    for item in items:
                        if not isinstance(item, dict): continue
                        item_obj = item.get("item", {})
                        if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                        name = item.get("itemName") or (item_obj.get("name") if isinstance(item_obj, dict) else None) or "-"
                        if not name or name == "-": continue
                        qty = float(item.get("quantity") or item.get("qty") or 0)
                        amount = float(item.get("amount") or item.get("totalAmount") or item.get("unitPrice", 0) * qty or 0)
                        with lock:
                            product_qty[name] = product_qty.get(name, 0) + qty
                            product_total[name] = product_total.get(name, 0) + amount
                            product_invoices[name] = product_invoices.get(name, 0) + 1
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(scan_invoice, all_invoices))

            if not product_qty:
                send_message(chat_id, "❌ Tidak ada data produk ditemukan.")
                return

            top = sorted(product_qty.items(), key=lambda x: x[1], reverse=True)[:20]
            msg = f"🏆 *Top 20 Produk Terlaris - {label}*\n_(dari {len(all_invoices)} invoice)_\n\n"
            for i, (name, qty) in enumerate(top, 1):
                val = product_total.get(name, 0)
                inv_count = product_invoices.get(name, 0)
                msg += f"{i}. *{name}*\n   📦 {qty:,.0f} pcs | 🧾 {inv_count} inv"
                if val > 0: msg += f" | 💰 Rp {val:,.0f}"
                msg += "\n"
            send_message(chat_id, msg)

        except Exception as e:
            send_message(chat_id, f"❌ Error: {str(e)[:100]}")
            print(f"[TOP PRODUCTS ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_unpaid_customers_background(host, chat_id, date_from=None, date_to=None, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            lock = threading.Lock()
            customer_data = {}
            all_invoices = []
            page = 1
            total_invoice = 0

            while True:
                params = {"fields": "id,number,totalAmount,subTotal,retailWpName,dueDate,statusName",
                    "sp.pageSize": 200, "sp.page": page, "filter.status": "OPEN"}
                if date_from:
                    params["filter.transDate.op"] = "BETWEEN"
                    params["filter.transDate.val[0]"] = date_from
                    params["filter.transDate.val[1]"] = date_to or date_from
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1: total_invoice = sp.get("rowCount", 0)
                all_invoices.extend(page_data)
                print(f"[BG UNPAID] page {page}/{sp.get('pageCount',1)} loaded={len(all_invoices)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            def enrich(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    owing = float(detail.get("primeOwing") or 0)
                    if owing <= 0:
                        return
                    customer = detail.get("customer")
                    if isinstance(customer, dict): cname = customer.get("name")
                    elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                    else: cname = None
                    name = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                    if name:
                        with lock:
                            if name not in customer_data: customer_data[name] = {"count": 0, "total": 0.0, "outstanding": 0.0}
                            customer_data[name]["count"] += 1
                            customer_data[name]["total"] += owing
                            customer_data[name]["outstanding"] += owing
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(enrich, all_invoices))

            sorted_customers = sorted(customer_data.items(), key=lambda x: x[1]["outstanding"], reverse=True)
            total_outstanding = sum(v["outstanding"] for v in customer_data.values())

            msg = f"✅ *Customer Belum Bayar - {label}*\n\n"
            msg += f"Total invoice OPEN: {total_invoice}\n"
            msg += f"Total sisa tagihan: Rp {total_outstanding:,.0f}\n"
            msg += f"Jumlah customer (masih ada sisa): {len(customer_data)}\n\n*Daftar (urut sisa tagihan terbesar):*\n"
            for name, d in sorted_customers[:30]:
                msg += f"• {name} — {d['count']} inv | Sisa: Rp {d['outstanding']:,.0f}\n"
            if len(sorted_customers) > 30: msg += f"\n_...dan {len(sorted_customers)-30} customer lainnya_"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal: {str(e)[:100]}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_piutang_summary(host, chat_id, date_from=None, date_to=None, label="Semua Periode"):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            lock = threading.Lock()
            total_owing = [0.0]
            count_berisa = [0]
            all_invoices = []
            page = 1
            total_invoice = 0
            while True:
                params = {"fields": "id,number", "sp.pageSize": 200, "sp.page": page, "filter.status": "OPEN"}
                if date_from:
                    params["filter.transDate.op"] = "BETWEEN"
                    params["filter.transDate.val[0]"] = date_from
                    params["filter.transDate.val[1]"] = date_to or date_from
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1: total_invoice = sp.get("rowCount", 0)
                all_invoices.extend(page_data)
                print(f"[BG PIUTANG] list {page}/{sp.get('pageCount',1)} loaded={len(all_invoices)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            def get_owing(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    owing = float(detail.get("primeOwing") or 0)
                    if owing > 0:
                        with lock:
                            total_owing[0] += owing
                            count_berisa[0] += 1
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(get_owing, all_invoices))

            msg = f"✅ *Piutang Belum Lunas - {label}*\n\n"
            msg += f"Total invoice status OPEN: {total_invoice}\n"
            msg += f"Invoice yang masih ada sisa: {count_berisa[0]}\n"
            msg += f"Total sisa tagihan: Rp {total_owing[0]:,.0f}\n\n"
            msg += f"_Sisa tagihan dihitung dari field primeOwing (sisa yang benar-benar belum dibayar), bukan nilai penuh invoice._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal: {str(e)[:100]}")
            print(f"[BG PIUTANG ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_omset_summary(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            total_semua = 0.0
            total_lunas = 0.0
            total_belum = 0.0
            count_semua = 0
            count_lunas = 0
            count_belum = 0
            page = 1
            total_invoice = 0
            while True:
                params = {"fields": "id,totalAmount,salesAmount,subTotal,statusName", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1: total_invoice = sp.get("rowCount", 0)
                for inv in page_data:
                    nilai = float(inv.get("totalAmount") or inv.get("salesAmount") or inv.get("subTotal") or 0)
                    status = (inv.get("statusName") or "").upper()
                    total_semua += nilai
                    count_semua += 1
                    if "LUNAS" in status or "PAID" in status or "CLOSE" in status:
                        total_lunas += nilai
                        count_lunas += 1
                    else:
                        total_belum += nilai
                        count_belum += 1
                print(f"[BG OMSET] {page}/{sp.get('pageCount',1)} total Rp {total_semua:,.0f}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            msg = f"💰 *Total Penjualan - {label}*\n\n"
            msg += f"Total invoice: {count_semua}\n"
            msg += f"Total penjualan: Rp {total_semua:,.0f}\n\n"
            msg += f"✅ Sudah lunas: {count_lunas} inv (Rp {total_lunas:,.0f})\n"
            msg += f"⏳ Belum lunas: {count_belum} inv (Rp {total_belum:,.0f})\n\n"
            msg += f"_⚠️ Nilai adalah total invoice. Jika ada partial payment, angka bisa sedikit berbeda dari Accurate._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung omset: {str(e)[:100]}")
            print(f"[BG OMSET ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_low_stock(host, chat_id, keyword, threshold=30):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            keyword_lower = keyword.lower()
            keywords = keyword_lower.split()
            matched = []
            page = 1
            while True:
                r = requests.get(f"{h}/accurate/api/item/list.do", headers=accurate_headers(),
                    params={"fields": "id,no,name,unitPrice,availableStock,unit", "sp.pageSize": 100, "sp.page": page}, timeout=20)
                data = r.json()
                if not data.get("s"): break
                items = data.get("d", [])
                sp = data.get("sp", {})
                for item in items:
                    name = (item.get("name") or "").lower()
                    no = (item.get("no") or "").lower()
                    if all(kw in name or kw in no for kw in keywords):
                        matched.append(item)
                if page >= sp.get("pageCount", 1): break
                page += 1

            lock = threading.Lock()
            low_items = []

            def check_stock(item):
                try:
                    r2 = requests.get(f"{h}/accurate/api/item/detail.do", headers=accurate_headers(), params={"id": item["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    stock = detail.get("balance")
                    if stock is None: stock = detail.get("availableStock") or 0
                    stock = float(stock)
                    print(f"[LOW STOCK CHECK] {item['name']} stock={stock}")
                    if stock < threshold:
                        with lock:
                            low_items.append({"name": item["name"], "stock": stock})
                except Exception as e:
                    print(f"[LOW STOCK ERROR] {e}")

            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(check_stock, matched))

            if not low_items:
                send_message(chat_id, f"✅ Tidak ada produk '{keyword}' dengan stok di bawah {threshold:.0f} pcs. Semua aman!")
                return

            low_items.sort(key=lambda x: x["stock"])
            msg = f"⚠️ *Stok Menipis '{keyword}' (di bawah {threshold:.0f} pcs)*\n\n"
            msg += f"Ditemukan {len(low_items)} produk:\n\n"
            for it in low_items:
                msg += f"• {it['name']}: {it['stock']:,.0f} pcs\n"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek stok: {str(e)[:100]}")
            print(f"[LOW STOCK BG ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_overdue_customers(host, chat_id, days=30):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            today = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
            cutoff = today - datetime.timedelta(days=days)
            lock = threading.Lock()
            overdue = {}
            all_invoices = []
            page = 1

            while True:
                params = {"fields": "id,number,totalAmount,subTotal,retailWpName,dueDate,statusName",
                    "sp.pageSize": 200, "sp.page": page, "filter.status": "OPEN"}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                all_invoices.extend(page_data)
                print(f"[BG OVERDUE] page {page}/{sp.get('pageCount',1)} loaded={len(all_invoices)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            def parse_date(s):
                if not s: return None
                for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                    try: return datetime.datetime.strptime(s.split(" ")[0], fmt)
                    except: continue
                return None

            def enrich(inv):
                try:
                    due = parse_date(inv.get("dueDate"))
                    if due is None or due > cutoff:
                        return
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    owing = float(detail.get("primeOwing") or 0)
                    if owing <= 0:
                        return
                    customer = detail.get("customer")
                    if isinstance(customer, dict): cname = customer.get("name")
                    elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                    else: cname = None
                    name = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                    hari_lewat = (today - due).days
                    if name:
                        with lock:
                            if name not in overdue: overdue[name] = {"count": 0, "total": 0.0, "max_days": 0}
                            overdue[name]["count"] += 1
                            overdue[name]["total"] += owing
                            overdue[name]["max_days"] = max(overdue[name]["max_days"], hari_lewat)
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(enrich, all_invoices))

            if not overdue:
                send_message(chat_id, f"✅ Tidak ada customer yang menunggak lebih dari {days} hari dari jatuh tempo. Bagus!")
                return

            sorted_cust = sorted(overdue.items(), key=lambda x: x[1]["total"], reverse=True)
            total_nilai = sum(v["total"] for v in overdue.values())
            msg = f"🚨 *Customer Nunggak > {days} Hari (dari jatuh tempo)*\n\n"
            msg += f"Jumlah customer: {len(overdue)}\n"
            msg += f"Total tagihan tertunggak: Rp {total_nilai:,.0f}\n\n"
            msg += f"*Daftar (urut nilai terbesar):*\n"
            for name, d in sorted_cust[:30]:
                msg += f"• {name} — {d['count']} inv | Rp {d['total']:,.0f} | telat {d['max_days']} hari\n"
            if len(sorted_cust) > 30: msg += f"\n_...dan {len(sorted_cust)-30} customer lainnya_"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek tunggakan: {str(e)[:100]}")
            print(f"[BG OVERDUE ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_unpaid_invoices_detail(host, chat_id, date_from=None, date_to=None, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            lock = threading.Lock()
            rows = []
            all_invoices = []
            page = 1

            while True:
                params = {"fields": "id,number,totalAmount,subTotal,retailWpName,statusName",
                    "sp.pageSize": 200, "sp.page": page, "filter.status": "OPEN"}
                if date_from:
                    params["filter.transDate.op"] = "BETWEEN"
                    params["filter.transDate.val[0]"] = date_from
                    params["filter.transDate.val[1]"] = date_to or date_from
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                all_invoices.extend(page_data)
                print(f"[BG UNPAID DETAIL] page {page}/{sp.get('pageCount',1)} loaded={len(all_invoices)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            def enrich(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    owing = float(detail.get("primeOwing") or 0)
                    if owing <= 0:
                        return
                    customer = detail.get("customer")
                    if isinstance(customer, dict): cname = customer.get("name")
                    elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                    else: cname = None
                    name = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                    number = detail.get("number") or inv.get("number") or "-"
                    with lock:
                        rows.append({"number": number, "name": name, "nilai": owing})
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(enrich, all_invoices))

            if not rows:
                send_message(chat_id, f"✅ Tidak ada invoice dengan sisa tagihan untuk {label}.")
                return

            rows.sort(key=lambda x: x["nilai"], reverse=True)
            total_nilai = sum(x["nilai"] for x in rows)
            header = f"📋 *Invoice Belum Bayar - {label}*\n\n"
            header += f"Jumlah invoice (ada sisa): {len(rows)}\n"
            header += f"Total sisa tagihan: Rp {total_nilai:,.0f}\n\n*Daftar (urut sisa terbesar):*\n"
            msg = header
            for x in rows:
                msg += f"• {x['number']} | {x['name']} | Rp {x['nilai']:,.0f}\n"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal ambil daftar invoice: {str(e)[:100]}")
            print(f"[BG UNPAID DETAIL ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def _normalisasi_nama(teks):
    """Samakan variasi ejaan umum agar pencocokan produk lebih tahan banting."""
    t = (teks or "").lower()
    t = t.replace("tumbler", "tumblr")     # Accurate pakai 'tumblr'
    t = t.replace("cartoon", "carton")     # samakan carton/cartoon
    t = t.replace("-", " ")                # samakan pemisah
    return t

# Kata terlalu umum -> diabaikan sebagai filter pencocokan
_KATA_UMUM = {"tumblr", "kertas", "uv", "grafir", "mug", "stiker", "set", "premium"}

def _kata_kunci_cocok(keyword):
    kws = [k for k in _normalisasi_nama(keyword).split() if k not in _KATA_UMUM]
    if not kws:  # kalau semua kata kebetulan umum, pakai apa adanya
        kws = _normalisasi_nama(keyword).split()
    return kws


def tool_get_product_profit(host, chat_id, keyword, date_from, date_to):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            kws_cari = _kata_kunci_cocok(keyword)

            # 1. Ambil harga modal (balanceUnitCost) tiap varian produk dari master item
            modal_map = {}
            page = 1
            while True:
                r = requests.get(f"{h}/accurate/api/item/list.do", headers=accurate_headers(),
                    params={"fields": "id,no,name", "sp.pageSize": 100, "sp.page": page}, timeout=20)
                data = r.json()
                if not data.get("s"): break
                items = data.get("d", [])
                sp = data.get("sp", {})
                cocok = []
                for it in items:
                    nm_norm = _normalisasi_nama(it.get("name"))
                    no_norm = _normalisasi_nama(it.get("no"))
                    if all(k in nm_norm or k in no_norm for k in kws_cari):
                        cocok.append(it)
                lock0 = threading.Lock()
                def amb(it):
                    try:
                        r2 = requests.get(f"{h}/accurate/api/item/detail.do", headers=accurate_headers(), params={"id": it["id"]}, timeout=10)
                        det = r2.json().get("d", {})
                        modal = float(det.get("balanceUnitCost") or 0)
                        with lock0:
                            modal_map[_normalisasi_nama(it["name"])] = modal
                    except: pass
                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(amb, cocok))
                if page >= sp.get("pageCount", 1): break
                page += 1

            if not modal_map:
                send_message(chat_id, f"❌ Produk '{keyword}' tidak ditemukan di master produk.")
                return

            # 2. Scan invoice periode, kumpulkan qty & nilai jual produk tsb
            lock = threading.Lock()
            jual_qty = {}
            jual_total = {}
            all_ids = []
            page = 1
            while True:
                params = {"fields": "id", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_ids.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def scan(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=12)
                    detail = r2.json().get("d", {})
                    items = detail.get("detailItem", [])
                    if not isinstance(items, list): return
                    for item in items:
                        if not isinstance(item, dict): continue
                        # Nama item bisa di itemName, atau di objek item di dalamnya
                        item_obj = item.get("item", {})
                        if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                        nm_raw = item.get("itemName") or (item_obj.get("name") if isinstance(item_obj, dict) else None) or ""
                        nm = _normalisasi_nama(nm_raw)
                        if all(k in nm for k in kws_cari):
                            qty = float(item.get("quantity") or item.get("qty") or 0)
                            # Harga jual: coba beberapa field. amount sering 0,
                            # jadi fallback ke unitPrice*qty atau totalPrice.
                            unit_jual = float(item.get("unitPrice") or item.get("price") or 0)
                            amount = float(item.get("amount") or item.get("totalAmount") or item.get("totalPrice") or 0)
                            if amount <= 0 and unit_jual > 0:
                                amount = unit_jual * qty
                            realname = nm_raw or "-"
                            # Log sekali untuk lihat field angka yang tersedia (diagnosa harga jual)
                            with lock:
                                if not jual_qty:
                                    angka = {k: v for k, v in item.items() if isinstance(v, (int, float))}
                                    print(f"[PROFIT FIELD CEK] {realname} -> {angka}")
                                jual_qty[realname] = jual_qty.get(realname, 0) + qty
                                jual_total[realname] = jual_total.get(realname, 0) + amount
                except: pass

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(scan, all_ids))

            if not jual_qty:
                send_message(chat_id, f"❌ Tidak ada penjualan '{keyword}' di periode {date_from} - {date_to}. (Harga modal tetap bisa dicek lewat 'harga modal {keyword}')")
                return

            # 3. Hitung profit
            total_qty = sum(jual_qty.values())
            total_jual = sum(jual_total.values())
            # Modal total = sum(qty terjual * modal per unit varian)
            total_modal = 0.0
            for nm, qty in jual_qty.items():
                modal_unit = modal_map.get(_normalisasi_nama(nm), 0)
                total_modal += qty * modal_unit
            laba = total_jual - total_modal
            margin = (laba / total_jual * 100) if total_jual > 0 else 0

            msg = f"📊 *Analisa Profit '{keyword}' ({date_from} - {date_to})*\n\n"
            msg += f"Qty terjual: {total_qty:,.0f} pcs\n"
            msg += f"Total penjualan: Rp {total_jual:,.0f}\n"
            msg += f"Total modal (HPP): Rp {total_modal:,.0f}\n"
            msg += f"Laba kotor: Rp {laba:,.0f}\n"
            msg += f"Margin profit: {margin:.1f}%\n\n*Per varian:*\n"
            for nm in sorted(jual_qty, key=lambda x: jual_total.get(x,0), reverse=True):
                qty = jual_qty[nm]
                jual = jual_total.get(nm, 0)
                modal_unit = modal_map.get(_normalisasi_nama(nm), 0)
                jual_unit = (jual/qty) if qty else 0
                m = ((jual_unit - modal_unit)/jual_unit*100) if jual_unit else 0
                msg += f"• {nm}: jual Rp {jual_unit:,.0f}/pcs, modal Rp {modal_unit:,.0f}/pcs → margin {m:.0f}%\n"
            msg += f"\n_Modal = rata-rata HPP Accurate (balanceUnitCost). Margin negatif bisa terjadi jika harga jual di bawah modal atau ada diskon._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung profit: {str(e)[:100]}")
            print(f"[BG PROFIT ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_profit_periode(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # 1. Bangun cache modal (HPP) per item dari master item: id -> balanceUnitCost
            #    Diisi lazy saat scan invoice supaya tidak perlu tarik seluruh master dulu.
            modal_cache = {}
            modal_lock = threading.Lock()
            def get_modal(item_id):
                if item_id is None:
                    return 0.0
                with modal_lock:
                    if item_id in modal_cache:
                        return modal_cache[item_id]
                modal = 0.0
                try:
                    r2 = requests.get(f"{h}/accurate/api/item/detail.do", headers=accurate_headers(), params={"id": item_id}, timeout=12)
                    det = r2.json().get("d", {})
                    modal = float(det.get("balanceUnitCost") or det.get("defStandardCost") or 0)
                except: pass
                with modal_lock:
                    modal_cache[item_id] = modal
                return modal

            # 2. Ambil semua invoice + tanggal
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return datetime.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            lock = threading.Lock()
            # per hari: tgl(YYYY-MM-DD) -> {"jual":x,"modal":y}
            harian = {}
            # per bulan: bulan(YYYY-MM) -> {"jual":x,"modal":y}
            bulanan = {}

            def scan(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=15)
                    det = r2.json().get("d", {})
                    tgl = parse_tgl(det.get("transDate") or inv.get("transDate"))
                    if tgl is None: return
                    hari_key = tgl.strftime("%Y-%m-%d")
                    bulan_key = tgl.strftime("%Y-%m")
                    items = det.get("detailItem", [])
                    jual_inv = 0.0
                    modal_inv = 0.0
                    if isinstance(items, list):
                        for item in items:
                            if not isinstance(item, dict): continue
                            item_obj = item.get("item", {})
                            if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                            item_id = item_obj.get("id") if isinstance(item_obj, dict) else None
                            qty = float(item.get("quantity") or item.get("qty") or 0)
                            modal_inv += get_modal(item_id) * qty
                    # Penjualan = salesAmount/totalAmount invoice (konsisten dgn tool lain).
                    jual_inv = _resolve_nilai_invoice(det)
                    with lock:
                        if hari_key not in harian: harian[hari_key] = {"jual": 0.0, "modal": 0.0}
                        harian[hari_key]["jual"] += jual_inv
                        harian[hari_key]["modal"] += modal_inv
                        if bulan_key not in bulanan: bulanan[bulan_key] = {"jual": 0.0, "modal": 0.0}
                        bulanan[bulan_key]["jual"] += jual_inv
                        bulanan[bulan_key]["modal"] += modal_inv
                except: pass

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(scan, all_inv))

            if not bulanan:
                send_message(chat_id, f"❌ Tidak ada data penjualan di periode {date_from} - {date_to}.")
                return

            total_jual = sum(v["jual"] for v in bulanan.values())
            total_modal = sum(v["modal"] for v in bulanan.values())
            total_laba = total_jual - total_modal
            margin = (total_laba / total_jual * 100) if total_jual > 0 else 0

            # Sorotan: bulan penjualan tertinggi & bulan laba tertinggi
            _NAMA_BLN = {"01":"Januari","02":"Februari","03":"Maret","04":"April","05":"Mei","06":"Juni",
                         "07":"Juli","08":"Agustus","09":"September","10":"Oktober","11":"November","12":"Desember"}
            def _fmt_bln(bk):
                # bk format 'YYYY-MM' -> 'Juni 2026'
                try:
                    y, m = bk.split("-")
                    return f"{_NAMA_BLN.get(m, m)} {y}"
                except:
                    return bk
            bln_jual_top = max(bulanan, key=lambda b: bulanan[b]["jual"])
            bln_laba_top = max(bulanan, key=lambda b: (bulanan[b]["jual"] - bulanan[b]["modal"]))
            jual_top = bulanan[bln_jual_top]["jual"]
            laba_top = bulanan[bln_laba_top]["jual"] - bulanan[bln_laba_top]["modal"]

            judul = label or f"{date_from} - {date_to}"
            msg = f"📊 *Profit per Periode - {judul}*\n"
            msg += f"Dari {len(all_inv)} invoice\n\n"
            msg += f"⭐ *Sorotan:*\n"
            msg += f"📈 Penjualan tertinggi: *{_fmt_bln(bln_jual_top)}* (Rp {jual_top:,.0f})\n"
            msg += f"🏆 Laba tertinggi: *{_fmt_bln(bln_laba_top)}* (Rp {laba_top:,.0f})\n\n"
            msg += f"💵 Total penjualan: Rp {total_jual:,.0f}\n"
            msg += f"🏭 Total modal (HPP): Rp {total_modal:,.0f}\n"
            msg += f"✅ Laba kotor: Rp {total_laba:,.0f} (margin {margin:.1f}%)\n\n"

            msg += f"*📅 Per Bulan:*\n"
            for bk in sorted(bulanan):
                v = bulanan[bk]
                laba = v["jual"] - v["modal"]
                m = (laba / v["jual"] * 100) if v["jual"] > 0 else 0
                msg += f"• {bk}: laba Rp {laba:,.0f} (jual {v['jual']:,.0f} − modal {v['modal']:,.0f}, margin {m:.0f}%)\n"

            # Per hari: tampilkan semua tanggal terurut. Kalau kebanyakan, kirim tetap (send_message auto pecah).
            msg += f"\n*🗓️ Per Hari:*\n"
            for hk in sorted(harian):
                v = harian[hk]
                laba = v["jual"] - v["modal"]
                msg += f"• {hk}: laba Rp {laba:,.0f} (jual {v['jual']:,.0f} − modal {v['modal']:,.0f})\n"

            msg += f"\n_Profit = penjualan − modal HPP (balanceUnitCost Accurate). Modal negatif/0 bisa terjadi jika item belum ada HPP di Accurate._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung profit periode: {str(e)[:120]}")
            print(f"[PROFIT PERIODE ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_rekap_bulanan(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Cache modal HPP per item id
            modal_cache = {}
            modal_lock = threading.Lock()
            def get_modal(item_id):
                if item_id is None:
                    return 0.0
                with modal_lock:
                    if item_id in modal_cache:
                        return modal_cache[item_id]
                modal = 0.0
                try:
                    r2 = requests.get(f"{h}/accurate/api/item/detail.do", headers=accurate_headers(), params={"id": item_id}, timeout=12)
                    det = r2.json().get("d", {})
                    modal = float(det.get("balanceUnitCost") or det.get("defStandardCost") or 0)
                except: pass
                with modal_lock:
                    modal_cache[item_id] = modal
                return modal

            # Ambil semua invoice + tanggal
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return datetime.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            lock = threading.Lock()
            # bulan(YYYY-MM) -> {"jual","modal","inv"}
            bulanan = {}

            def scan(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=15)
                    det = r2.json().get("d", {})
                    tgl = parse_tgl(det.get("transDate") or inv.get("transDate"))
                    if tgl is None: return
                    bulan_key = tgl.strftime("%Y-%m")
                    items = det.get("detailItem", [])
                    jual_inv = 0.0
                    modal_inv = 0.0
                    if isinstance(items, list):
                        for item in items:
                            if not isinstance(item, dict): continue
                            item_obj = item.get("item", {})
                            if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                            item_id = item_obj.get("id") if isinstance(item_obj, dict) else None
                            qty = float(item.get("quantity") or item.get("qty") or 0)
                            modal_inv += get_modal(item_id) * qty
                    # Penjualan invoice = salesAmount/totalAmount (konsisten dgn tool lain).
                    jual_inv = _resolve_nilai_invoice(det)
                    with lock:
                        if bulan_key not in bulanan: bulanan[bulan_key] = {"jual": 0.0, "modal": 0.0, "inv": 0}
                        bulanan[bulan_key]["jual"] += jual_inv
                        bulanan[bulan_key]["modal"] += modal_inv
                        bulanan[bulan_key]["inv"] += 1
                except: pass

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(scan, all_inv))

            if not bulanan:
                send_message(chat_id, f"❌ Tidak ada data penjualan di periode {date_from} - {date_to}.")
                return

            _NAMA_BLN = {"01":"Januari","02":"Februari","03":"Maret","04":"April","05":"Mei","06":"Juni",
                         "07":"Juli","08":"Agustus","09":"September","10":"Oktober","11":"November","12":"Desember"}
            def _fmt_bln(bk):
                try:
                    y, m = bk.split("-"); return f"{_NAMA_BLN.get(m, m)} {y}"
                except: return bk

            total_jual = sum(v["jual"] for v in bulanan.values())
            total_modal = sum(v["modal"] for v in bulanan.values())
            total_laba = total_jual - total_modal

            bln_jual_top = max(bulanan, key=lambda b: bulanan[b]["jual"])
            bln_laba_top = max(bulanan, key=lambda b: (bulanan[b]["jual"] - bulanan[b]["modal"]))

            judul = label or f"{date_from} - {date_to}"
            msg = f"📊 *Rekap Bulanan (Penjualan & Laba) - {judul}*\n"
            msg += f"Dari {len(all_inv)} invoice\n\n"
            msg += f"⭐ *Sorotan:*\n"
            msg += f"📈 Penjualan tertinggi: *{_fmt_bln(bln_jual_top)}* (Rp {bulanan[bln_jual_top]['jual']:,.0f})\n"
            msg += f"🏆 Laba tertinggi: *{_fmt_bln(bln_laba_top)}* (Rp {bulanan[bln_laba_top]['jual'] - bulanan[bln_laba_top]['modal']:,.0f})\n\n"

            # Tabel per bulan (urut kronologis)
            msg += f"*Rincian per bulan:*\n"
            for bk in sorted(bulanan):
                v = bulanan[bk]
                laba = v["jual"] - v["modal"]
                m = (laba / v["jual"] * 100) if v["jual"] > 0 else 0
                msg += f"• {_fmt_bln(bk)}: jual Rp {v['jual']:,.0f} | laba Rp {laba:,.0f} ({m:.0f}%) | {v['inv']} inv\n"

            msg += f"\n*Total periode:*\n"
            msg += f"Penjualan Rp {total_jual:,.0f} | Modal Rp {total_modal:,.0f} | Laba Rp {total_laba:,.0f}\n"
            msg += f"\n_Laba = penjualan − modal HPP (balanceUnitCost). Penjualan tertinggi selalu akurat; laba tergantung HPP item di Accurate sudah terisi._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap bulanan: {str(e)[:120]}")
            print(f"[REKAP BULANAN ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_sales_per_salesman(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            lock = threading.Lock()
            sales_data = {}
            all_invoices = []
            page = 1
            total_invoice = 0
            while True:
                params = {"fields": "id,number,transDate,totalAmount,salesAmount,subTotal,masterSalesmanName,masterSalesmanId", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1: total_invoice = sp.get("rowCount", 0)
                all_invoices.extend(page_data)
                print(f"[BG SALES] list {page}/{sp.get('pageCount',1)} loaded={len(all_invoices)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            peta = _get_peta_salesman(h)

            def catat(sales_name, nilai):
                if not sales_name or not str(sales_name).strip():
                    sales_name = "Tanpa Sales"
                with lock:
                    if sales_name not in sales_data:
                        sales_data[sales_name] = {"count": 0, "total": 0.0}
                    sales_data[sales_name]["count"] += 1
                    sales_data[sales_name]["total"] += nilai

            # PENTING (sudah dipastikan lewat debug): endpoint LIST hanya mengirim
            # id, number, transDate, totalAmount, subTotal, statusName — TIDAK ada nama/ID sales.
            # Jadi: nilai diambil dari list (dijamin lengkap), nama sales diambil dari
            # detail tiap invoice. Nilai TIDAK PERNAH hilang walau detail gagal dibaca.

            # 1. Baca nama sales dari detail dengan beban RINGAN (hemat koneksi).
            valid = [inv for inv in all_invoices if isinstance(inv, dict) and inv.get("id") is not None]
            id_sales = _baca_nama_sales_massal(h, valid, max_workers=3)

            # 2. Akumulasi: nilai dari list (SELALU masuk, akurat), nama dari peta id_sales
            for inv in valid:
                nilai = float(inv.get("totalAmount") or inv.get("salesAmount") or inv.get("subTotal") or 0)
                nama = id_sales.get(inv.get("id")) or "Tanpa Sales"
                catat(nama, nilai)

            if not sales_data:
                send_message(chat_id, f"❌ Tidak ada data penjualan untuk {label}.")
                return

            sorted_sales = sorted(sales_data.items(), key=lambda x: x[1]["total"], reverse=True)
            grand_total = sum(v["total"] for v in sales_data.values())
            msg = f"👥 *Penjualan per Sales - {label}*\n\n"
            msg += f"Total invoice: {total_invoice}\n"
            msg += f"Total penjualan: Rp {grand_total:,.0f}\n"
            msg += f"Jumlah sales: {len(sales_data)}\n\n*Rincian (urut nilai terbesar):*\n"
            for name, d in sorted_sales:
                msg += f"• {name}: Rp {d['total']:,.0f} ({d['count']} inv)\n"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap sales: {str(e)[:100]}")
            print(f"[BG SALES ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


# ============================================================
# CLAUDE TOOLS DEFINITION
# ============================================================

TOOLS = [
    {
        "name": "get_invoices",
        "description": "Ambil daftar sales invoice dari Accurate Online. Filter by status (OPEN=belum lunas, CLOSED=lunas), tanggal, keyword customer. PENTING: hasil tool ini SUDAH otomatis berisi nama customer (customerName), outstanding, dan totalAmount untuk maksimal 20 invoice pertama. JANGAN panggil get_invoice_detail lagi untuk invoice-invoice ini kecuali user minta rincian item produk di dalam satu invoice spesifik.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Tanggal mulai DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "Tanggal akhir DD/MM/YYYY"},
                "status": {"type": "string", "description": "OPEN atau CLOSED"},
                "keyword": {"type": "string", "description": "Nama customer atau nomor invoice"},
                "page_size": {"type": "integer", "description": "Jumlah data max 100"}
            }
        }
    },
    {
        "name": "get_invoice_detail",
        "description": "Ambil detail lengkap SATU invoice termasuk rincian item produk. Hanya gunakan jika user minta isi/rincian produk dari satu invoice tertentu. Untuk omset/daftar invoice biasa, get_invoices saja sudah cukup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer", "description": "ID invoice"}
            },
            "required": ["invoice_id"]
        }
    },
    {
        "name": "get_items",
        "description": "Cari produk di Accurate: harga jual (unitPrice), stok (availableStock), harga modal/HPP per unit (hargaModal, dari rata-rata cost), SKU. Nama produk di Accurate mungkin disingkat, contoh 'Tumblr' bukan 'Tumbler'. Field hargaModal adalah harga modal/beli rata-rata per unit yang sebenarnya (purchasePrice biasanya 0 jadi jangan dipakai).",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama produk"},
                "page_size": {"type": "integer", "description": "Jumlah hasil"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "get_attachment",
        "description": "Ambil dan kirim bukti bayar/lampiran invoice ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string", "description": "Nomor invoice, contoh: SI.2026.06.00888"}
            },
            "required": ["invoice_number"]
        }
    },
    {
        "name": "get_sales_per_item",
        "description": "Hitung penjualan produk TERTENTU di periode tertentu. Background 3-5 menit. Untuk 'niagara laku berapa bulan juni'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama produk"},
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"}
            },
            "required": ["keyword", "date_from", "date_to"]
        }
    },
    {
        "name": "get_unpaid_customers_background",
        "description": "Daftar semua customer belum bayar. Background 2-3 menit. Untuk 'siapa saja belum bayar bulan juni'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            }
        }
    },
    {
        "name": "get_piutang_summary",
        "description": "Total nilai piutang keseluruhan. Background 2-3 menit. Untuk 'berapa total piutang'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY, kosongkan untuk semua periode"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            }
        }
    },
    {
        "name": "get_omset_summary",
        "description": "Hitung TOTAL pendapatan/omset/penjualan satu periode dengan membaca SEMUA invoice (semua halaman, bukan cuma 100). WAJIB pakai tool ini untuk pertanyaan total omset/pendapatan/penjualan per bulan atau per periode, contoh 'berapa pendapatan Juni', 'total penjualan bulan ini', 'omset Mei'. JANGAN pakai get_invoices untuk menghitung total omset karena get_invoices dibatasi 100 invoice. Background 2-3 menit, hasil dikirim otomatis ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juni 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "get_low_stock",
        "description": "Cek produk kategori tertentu yang stoknya MENIPIS (di bawah ambang batas, default 30 pcs). Untuk 'stok tumbler yang menipis', 'tumbler di bawah 30 pcs', 'mug yang hampir habis'. User HARUS sebut kategori produk (tumbler, mug, banner, dll). Background, hasil dikirim ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Kategori/nama produk, contoh 'tumbler'"},
                "threshold": {"type": "integer", "description": "Ambang batas stok menipis, default 30"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "get_overdue_customers",
        "description": "Daftar customer yang menunggak / belum bayar dan sudah LEWAT jatuh tempo lebih dari sekian hari (default 30 hari dari dueDate). Untuk 'siapa yang nunggak lebih dari 30 hari', 'customer telat bayar', 'tunggakan jatuh tempo'. Background 2-3 menit, hasil dikirim ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Jumlah hari lewat jatuh tempo, default 30"}
            }
        }
    },
    {
        "name": "get_unpaid_invoices_detail",
        "description": "Daftar LENGKAP invoice belum bayar berikut NOMOR invoice, NAMA customer, dan NILAI masing-masing, satu periode. Nama customer diambil lengkap untuk SEMUA invoice (bukan cuma sebagian). WAJIB pakai tool ini kalau user minta rincian invoice belum bayar dengan nomor invoice / nama / nilai per invoice, contoh 'customer belum bayar Juni, sebutkan nomor invoice dan nilainya'. JANGAN pakai get_invoices untuk ini karena get_invoices hanya 100 invoice dan nama tidak lengkap. Background 2-3 menit, hasil dikirim ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            }
        }
    },
    {
        "name": "get_sales_per_salesman",
        "description": "Rekap penjualan per tenaga penjual / sales / salesman di satu periode: nama tiap sales, total nilai penjualan, dan jumlah invoice. Untuk 'penjualan per sales', 'siapa saja sales-nya dan penjualannya berapa', 'rekap salesman Juni'. Background 3-5 menit, hasil dikirim ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juni 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "get_product_profit",
        "description": "Hitung PROFIT/MARGIN/KEUNTUNGAN produk tertentu: bandingkan harga modal (HPP) dengan harga jual dari invoice, hitung laba dan persen margin. Untuk 'berapa profit tumbler sultan', 'margin keuntungan produk X', 'bandingkan harga beli dan jual produk X'. Background 3-5 menit, hasil dikirim ke Telegram. Nama produk Accurate sering disingkat (Tumblr bukan Tumbler) tapi tool sudah menangani.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama produk, contoh 'sultan' atau 'tumbler sultan'"},
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"}
            },
            "required": ["keyword", "date_from", "date_to"]
        }
    },
    {
        "name": "get_produk_terlaku",
        "description": "Rekap SEMUA produk terlaku/terjual di satu rentang tanggal, diurutkan dari qty tertinggi ke terendah, semua kategori sekaligus (tidak perlu keyword). Untuk 'produk terlaku hari ini', 'produk paling laris minggu ini', 'urutkan semua produk dari penjualan tertinggi'. Cocok untuk rentang pendek (harian/mingguan) karena cepat. Untuk rentang panjang sebulan penuh boleh juga tapi lebih lama. Background, hasil dikirim ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Hari Ini 24 Juni 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "piutang_per_sales",
        "description": "Rekap piutang (tagihan belum dibayar) DIKELOMPOKKAN PER SALES/salesman di satu periode: tiap sales total piutangnya berapa, lengkap rincian nomor invoice + nama customer + umur piutang (berapa hari). Untuk 'piutang per sales', 'tagihan belum bayar tiap sales', 'sales mana yang piutangnya paling besar'. Background 5-10 menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juni 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "cek_piutang_customer",
        "description": "Cek piutang/tagihan belum dibayar untuk SATU customer tertentu berdasarkan nama, lengkap dengan umur piutang (sudah berapa hari). Untuk 'piutang si X', 'berapa utang customer Y', 'tagihan Z sudah berapa lama'. Cukup beri nama customer apa adanya (mis. 'Rico'), tool akan mencari. Background, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nama_customer": {"type": "string", "description": "Nama customer, contoh 'Rico' atau 'PRINTAMA MEDIA'"}
            },
            "required": ["nama_customer"]
        }
    },
    {
        "name": "cek_rekening_tujuan",
        "description": "Cek bukti bayar satu periode: apakah NAMA REKENING TUJUAN (penerima transfer) atas nama Six Pratama. Mengelompokkan: rekening benar (Six Pratama), rekening BEDA (perlu dicek), dan tanpa nama rekening (mis. Shopee, dilewati). Untuk 'cek rekening tujuan apakah atas nama Six Pratama', 'pastikan transfer masuk ke rekening saya'. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "bukti_tidak_cocok_per_sales",
        "description": "Cek bukti bayar satu periode, ambil yang nominalnya TIDAK COCOK dengan invoice, lalu kelompokkan PER SALES (nama sales -> nomor invoice -> nilai invoice vs bukti -> selisih). Untuk 'rincian bukti bayar yang tidak cocok per sales', 'invoice mana yang selisih dikelompokkan per salesman'. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "cek_nominal_massal",
        "description": "Cek KECOCOKAN NOMINAL untuk SEMUA invoice satu periode yang ada bukti bayarnya di Drive sekaligus (baca foto vs nilai invoice), diproses paralel agar lebih cepat. Untuk 'cek apakah semua bukti bayar sudah sesuai/cocok', 'cocokkan nominal semua bukti bulan X'. Hanya memproses invoice yang sudah ada filenya di Drive. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "cek_bukti_bayar_massal",
        "description": "Cek SEMUA invoice di satu periode terhadap file bukti bayar di Google Drive sekaligus: mana yang sudah ada file buktinya, mana yang belum (berdasarkan nama file = nomor invoice). Untuk 'cek semua bukti bayar bulan Juni', 'invoice mana saja yang belum ada buktinya'. CATATAN: ini cek KEBERADAAN file, BUKAN baca nominal (terlalu berat untuk ratusan foto). Background, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "get_customer_terbanyak",
        "description": "Rekap CUSTOMER dengan order/pesanan TERBANYAK di satu periode: tiap customer berapa kali order (jumlah invoice) dan total nilai belanjanya. Hasil MEMISAHKAN customer asli (di atas) dari channel/marketplace seperti Shopee, Tokopedia, dan 'Tanpa Nama' (di bagian terpisah). Nilai diambil dari detail tiap invoice agar akurat. WAJIB pakai tool ini untuk 'customer order terbanyak', 'pelanggan paling sering pesan', 'customer dengan belanja terbesar', 'siapa customer paling aktif 6 bulan'. JANGAN pakai get_unpaid_invoices_detail atau get_invoices untuk ini. DEFAULT urut by NILAI rupiah (belanja terbesar di atas); pakai urut_by='order' HANYA kalau user eksplisit minta diurutkan dari jumlah/banyaknya order. Background, untuk periode panjang (6 bulan) bisa 5-10 menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh '6 Bulan Terakhir' atau 'Jan-Jun 2026'"},
                "urut_by": {"type": "string", "description": "'order' (default, urut jumlah order) atau 'nilai' (urut total belanja Rp terbesar)"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "get_rata_sales_per_bulan",
        "description": "Hitung RATA-RATA penjualan per tenaga penjual/sales PER BULAN selama satu periode: tiap sales total penjualannya dibagi jumlah bulan dalam periode. WAJIB pakai tool ini untuk 'rata-rata penjualan tiap sales per bulan', 'penjualan masing-masing sales rata per bulan berapa', 'rata-rata omset salesman'. Beda dengan get_sales_per_salesman (itu cuma total, bukan rata-rata per bulan). Background 3-5 menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Jan-Jun 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "get_customer_reguler",
        "description": "Cari CUSTOMER yang RUTIN/REGULER order produk tertentu selama periode: bukan sekadar pernah beli, tapi ordernya konsisten muncul hampir tiap bulan (reguler bulanan) dan hampir tiap minggu (reguler mingguan). Hasil menampilkan dua kelompok terpisah. WAJIB pakai tool ini untuk 'customer yang order stiker dan kertas reguler', 'pelanggan rutin order tiap bulan/minggu', 'siapa yang langganan tetap'. PENTING soal keyword: 'stiker' dan 'kertas' adalah KATEGORI, bukan nama produk. Di Accurate, STIKER = bahan chromo, vinyl; KERTAS = art paper, art carton, ivory. Untuk 'stiker dan kertas reguler' BIARKAN keyword default (chromo,vinyl,art paper,art carton,ivory). Kalau user sebut produk lain, isi keyword dengan nama/bahan produk itu, dipisah KOMA (bukan spasi) supaya frasa multi-kata seperti 'art paper' utuh. Background 5-10 menit (scan detail invoice), hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "keyword": {"type": "string", "description": "Frasa produk dipisah KOMA, default 'chromo,vinyl,art paper,art carton,ivory' (stiker=chromo/vinyl, kertas=art paper/art carton/ivory). Cocok jika nama item mengandung salah satu frasa."},
                "label": {"type": "string", "description": "Label periode, contoh '6 Bulan Terakhir'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "hitung_lunas_metode",
        "description": "Hitung invoice LUNAS dalam satu periode, DIPISAH berdasarkan metode bayar: Tunai/Cash vs Transfer, masing-masing jumlah invoice dan total nilainya. WAJIB pakai tool ini untuk 'cek pembayaran cash', 'berapa yang bayar tunai', 'invoice lunas via cash berapa dan totalnya', 'pisahkan pembayaran tunai dan transfer'. Metode dibaca dari sales-receipt (penerimaan penjualan) tiap invoice. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juli 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "rekap_bayar_sales",
        "description": "Laporan/rekap pembayaran invoice LUNAS dikelompokkan PER BULAN lalu PER NAMA SALES, dipisah metode bayar (Tunai vs Transfer), LENGKAP dengan daftar nomor invoice tiap sales. WAJIB pakai tool ini untuk 'rekap pembayaran tiap sales', 'laporan pembayaran per bulan masing-masing sales', 'invoice tunai dan transfer per nama sales', 'kelompokkan invoice lunas per sales dan metode'. Bisa satu bulan atau beberapa bulan sekaligus (otomatis dipisah per bulan). Beda dengan hitung_lunas_metode (itu total tanpa per sales). Background beberapa menit (buka detail tiap invoice), hasil dikirim bertahap ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juli 2026' atau 'Jan-Jun 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "rekap_cash",
        "description": "Rekap total UANG CASH (pembayaran TUNAI) yang diterima dalam periode, dipecah PER MINGGU dan PER BULAN sekaligus. WAJIB pakai tool ini untuk 'rekap uang cash', 'berapa cash diterima per minggu', 'total tunai per bulan', 'uang cash masuk mingguan/bulanan'. Cash TIDAK memerlukan bukti bayar. Hanya menghitung invoice lunas dengan metode Tunai. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juli 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "rekap_transfer_bukti",
        "description": "Rekap invoice lunas metode TRANSFER, dipisah mana yang SUDAH ada bukti bayar di Google Drive dan mana yang BELUM (dikelompokkan per sales, daftar nomor invoice yang perlu ditagih buktinya). Bukti transfer = screenshot Shopee/Tokopedia atau transfer ke rekening Six Pratama / PT Pratama Talenta Media. WAJIB pakai tool ini untuk 'rekap transfer dan buktinya', 'transfer mana yang belum ada bukti', 'invoice transfer belum upload bukti per sales'. Beda dengan rekap_cash (itu tunai, tanpa bukti). Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juli 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "bukti_belum_ada_per_sales",
        "description": "Cek invoice yang BELUM ADA file bukti bayarnya di Google Drive, dikelompokkan PER SALES (nama sales -> daftar nomor invoice + nilai). Secara DEFAULT hanya menampilkan invoice berstatus LUNAS (hanya_lunas=true) — karena itu yang paling relevan: sudah lunas tapi bukti belum diupload. WAJIB pakai tool ini untuk 'sales mana yang customernya sudah lunas tapi belum ada bukti bayar', 'invoice lunas belum ada bukti per sales', 'bukti bayar yang belum ada per sales'. Kalau user mau SEMUA invoice (termasuk yang belum lunas), set hanya_lunas=false. Beda dengan cek_bukti_bayar_massal (tidak per sales) dan bukti_tidak_cocok_per_sales (yang nominalnya beda). Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode"},
                "hanya_lunas": {"type": "boolean", "description": "true (default) = hanya invoice LUNAS yang belum ada bukti. false = semua invoice belum ada bukti."}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "get_profit_periode",
        "description": "Hitung PROFIT/LABA (penjualan − modal HPP) untuk satu periode, ditampilkan rincian PER BULAN dan PER HARI sekaligus. WAJIB pakai tool ini untuk 'profit per hari', 'profit per bulan', 'laba harian dan bulanan', 'keuntungan tiap hari/bulan'. Beda dengan get_product_profit (itu profit per PRODUK tertentu, bukan per periode waktu). Background beberapa menit (buka detail tiap invoice + ambil modal item), hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juni 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "rekap_bulanan",
        "description": "Rekap PENJUALAN dan LABA per bulan dalam SATU pesan untuk satu rentang periode: tabel tiap bulan (penjualan, laba, margin, jumlah invoice) plus sorotan bulan penjualan tertinggi dan bulan laba tertinggi. WAJIB pakai tool ini (SATU KALI saja) untuk pertanyaan seperti 'penjualan dan laba tertinggi bulan apa', 'bulan mana omset/laba paling tinggi', 'rekap penjualan per bulan 2026', 'bandingkan penjualan tiap bulan'. JANGAN memanggil get_omset_summary berkali-kali per bulan untuk ini — cukup panggil rekap_bulanan sekali dengan rentang penuh (mis. 01/01/2026 - 30/06/2026). Background 5-10 menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY, awal periode (mis. 01/01/2026)"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY, akhir periode"},
                "label": {"type": "string", "description": "Label periode, contoh '2026' atau 'Jan-Jun 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "sales_tinggi_rendah_per_bulan",
        "description": "Untuk TIAP sales, tampilkan bulan penjualan TERTINGGI dan bulan TERENDAH-nya selama periode (contoh: 'Febby tertinggi Juni Rp 200jt, terendah Mei Rp 150jt'). WAJIB pakai tool ini untuk 'penjualan tiap sales tertinggi berapa dan terendah berapa', 'bulan tertinggi dan terendah masing-masing sales', 'naik turun penjualan per sales'. Beda dari get_sales_per_salesman (itu total) dan get_rata_sales_per_bulan (itu rata-rata). Bulan tanpa penjualan diabaikan. Background 3-5 menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Jan-Jun 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "cek_nominal_gabungan",
        "description": "Cek bukti bayar GABUNGAN: satu file bukti untuk BEBERAPA invoice sekaligus (nama file memuat 2+ nomor invoice, mis. 'SI.2026.07.00123_SI.2026.07.00124'). Bot membaca semua nomor invoice dari nama file, menjumlahkan nilai semua invoice itu, lalu mencocokkan persis dengan nominal di foto bukti. WAJIB pakai tool ini untuk 'cek bukti bayar gabungan', 'bukti bayar untuk beberapa invoice', 'satu bukti banyak invoice'. Pemisah antar nomor bebas (spasi/underscore/koma). Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Juli 2026'"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "penjualan_sales_per_bulan",
        "description": "Untuk SETIAP sales, tampilkan RINCIAN penjualan TIAP BULAN dalam periode (Januari berapa, Februari berapa, dst) beserta total. WAJIB pakai tool ini untuk 'penjualan setiap sales per bulan', 'sales X penjualan tiap bulan berapa', 'rincian bulanan tiap sales', 'penjualan Caca Januari sampai Juni per bulan'. Beda dari get_sales_per_salesman (cuma total), sales_tinggi_rendah_per_bulan (cuma bulan tertinggi/terendah), get_rata_sales_per_bulan (cuma rata-rata). Kalau user sebut nama sales tertentu, isi nama_sales; kalau tidak, tampilkan semua sales. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Jan-Jun 2026'"},
                "nama_sales": {"type": "string", "description": "Opsional. Nama sales tertentu (mis. 'Caca'). Kosongkan untuk semua sales."}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "customer_rutin_bulanan",
        "description": "Cari CUSTOMER yang order RUTIN hampir tiap bulan (produk apa saja, bukan cuma stiker/kertas). Default kriteria: order di minimal ~5 dari 6 bulan. Menampilkan tiap customer rutin + di bulan mana saja mereka order + jumlah order. WAJIB pakai tool ini untuk 'customer yang order rutin', 'pelanggan rutin tiap bulan', 'siapa langganan tetap seperti Diri Care, Rico'. Marketplace (Shopee/Tokopedia) otomatis dilewati. Beda dari get_customer_reguler (itu khusus produk stiker/kertas). Kalau user minta kriteria lebih longgar/ketat, isi min_bulan. Background beberapa menit, hasil ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "DD/MM/YYYY"},
                "date_to": {"type": "string", "description": "DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode, contoh 'Jan-Jun 2026'"},
                "min_bulan": {"type": "integer", "description": "Opsional. Minimal jumlah bulan berbeda customer harus order agar dianggap rutin. Kosongkan untuk default (~5 dari 6 bulan)."}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "cek_bukti_bayar",
        "description": "Cek bukti bayar/pembayaran sebuah invoice dari Google Drive: cari file bukti (dinamai sesuai nomor invoice), baca nominal di foto, bandingkan dengan nilai invoice di Accurate. Untuk 'cek bukti bayar invoice X', 'apakah invoice X sudah ada bukti bayarnya', 'cocokkan pembayaran invoice X'. Background, hasil dikirim ke Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nomor_invoice": {"type": "string", "description": "Nomor invoice lengkap, contoh SI.2026.06.00986"}
            },
            "required": ["nomor_invoice"]
        }
    }
]

SYSTEM_PROMPT = """Kamu adalah asisten keuangan dan operasional untuk perusahaan Print Master, terhubung ke Accurate Online via API.

Gunakan tools untuk ambil data real-time dari Accurate. Jawab dalam Bahasa Indonesia, ramah, gunakan emoji.
Format angka: Rp 1.500.000. Jangan panggil tool lebih dari 3x per pertanyaan.

PENTING soal efisiensi:
- Untuk pertanyaan omset/penjualan/daftar invoice harian (hari ini, kemarin), cukup panggil get_invoices SATU KALI. Hasilnya sudah berisi nama customer dan nilai. JANGAN panggil get_invoice_detail berulang-ulang setelah get_invoices.
- get_invoice_detail hanya dipakai kalau user minta rincian item produk di dalam satu invoice tertentu.

SANGAT PENTING soal total omset/pendapatan bulanan:
- Untuk pertanyaan TOTAL pendapatan/omset/penjualan satu bulan atau periode (contoh 'berapa pendapatan Juni', 'total penjualan bulan ini'), WAJIB pakai get_omset_summary. JANGAN pakai get_invoices untuk menjumlahkan omset bulanan, karena get_invoices hanya mengambil 100 invoice dari ratusan/ribuan yang ada, sehingga hasilnya SALAH dan terlalu kecil.

SANGAT PENTING soal daftar invoice belum bayar:
- Kalau user minta daftar customer/invoice belum bayar satu bulan LENGKAP DENGAN NOMOR INVOICE, NAMA, atau NILAI per invoice (contoh 'customer belum bayar Juni, sebutkan nomornya dan nilainya', 'piutang Juni atas nama siapa saja, nomor invoice dan jumlahnya'), LANGSUNG panggil get_unpaid_invoices_detail TANPA bertanya ulang dan TANPA menampilkan sampel dari get_invoices. JANGAN pakai get_invoices untuk ini, karena get_invoices hanya 100 invoice dan banyak nama customer tidak terisi (muncul '-').
- Setelah memanggil tool background, cukup beri tahu user singkat bahwa proses berjalan dan hasil dikirim 2-3 menit lagi. JANGAN tampilkan data sampel apa pun.
- Kalau user cuma minta ringkasan jumlah customer belum bayar tanpa rincian per invoice, pakai get_unpaid_customers_background.

Tools background (hasilnya dikirim otomatis ke Telegram setelah selesai, beri tahu user untuk menunggu):
- get_omset_summary: total pendapatan/omset satu periode, baca semua invoice (2-3 menit)
- get_sales_per_item: penjualan produk tertentu (3-5 menit)  
- get_unpaid_customers_background: daftar customer belum bayar (2-3 menit)
- get_unpaid_invoices_detail: daftar invoice belum bayar lengkap dengan nomor + nama + nilai (2-3 menit)
- get_sales_per_salesman: rekap penjualan per tenaga penjual/sales (3-5 menit)
- get_product_profit: hitung profit/margin produk (modal vs jual) (3-5 menit)
- cek_bukti_bayar: cek & cocokkan bukti bayar SATU invoice dari Google Drive (baca nominal foto)
- cek_bukti_bayar_massal: cek SEMUA invoice satu periode, mana yang sudah/belum ada file bukti di Drive. WAJIB pakai ini untuk 'cek semua bukti bayar bulan X', 'invoice mana yang belum ada bukti'. JANGAN pakai get_invoices/attachmentExist (itu lampiran Accurate, bukan Drive). Ini cek keberadaan file saja, bukan nominal.
- cek_nominal_massal: cek KECOCOKAN NOMINAL semua bukti bayar satu periode sekaligus (baca foto vs invoice, paralel). WAJIB pakai ini untuk 'cek apakah semua bukti sudah sesuai/cocok', 'cocokkan nominal semua bukti'. Lebih cepat dari cek satu-satu. CATATAN: ini untuk bukti SATUAN (1 file = 1 invoice).
- cek_nominal_gabungan: khusus bukti bayar GABUNGAN (1 file = beberapa invoice, nama file memuat 2+ nomor invoice). Menjumlahkan nilai semua invoice di nama file lalu cocokkan dengan nominal bukti. WAJIB pakai ini untuk 'cek bukti bayar gabungan', 'bukti untuk beberapa invoice', 'satu bukti banyak invoice'.
- bukti_tidak_cocok_per_sales: cek bukti bayar yang TIDAK COCOK, dikelompokkan per SALES (sales -> invoice -> selisih). WAJIB pakai ini untuk 'rincian bukti tidak cocok per sales', 'invoice selisih dikelompokkan per salesman'. Langsung jalankan, JANGAN minta user kirim hasil sebelumnya.
- cek_rekening_tujuan: cek apakah NAMA REKENING TUJUAN di bukti bayar atas nama Six Pratama. WAJIB pakai ini untuk 'cek rekening tujuan', 'pastikan transfer ke rekening Six Pratama'. Bukti tanpa nama rekening (Shopee) otomatis dilewati.
- cek_piutang_customer: cek piutang SATU customer berdasarkan nama + umur piutang (berapa hari). WAJIB pakai ini untuk 'piutang si X', 'utang customer Y berapa', JANGAN pakai get_invoices. Cukup beri nama customer apa adanya.
- piutang_per_sales: rekap piutang DIKELOMPOKKAN PER SALES + rincian customer & umur. WAJIB pakai ini untuk 'piutang per sales', 'tagihan belum bayar tiap sales'. JANGAN pakai get_invoices atau get_sales_per_salesman (itu untuk omset, bukan piutang).
- get_produk_terlaku: rekap SEMUA produk terlaku di rentang tanggal, terurut dari qty tertinggi ke terendah, menampilkan qty + jumlah invoice + nilai Rp, tanpa perlu keyword. WAJIB pakai ini untuk SEMUA pertanyaan 'produk terlaku/terlaris/paling laku' baik harian, mingguan, MAUPUN BULANAN. Untuk 'produk terlaku hari ini' panggil date_from=date_to=tanggal hari ini. Untuk 'produk terlaku bulan ini' panggil date_from=01/bulan, date_to=tanggal hari ini.
- get_customer_terbanyak: rekap CUSTOMER dengan order terbanyak di satu periode (berapa kali order + total belanja), terurut dari terbanyak. WAJIB pakai ini untuk 'customer order terbanyak', 'pelanggan paling sering pesan', 'customer belanja terbesar', 'customer paling aktif'. Default urut_by='order' (jumlah order); pakai urut_by='nilai' kalau user minta yang nilai/belanjanya terbesar. Untuk '6 bulan terakhir' hitung date_from = tanggal 6 bulan lalu, date_to = hari ini.
- get_rata_sales_per_bulan: RATA-RATA penjualan tiap sales PER BULAN (total sales dibagi jumlah bulan periode). WAJIB pakai ini untuk 'rata-rata penjualan tiap sales per bulan', 'penjualan masing-masing sales rata per bulan berapa'. JANGAN pakai get_sales_per_salesman (itu cuma total).
- get_customer_reguler: customer yang RUTIN order produk tertentu, dikelompokkan reguler bulanan & mingguan. WAJIB pakai ini untuk 'customer yang order stiker dan kertas reguler', 'pelanggan rutin tiap bulan/minggu', 'langganan tetap'. CATATAN: 'stiker'=chromo/vinyl, 'kertas'=art paper/art carton/ivory (itu bahannya di Accurate). Untuk stiker&kertas biarkan keyword default. Kalau produk lain, isi keyword dipisah KOMA.
- customer_rutin_bulanan: customer yang order RUTIN hampir tiap bulan (produk APA SAJA, bukan cuma stiker/kertas), default ≥5 dari 6 bulan. WAJIB pakai ini untuk 'customer yang order rutin', 'pelanggan rutin tiap bulan seperti Diri Care/Rico', 'langganan tetap' TANPA sebut produk tertentu. Marketplace dilewati. Beda dari get_customer_reguler (itu khusus produk stiker/kertas).
- bukti_belum_ada_per_sales: invoice yang BELUM ADA bukti bayarnya di Drive, dikelompokkan PER SALES. DEFAULT hanya invoice LUNAS (hanya_lunas=true). WAJIB pakai ini untuk 'sales mana yang customernya sudah lunas tapi belum ada bukti', 'invoice lunas belum ada bukti per sales', 'bukti bayar belum ada per sales'. Kalau user minta semua (termasuk belum lunas), set hanya_lunas=false. Beda dari cek_bukti_bayar_massal (tidak per sales).
- hitung_lunas_metode: invoice LUNAS satu periode DIPISAH per metode bayar Tunai/Cash vs Transfer (jumlah invoice + total nilai masing-masing). WAJIB pakai ini untuk 'cek pembayaran cash', 'berapa yang bayar tunai', 'invoice lunas via cash berapa dan totalnya', 'pisahkan pembayaran tunai dan transfer'. Metode dibaca dari sales-receipt tiap invoice.
- get_profit_periode: PROFIT/LABA (penjualan − modal HPP) satu periode, rincian PER BULAN dan PER HARI sekaligus. WAJIB pakai ini untuk 'profit per hari', 'profit per bulan', 'laba harian bulanan'. Beda dari get_product_profit (itu per produk, bukan per periode).
- rekap_bulanan: rekap PENJUALAN + LABA per bulan dalam SATU pesan (tabel tiap bulan + sorotan bulan tertinggi penjualan & laba). WAJIB pakai ini (SATU KALI, rentang penuh) untuk 'penjualan dan laba tertinggi bulan apa', 'bulan mana omset/laba paling tinggi', 'rekap penjualan per bulan'. DILARANG memanggil get_omset_summary berkali-kali per bulan untuk pertanyaan seperti ini — itu boros dan hasilnya berserakan. Cukup rekap_bulanan sekali, mis. date_from=01/01/2026 date_to=30/06/2026.
- sales_tinggi_rendah_per_bulan: untuk TIAP sales, bulan penjualan TERTINGGI & TERENDAH-nya (mis. 'Febby tertinggi Juni, terendah Mei'). WAJIB pakai ini untuk 'penjualan tiap sales tertinggi berapa terendah berapa', 'bulan tertinggi & terendah masing-masing sales'. Beda dari get_sales_per_salesman (total) dan get_rata_sales_per_bulan (rata-rata).
- penjualan_sales_per_bulan: untuk TIAP sales, RINCIAN penjualan TIAP BULAN (Jan berapa, Feb berapa, ... + total). WAJIB pakai ini untuk 'penjualan setiap sales per bulan', 'sales Caca tiap bulan berapa', 'rincian bulanan tiap sales', 'penjualan masing-masing sales Januari sampai Juni per bulan'. Kalau user sebut nama sales tertentu isi nama_sales, kalau tidak tampilkan semua. Beda dari sales_tinggi_rendah_per_bulan (cuma 2 bulan ekstrem) dan get_rata_sales_per_bulan (cuma rata-rata).
- get_piutang_summary: total piutang (2-3 menit)
- get_low_stock: produk yang stoknya menipis di bawah ambang batas (perlu sebut kategori produk)
- get_overdue_customers: customer yang nunggak lewat jatuh tempo > sekian hari

Tanggal hari ini: {today}"""


def handle_with_claude(chat_id, user_text, host):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%d/%m/%Y")
    system = SYSTEM_PROMPT.format(today=today)

    conversation_history[chat_id].append({"role": "user", "content": user_text})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    messages = list(conversation_history[chat_id])

    for _ in range(5):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 4096, "system": system, "tools": TOOLS, "messages": messages},
            timeout=60
        )
        response = r.json()
        if "content" not in response:
            print(f"[CLAUDE ERROR] {response}")
            return "Maaf, terjadi error. Coba pertanyaan yang lebih spesifik."

        print(f"[CLAUDE] stop_reason={response.get('stop_reason')} tools={[c['name'] for c in response.get('content',[]) if c['type']=='tool_use']}")
        messages.append({"role": "assistant", "content": response["content"]})

        if response.get("stop_reason") == "end_turn":
            text_blocks = [c["text"] for c in response["content"] if c["type"] == "text"]
            reply = "\n".join(text_blocks)
            conversation_history[chat_id].append({"role": "assistant", "content": reply})
            return reply

        if response.get("stop_reason") == "tool_use":
            tool_results = []
            for block in response["content"]:
                if block["type"] != "tool_use": continue
                tool_name = block["name"]
                tool_input = block["input"]
                tool_use_id = block["id"]
                print(f"[TOOL CALL] {tool_name} input={json.dumps(tool_input)[:150]}")

                if tool_name == "get_invoices":
                    result = tool_get_invoices(host, tool_input)
                elif tool_name == "get_invoice_detail":
                    result = tool_get_invoice_detail(host, tool_input["invoice_id"])
                elif tool_name == "get_items":
                    result = tool_get_items(host, tool_input["keyword"], tool_input.get("page_size", 20))
                elif tool_name == "get_attachment":
                    result = tool_get_attachment(host, chat_id, tool_input["invoice_number"])
                elif tool_name == "get_sales_per_item":
                    result = tool_get_sales_per_item(host, chat_id, tool_input.get("keyword",""), tool_input["date_from"], tool_input["date_to"])
                elif tool_name == "get_top_products_background":
                    result = tool_get_top_products_background(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_unpaid_customers_background":
                    result = tool_get_unpaid_customers_background(host, chat_id, tool_input.get("date_from"), tool_input.get("date_to"), tool_input.get("label",""))
                elif tool_name == "get_piutang_summary":
                    result = tool_get_piutang_summary(host, chat_id, tool_input.get("date_from"), tool_input.get("date_to"), tool_input.get("label","Semua Periode"))
                elif tool_name == "get_omset_summary":
                    result = tool_get_omset_summary(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_low_stock":
                    result = tool_get_low_stock(host, chat_id, tool_input["keyword"], tool_input.get("threshold", 30))
                elif tool_name == "get_overdue_customers":
                    result = tool_get_overdue_customers(host, chat_id, tool_input.get("days", 30))
                elif tool_name == "get_unpaid_invoices_detail":
                    result = tool_get_unpaid_invoices_detail(host, chat_id, tool_input.get("date_from"), tool_input.get("date_to"), tool_input.get("label",""))
                elif tool_name == "get_sales_per_salesman":
                    result = tool_get_sales_per_salesman(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_product_profit":
                    result = tool_get_product_profit(host, chat_id, tool_input["keyword"], tool_input["date_from"], tool_input["date_to"])
                elif tool_name == "cek_bukti_bayar":
                    result = tool_cek_bukti_bayar(host, chat_id, tool_input["nomor_invoice"])
                elif tool_name == "cek_bukti_bayar_massal":
                    result = tool_cek_bukti_bayar_massal(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "cek_nominal_massal":
                    result = tool_cek_nominal_massal(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "cek_nominal_gabungan":
                    result = tool_cek_nominal_gabungan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "bukti_tidak_cocok_per_sales":
                    result = tool_bukti_tidak_cocok_per_sales(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "cek_rekening_tujuan":
                    result = tool_cek_rekening_tujuan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "cek_piutang_customer":
                    result = tool_cek_piutang_customer(host, chat_id, tool_input["nama_customer"])
                elif tool_name == "piutang_per_sales":
                    result = tool_piutang_per_sales(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_produk_terlaku":
                    result = tool_get_produk_terlaku(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_customer_terbanyak":
                    result = tool_get_customer_terbanyak(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""), tool_input.get("urut_by","nilai"))
                elif tool_name == "get_rata_sales_per_bulan":
                    result = tool_get_rata_sales_per_bulan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_customer_reguler":
                    result = tool_get_customer_reguler(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("keyword","chromo,vinyl,art paper,art carton,ivory"), tool_input.get("label",""))
                elif tool_name == "customer_rutin_bulanan":
                    result = tool_customer_rutin_bulanan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""), tool_input.get("min_bulan"))
                elif tool_name == "bukti_belum_ada_per_sales":
                    result = tool_bukti_belum_ada_per_sales(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""), tool_input.get("hanya_lunas", True))
                elif tool_name == "hitung_lunas_metode":
                    result = tool_hitung_lunas_metode(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "rekap_bayar_sales":
                    result = tool_rekap_bayar_sales(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "rekap_cash":
                    result = tool_rekap_cash(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "rekap_transfer_bukti":
                    result = tool_rekap_transfer_bukti(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "get_profit_periode":
                    result = tool_get_profit_periode(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "rekap_bulanan":
                    result = tool_rekap_bulanan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "sales_tinggi_rendah_per_bulan":
                    result = tool_sales_tinggi_rendah_per_bulan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""))
                elif tool_name == "penjualan_sales_per_bulan":
                    result = tool_penjualan_sales_per_bulan(host, chat_id, tool_input["date_from"], tool_input["date_to"], tool_input.get("label",""), tool_input.get("nama_sales"))
                else:
                    result = json.dumps({"error": f"Unknown tool: {tool_name}"})

                tool_results.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": result})
            messages.append({"role": "user", "content": tool_results})

    return "Maaf, tidak bisa memproses permintaan ini."


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
        send_message(chat_id, f"Halo {user_name}! Saya Accurate Checker Bot Print Master 👋\n\nTanya apa saja:\n- 📄 Invoice & status pembayaran\n- 💰 Piutang & belum lunas\n- 📦 Stok & harga produk\n- 📊 Produk terlaris\n- 👥 Customer terbanyak order\n\nLangsung tanya dengan bahasa natural! 😊")
        return "ok", 200
    if user_text == "/reset":
        conversation_history[chat_id] = []
        send_message(chat_id, "Percakapan direset! ✅")
        return "ok", 200

    try:
        requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:
        pass
    try:
        host = get_host()
        if not host:
            send_message(chat_id, "❌ Gagal koneksi ke Accurate Online.")
            return "ok", 200
        reply = handle_with_claude(chat_id, user_text, host)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, f"❌ Error: {str(e)[:100]}")
        print(f"[ERROR] {e}")
    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot OK", 200


@app.route("/debug-item", methods=["GET"])
def debug_item():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        r = requests.get(f"{host}/accurate/api/item/list.do", headers=accurate_headers(),
            params={"fields": "id,no,name,unitPrice,purchasePrice,availableStock,unit", "sp.pageSize": 3, "sp.page": 1}, timeout=15)
        return {"status": r.status_code, "response": r.json()}
    except Exception as e:
        return {"error": str(e)}, 500


def get_drive_token():
    """Ambil access token Google Drive dari service account (JWT flow)."""
    from google.oauth2 import service_account
    import google.auth.transport.requests
    info = _load_google_creds_info()
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def drive_list_files(name_contains=None, page_size=1000):
    """List file di folder GDRIVE_FOLDER_ID. Optional filter nama mengandung teks."""
    token = get_drive_token()
    q = f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false"
    if name_contains:
        safe = name_contains.replace("'", "")
        q += f" and name contains '{safe}'"
    files = []
    page_token = None
    while True:
        params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,size)",
                  "pageSize": min(page_size, 1000), "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        r = requests.get("https://www.googleapis.com/drive/v3/files",
                         headers={"Authorization": f"Bearer {token}"}, params=params, timeout=20)
        data = r.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files


def drive_download_file(file_id):
    """Download isi file (bytes) dari Drive."""
    token = get_drive_token()
    r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"alt": "media", "supportsAllDrives": "true"}, timeout=30)
    return r.content


def drive_cari_subfolder(nama_subfolder):
    """Cari ID subfolder (mis. 'Juni 2026') di dalam folder utama."""
    token = get_drive_token()
    q = f"'{GDRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = requests.get("https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": q, "fields": "files(id,name)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=20)
    for f in r.json().get("files", []):
        if f["name"].lower() == nama_subfolder.lower():
            return f["id"]
    return None


def drive_cari_file_invoice(nomor_invoice):
    """Cari file bukti bayar yang namanya mengandung nomor invoice, di semua subfolder."""
    token = get_drive_token()
    # cari di seluruh folder (folder utama + subfolder) berdasar nama mengandung nomor
    safe = nomor_invoice.replace("'", "")
    q = f"name contains '{safe}' and trashed=false"
    r = requests.get("https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": q, "fields": "files(id,name,mimeType)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=20)
    files = [f for f in r.json().get("files", []) if f.get("mimeType") != "application/vnd.google-apps.folder"]
    return files


def baca_nominal_dari_gambar(image_bytes, mime_type):
    """Kirim gambar ke Claude vision, minta baca nominal total akhir. Return (angka, penjelasan)."""
    import re as _re
    b64img = base64.b64encode(image_bytes).decode("utf-8")
    media = mime_type if mime_type in ("image/png", "image/jpeg", "image/webp", "image/gif") else "image/png"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64img}},
                {"type": "text", "text": "Ini bukti pembayaran/transfer (sering dari Shopee/marketplace). Baca nominal TOTAL yang dibayar pembeli (atau total penghasilan yang tertera paling menonjol). PENTING soal format jawaban: baris PERTAMA berisi HANYA angka nominal tanpa titik, koma, Rp, atau teks apa pun (contoh kalau Rp59.980 tulis: 59980). Baris KEDUA berisi penjelasan singkat. Jangan tulis angka lain di baris pertama. Kalau tidak terbaca, baris pertama tulis 0."}
            ]
        }]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload, timeout=40)
        data = r.json()
    except Exception:
        return (0, "gagal hubungi AI")
    teks = ""
    for blk in data.get("content", []):
        if blk.get("type") == "text":
            teks += blk["text"]
    teks = teks.strip()
    if not teks:
        return (0, "")
    baris = teks.split("\n")
    baris1 = baris[0]
    penjelasan = " ".join(baris[1:]).strip() if len(baris) > 1 else ""
    # Dari baris pertama, ambil rangkaian angka pertama; buang titik/koma ribuan
    bersih = baris1.replace(".", "").replace(",", "").replace(" ", "")
    m = _re.search(r"\d+", bersih)
    angka = 0
    if m:
        kandidat = m.group(0)
        # batasi panjang wajar (maks 12 digit = ratusan miliar); kalau lebih, ambil 12 pertama
        if len(kandidat) > 12:
            kandidat = kandidat[:12]
        try:
            angka = int(kandidat)
        except:
            angka = 0
    if not penjelasan:
        penjelasan = baris1[:80]
    return (angka, penjelasan)


def baca_rekening_tujuan_dari_gambar(image_bytes, mime_type):
    """Baca nama pemilik rekening TUJUAN transfer di bukti bayar. Return (nama, ada_rekening_bool)."""
    b64img = base64.b64encode(image_bytes).decode("utf-8")
    media = mime_type if mime_type in ("image/png", "image/jpeg", "image/webp", "image/gif") else "image/png"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64img}},
                {"type": "text", "text": "Ini bukti pembayaran/transfer. Apakah ada NAMA PEMILIK REKENING TUJUAN (penerima transfer)? Format jawaban: baris PERTAMA tulis HANYA nama penerima persis seperti tertulis (contoh: SIX PRATAMA). Kalau TIDAK ADA nama rekening tujuan sama sekali (misal ini screenshot Shopee/marketplace yang hanya menampilkan total tanpa rekening), baris pertama tulis: TIDAK ADA. Baris kedua boleh penjelasan singkat."}
            ]
        }]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload, timeout=40)
        data = r.json()
    except Exception:
        return ("", False)
    teks = ""
    for blk in data.get("content", []):
        if blk.get("type") == "text":
            teks += blk["text"]
    baris1 = teks.strip().split("\n")[0].strip()
    if not baris1 or baris1.upper().startswith("TIDAK ADA"):
        return ("", False)
    return (baris1, True)


def tool_cek_rekening_tujuan(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            token = get_drive_token()
            # Kumpulkan file Drive: nomor(upper) -> {id, mimeType}
            file_map = {}
            def daftar(folder_id):
                page_token = None
                while True:
                    params = {"q": f"'{folder_id}' in parents and trashed=false",
                              "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000,
                              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                    if page_token: params["pageToken"] = page_token
                    rs = requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                    ds = rs.json()
                    for x in ds.get("files", []):
                        if x.get("mimeType") == "application/vnd.google-apps.folder":
                            daftar(x["id"])
                        else:
                            key = x["name"].rsplit(".", 1)[0].strip().upper()
                            file_map[key] = {"id": x["id"], "mimeType": x.get("mimeType", "image/png")}
                    page_token = ds.get("nextPageToken")
                    if not page_token: break
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false", "fields": "files(id,name,mimeType)",
                        "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    daftar(f["id"])
                else:
                    key = f["name"].rsplit(".", 1)[0].strip().upper()
                    file_map[key] = {"id": f["id"], "mimeType": f.get("mimeType", "image/png")}

            # Ambil invoice periode
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                all_inv.extend(dd.get("d", []))
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            target = []
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                nomor = (inv.get("number") or "").strip().upper()
                if nomor in file_map:
                    target.append({"number": inv.get("number"), "file": file_map[nomor]})
            if not target:
                send_message(chat_id, f"Tidak ada invoice {label} yang ada bukti di Drive.")
                return

            lock = threading.Lock()
            benar, beda, tanpa_rek = [], [], []
            def proses(t):
                try:
                    img = drive_download_file(t["file"]["id"])
                    nama, ada = baca_rekening_tujuan_dari_gambar(img, t["file"]["mimeType"])
                    if not ada:
                        with lock: tanpa_rek.append(t["number"])
                    elif "six" in nama.lower() and "pratama" in nama.lower():
                        with lock: benar.append({"number": t["number"], "nama": nama})
                    else:
                        with lock: beda.append({"number": t["number"], "nama": nama})
                except:
                    with lock: tanpa_rek.append(t["number"])
            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(proses, target))

            judul = label or f"{date_from} - {date_to}"
            msg = f"🏦 *Cek Rekening Tujuan - {judul}*\n"
            msg += f"Dicek: {len(target)} bukti\n"
            msg += f"✅ Atas nama Six Pratama: {len(benar)}\n"
            msg += f"⚠️ Rekening BEDA: {len(beda)}\n"
            msg += f"➖ Tanpa nama rekening (dilewati): {len(tanpa_rek)}\n\n"
            if beda:
                msg += "*⚠️ Rekening tujuan BUKAN Six Pratama (perlu dicek):*\n"
                for b in beda:
                    msg += f"• {b['number']}: {b['nama']}\n"
                msg += "\n"
            else:
                msg += "_Semua bukti yang ada nama rekening tertuju ke Six Pratama. Tidak ada yang menyimpang._\n\n"
            msg += "_Bukti tanpa nama rekening (mis. Shopee) dilewati. Nama dibaca AI, bisa kurang akurat untuk foto buram._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek rekening tujuan: {str(e)[:150]}")
            print(f"[REKENING ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


# Nama channel/marketplace yang BUKAN customer asli — dipisah dari daftar customer.
# Pencocokan: nama customer yang mengandung salah satu kata ini (case-insensitive)
# dianggap channel. "Tanpa Nama" juga masuk kelompok ini.
_CHANNEL_MARKETPLACE = {
    "shopee", "tokopedia", "tokped", "lazada", "tiktok", "tik tok",
    "blibli", "bukalapak", "bli bli", "tanpa nama"
}

def _is_marketplace(nama):
    n = (nama or "").strip().lower()
    if not n:
        return True
    return any(kw in n for kw in _CHANNEL_MARKETPLACE)


def _baca_nama_sales_massal(h, invoices, max_workers=3):
    """Baca nama sales dari detail untuk banyak invoice, dengan beban RINGAN supaya
    Accurate tidak memutus koneksi (connection reset/timeout). Worker sedikit + jeda.
    Nilai TIDAK diambil di sini (nilai dari list saja). Kembalikan dict {id: nama}.
    Yang gagal dibaca tidak masuk dict (nanti dianggap 'Tanpa Sales' oleh pemanggil)."""
    import time as _t
    id_sales = {}
    lock = threading.Lock()
    gagal = []

    def baca(inv):
        iid = inv.get("id")
        if iid is None:
            return
        detail = None
        for attempt in range(3):
            try:
                r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do",
                    headers=accurate_headers(), params={"id": iid}, timeout=25)
                d = r2.json()
                if d.get("s") and d.get("d"):
                    detail = d["d"]; break
            except Exception:
                pass
            _t.sleep(0.6 * (attempt + 1))  # jeda naik tiap gagal
        if detail is None:
            with lock: gagal.append(inv)
            return
        with lock:
            id_sales[iid] = _resolve_sales_name(detail)
        _t.sleep(0.05)  # jeda kecil tiap sukses agar tidak membanjiri server

    valid = [inv for inv in invoices if isinstance(inv, dict) and inv.get("id") is not None]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(baca, valid))
    # satu putaran ulang untuk yang gagal, worker lebih kecil lagi
    if gagal:
        ulang = list(gagal); gagal.clear()
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(baca, ulang))
    return id_sales


def _get_peta_salesman(h):
    """Ambil peta masterSalesmanId -> nama dari master salesman Accurate.
    Dipakai untuk menerjemahkan ID sales (yang ada di list) menjadi nama tanpa
    perlu buka detail tiap invoice. Kembalikan dict {id: nama}."""
    peta = {}
    # Coba beberapa kemungkinan endpoint master salesman
    for ep in ("salesman", "sales-man", "master-salesman"):
        try:
            page = 1
            while True:
                r = requests.get(f"{h}/accurate/api/{ep}/list.do", headers=accurate_headers(),
                    params={"fields": "id,name", "sp.pageSize": 100, "sp.page": page}, timeout=15)
                d = r.json()
                if not d.get("s"):
                    break
                for row in d.get("d", []):
                    if isinstance(row, dict) and row.get("id") is not None:
                        peta[row["id"]] = row.get("name") or f"Sales #{row['id']}"
                sp = d.get("sp", {})
                if page >= sp.get("pageCount", 1):
                    break
                page += 1
            if peta:
                break  # endpoint ini berhasil, tidak perlu coba yang lain
        except Exception:
            continue
    return peta


def _resolve_nilai_invoice(detail):
    """Ambil nilai penjualan invoice dari detail. Accurate PTM ternyata TIDAK punya
    field totalAmount di detail; nilai sebenarnya ada di salesAmount (atau
    salesAmountBase). Urutan fallback: totalAmount -> salesAmount -> salesAmountBase
    -> subTotal. Kembalikan float."""
    if not isinstance(detail, dict):
        return 0.0
    for key in ("totalAmount", "salesAmount", "salesAmountBase", "subTotal"):
        v = detail.get(key)
        if v is not None:
            try:
                fv = float(v)
                if fv != 0:
                    return fv
            except: pass
    # kalau semua 0/kosong, kembalikan 0
    return 0.0


def _resolve_sales_name(detail):
    """Ambil nama sales dari detail invoice, dengan fallback ke objek bersarang.
    Accurate kadang menaruh nama sales di masterSalesmanName, kadang di objek
    'salesman' (dict/list), atau field lain. Kembalikan 'Tanpa Sales' jika benar
    kosong."""
    if not isinstance(detail, dict):
        return "Tanpa Sales"
    # 1. field langsung
    nm = detail.get("masterSalesmanName")
    if nm and str(nm).strip():
        return str(nm).strip()
    # 2. objek salesman bersarang
    for key in ("salesman", "masterSalesman", "salesmanList", "detailSalesman"):
        obj = detail.get(key)
        if isinstance(obj, dict):
            cand = obj.get("name") or obj.get("salesmanName") or obj.get("masterSalesmanName")
            if cand and str(cand).strip():
                return str(cand).strip()
        elif isinstance(obj, list) and obj:
            first = obj[0]
            if isinstance(first, dict):
                cand = first.get("name") or first.get("salesmanName") or first.get("masterSalesmanName")
                if cand and str(cand).strip():
                    return str(cand).strip()
            elif first:
                return str(first).strip()
    return "Tanpa Sales"


def _hitung_jumlah_bulan(date_from, date_to):
    """Hitung berapa bulan kalender tercakup antara date_from dan date_to (DD/MM/YYYY)."""
    from datetime import datetime
    d1 = d2 = None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            d1 = datetime.strptime(date_from, fmt); break
        except: pass
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            d2 = datetime.strptime(date_to, fmt); break
        except: pass
    if not d1 or not d2:
        return 1
    bulan = (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1
    return max(bulan, 1)


def tool_get_rata_sales_per_bulan(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            lock = threading.Lock()
            sales_data = {}  # nama_sales -> {"count": x, "total": y}
            all_invoices = []
            page = 1
            total_invoice = 0
            while True:
                params = {"fields": "id,number,totalAmount,salesAmount,subTotal,masterSalesmanName,masterSalesmanId", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1: total_invoice = sp.get("rowCount", 0)
                all_invoices.extend(page_data)
                print(f"[RATA SALES] list {page}/{sp.get('pageCount',1)} loaded={len(all_invoices)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            peta = _get_peta_salesman(h)

            def catat(sales_name, nilai):
                if not sales_name or not str(sales_name).strip():
                    sales_name = "Tanpa Sales"
                with lock:
                    if sales_name not in sales_data:
                        sales_data[sales_name] = {"count": 0, "total": 0.0}
                    sales_data[sales_name]["count"] += 1
                    sales_data[sales_name]["total"] += nilai

            # Nilai dari list (lengkap & akurat); nama sales dari detail dengan beban RINGAN.
            valid = [inv for inv in all_invoices if isinstance(inv, dict) and inv.get("id") is not None]
            id_sales = _baca_nama_sales_massal(h, valid, max_workers=3)

            for inv in valid:
                nilai = float(inv.get("totalAmount") or inv.get("salesAmount") or inv.get("subTotal") or 0)
                nama = id_sales.get(inv.get("id")) or "Tanpa Sales"
                catat(nama, nilai)

            if not sales_data:
                send_message(chat_id, f"❌ Tidak ada data penjualan untuk {label or (date_from + ' - ' + date_to)}.")
                return

            jml_bulan = _hitung_jumlah_bulan(date_from, date_to)
            sorted_sales = sorted(sales_data.items(), key=lambda x: x[1]["total"], reverse=True)
            grand_total = sum(v["total"] for v in sales_data.values())
            rata_total = grand_total / jml_bulan if jml_bulan else grand_total

            judul = label or f"{date_from} - {date_to}"
            msg = f"📈 *Rata-rata Penjualan per Sales/Bulan - {judul}*\n"
            msg += f"Periode: {jml_bulan} bulan | Total invoice: {total_invoice}\n"
            msg += f"Total penjualan: Rp {grand_total:,.0f} (rata-rata Rp {rata_total:,.0f}/bulan)\n\n"
            msg += f"*Rincian (urut total terbesar):*\n"
            for name, d in sorted_sales:
                rata = d["total"] / jml_bulan if jml_bulan else d["total"]
                msg += f"• {name}: rata Rp {rata:,.0f}/bln\n   (total Rp {d['total']:,.0f} dari {d['count']} inv ÷ {jml_bulan} bln)\n"
            msg += f"\n_Rata-rata = total penjualan sales dibagi {jml_bulan} bulan dalam periode._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung rata-rata sales: {str(e)[:120]}")
            print(f"[RATA SALES ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_sales_tinggi_rendah_per_bulan(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Ambil invoice + nama sales + nilai + tanggal LANGSUNG dari list (andal & cepat).
            # Detail hanya dibuka untuk invoice yang totalAmount-nya 0/kosong di list.
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate,totalAmount,salesAmount,subTotal,masterSalesmanName,masterSalesmanId",
                    "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return datetime.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            lock = threading.Lock()
            # sales -> { bulan(YYYY-MM) -> total nilai }
            sales_bulan = {}
            stat = {"dari_list": 0, "dari_detail": 0, "gagal": 0}

            def catat(sales_name, tgl, nilai):
                if not sales_name or not str(sales_name).strip():
                    sales_name = "Tanpa Sales"
                if tgl is None: return
                bulan_key = tgl.strftime("%Y-%m")
                with lock:
                    if sales_name not in sales_bulan: sales_bulan[sales_name] = {}
                    sales_bulan[sales_name][bulan_key] = sales_bulan[sales_name].get(bulan_key, 0.0) + nilai

            # Nilai & tanggal dari list (lengkap); nama sales dari detail beban RINGAN.
            valid = [inv for inv in all_inv if isinstance(inv, dict) and inv.get("id") is not None]
            id_sales = _baca_nama_sales_massal(h, valid, max_workers=3)

            for inv in valid:
                tgl = parse_tgl(inv.get("transDate"))
                nilai = float(inv.get("totalAmount") or inv.get("salesAmount") or inv.get("subTotal") or 0)
                nama = id_sales.get(inv.get("id")) or "Tanpa Sales"
                catat(nama, tgl, nilai)

            if not sales_bulan:
                send_message(chat_id, f"❌ Tidak ada data penjualan untuk {label or (date_from + ' - ' + date_to)}.")
                return

            _NAMA_BLN = {"01":"Januari","02":"Februari","03":"Maret","04":"April","05":"Mei","06":"Juni",
                         "07":"Juli","08":"Agustus","09":"September","10":"Oktober","11":"November","12":"Desember"}
            def _fmt_bln(bk):
                try:
                    y, m = bk.split("-"); return f"{_NAMA_BLN.get(m, m)} {y}"
                except: return bk

            # Urutkan sales dari total penjualan terbesar
            def total_sales(s):
                return sum(sales_bulan[s].values())
            urut_sales = sorted(sales_bulan, key=total_sales, reverse=True)

            judul = label or f"{date_from} - {date_to}"
            msg = f"📊 *Penjualan Tertinggi & Terendah per Sales - {judul}*\n"
            msg += f"_(dari bulan yang ada penjualan saja)_\n\n"
            for s in urut_sales:
                bln = sales_bulan[s]  # dict bulan -> nilai
                # bulan tertinggi & terendah (hanya bulan yang ada penjualan)
                bln_top = max(bln, key=lambda b: bln[b])
                bln_low = min(bln, key=lambda b: bln[b])
                total = sum(bln.values())
                msg += f"👤 *{s}* (total Rp {total:,.0f}, aktif {len(bln)} bln)\n"
                msg += f"   📈 Tertinggi: {_fmt_bln(bln_top)} — Rp {bln[bln_top]:,.0f}\n"
                if bln_top == bln_low:
                    msg += f"   📉 Terendah: — (cuma 1 bulan ada penjualan)\n"
                else:
                    msg += f"   📉 Terendah: {_fmt_bln(bln_low)} — Rp {bln[bln_low]:,.0f}\n"
            msg += f"\n_Bulan tanpa penjualan diabaikan (tidak dihitung sebagai terendah)._"
            grand = sum(sum(b.values()) for b in sales_bulan.values())
            msg += f"\n_Total semua sales: Rp {grand:,.0f} dari {len(all_inv)} invoice_"
            if stat["gagal"] > 0:
                msg += f"\n⚠️ _{stat['gagal']} invoice gagal dibaca dan tidak masuk hitungan._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung tinggi/rendah per sales: {str(e)[:120]}")
            print(f"[SALES TINGGI RENDAH ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_penjualan_sales_per_bulan(host, chat_id, date_from, date_to, label="", nama_sales=None):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Ambil semua invoice + tanggal + nilai dari list
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate,totalAmount,salesAmount,subTotal", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return datetime.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            # Nama sales dari detail (beban ringan)
            valid = [inv for inv in all_inv if isinstance(inv, dict) and inv.get("id") is not None]
            id_sales = _baca_nama_sales_massal(h, valid, max_workers=3)

            # Kumpulkan: sales -> { bulan(YYYY-MM) -> nilai }
            sales_bulan = {}
            bulan_set = set()
            for inv in valid:
                tgl = parse_tgl(inv.get("transDate"))
                if tgl is None: continue
                bulan_key = tgl.strftime("%Y-%m")
                bulan_set.add(bulan_key)
                nilai = float(inv.get("totalAmount") or inv.get("salesAmount") or inv.get("subTotal") or 0)
                nama = id_sales.get(inv.get("id")) or "Tanpa Sales"
                if nama not in sales_bulan: sales_bulan[nama] = {}
                sales_bulan[nama][bulan_key] = sales_bulan[nama].get(bulan_key, 0.0) + nilai

            if not sales_bulan:
                send_message(chat_id, f"❌ Tidak ada data penjualan untuk {label or (date_from + ' - ' + date_to)}.")
                return

            # Kalau user minta sales tertentu, saring (cocok sebagian nama, case-insensitive)
            if nama_sales and str(nama_sales).strip():
                kw = str(nama_sales).strip().lower()
                terpilih = {s: v for s, v in sales_bulan.items() if kw in s.lower()}
                if not terpilih:
                    daftar = ", ".join(sorted(sales_bulan.keys()))
                    send_message(chat_id, f"❌ Sales '{nama_sales}' tidak ditemukan. Sales yang ada: {daftar}")
                    return
                sales_bulan = terpilih

            _NAMA_BLN = {"01":"Januari","02":"Februari","03":"Maret","04":"April","05":"Mei","06":"Juni",
                         "07":"Juli","08":"Agustus","09":"September","10":"Oktober","11":"November","12":"Desember"}
            def _fmt_bln(bk):
                try:
                    y, m = bk.split("-"); return f"{_NAMA_BLN.get(m, m)} {y}"
                except: return bk

            bulan_urut = sorted(bulan_set)
            judul = label or f"{date_from} - {date_to}"
            msg = f"📊 *Penjualan per Sales Rincian Bulanan - {judul}*\n\n"
            # urut sales dari total terbesar
            for s in sorted(sales_bulan, key=lambda x: sum(sales_bulan[x].values()), reverse=True):
                bln = sales_bulan[s]
                total = sum(bln.values())
                msg += f"👤 *{s}* — total Rp {total:,.0f}\n"
                for bk in bulan_urut:
                    nilai = bln.get(bk, 0.0)
                    msg += f"   {_fmt_bln(bk)}: Rp {nilai:,.0f}\n"
                msg += "\n"
            msg += "_Nilai penjualan per bulan tiap sales. Bulan tanpa penjualan ditampilkan Rp 0._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rincian bulanan per sales: {str(e)[:120]}")
            print(f"[SALES PER BULAN ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def _baca_nama_customer_massal(h, invoices, max_workers=3):
    """Baca nama customer dari detail untuk invoice yang namanya kosong di list.
    Beban ringan (worker sedikit + jeda) agar Accurate tidak memutus koneksi.
    Kembalikan dict {id: nama_customer}."""
    import time as _t
    id_cust = {}
    lock = threading.Lock()
    gagal = []

    def baca(inv):
        iid = inv.get("id")
        if iid is None: return
        detail = None
        for attempt in range(3):
            try:
                r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do",
                    headers=accurate_headers(), params={"id": iid}, timeout=25)
                d = r2.json()
                if d.get("s") and d.get("d"):
                    detail = d["d"]; break
            except Exception: pass
            _t.sleep(0.6 * (attempt + 1))
        if detail is None:
            with lock: gagal.append(inv)
            return
        customer = detail.get("customer")
        if isinstance(customer, dict): cname = customer.get("name")
        elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
        else: cname = None
        nama = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
        with lock:
            id_cust[iid] = nama
        _t.sleep(0.05)

    valid = [inv for inv in invoices if isinstance(inv, dict) and inv.get("id") is not None]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(baca, valid))
    if gagal:
        ulang = list(gagal); gagal.clear()
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(baca, ulang))
    return id_cust


def tool_customer_rutin_bulanan(host, chat_id, date_from, date_to, label="", min_bulan=None):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Ambil semua invoice + tanggal + nama customer dari list
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate,retailWpName", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return datetime.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            valid = [inv for inv in all_inv if isinstance(inv, dict) and inv.get("id") is not None]

            # Nama customer: pakai retailWpName dari list; yang kosong baru baca detail
            perlu_detail = [inv for inv in valid if not (inv.get("retailWpName") and str(inv.get("retailWpName")).strip())]
            id_cust_extra = {}
            if perlu_detail:
                id_cust_extra = _baca_nama_customer_massal(h, perlu_detail, max_workers=3)

            # Kumpulkan: customer -> { set bulan, jumlah order, total nilai(dari list bila ada) }
            cust_bulan = {}   # nama -> set(bulan)
            cust_order = {}   # nama -> jumlah invoice
            for inv in valid:
                tgl = parse_tgl(inv.get("transDate"))
                if tgl is None: continue
                nama = inv.get("retailWpName")
                if not (nama and str(nama).strip()):
                    nama = id_cust_extra.get(inv.get("id")) or "Tanpa Nama"
                bulan_key = tgl.strftime("%Y-%m")
                cust_bulan.setdefault(nama, set()).add(bulan_key)
                cust_order[nama] = cust_order.get(nama, 0) + 1

            if not cust_bulan:
                send_message(chat_id, f"❌ Tidak ada data order untuk {label or (date_from + ' - ' + date_to)}.")
                return

            jml_bulan = _hitung_jumlah_bulan(date_from, date_to)
            # ambang default: minimal 5 dari 6 bulan (>=83%), skala ke periode lain
            if min_bulan is None:
                ambang = max(2, round(jml_bulan * 5 / 6)) if jml_bulan >= 3 else jml_bulan
            else:
                ambang = int(min_bulan)

            # Customer rutin = muncul di >= ambang bulan berbeda
            rutin = []
            for nama, bset in cust_bulan.items():
                if nama == "Tanpa Nama": continue
                if _is_marketplace(nama): continue  # lewati channel marketplace
                if len(bset) >= ambang:
                    rutin.append((nama, len(bset), cust_order.get(nama, 0)))

            rutin.sort(key=lambda x: (x[1], x[2]), reverse=True)

            _NAMA_BLN = {"01":"Jan","02":"Feb","03":"Mar","04":"Apr","05":"Mei","06":"Jun",
                         "07":"Jul","08":"Agu","09":"Sep","10":"Okt","11":"Nov","12":"Des"}
            judul = label or f"{date_from} - {date_to}"
            msg = f"🔁 *Customer Rutin Order Bulanan - {judul}*\n"
            msg += f"Kriteria: order di ≥{ambang} dari {jml_bulan} bulan\n"
            msg += f"Total customer memenuhi: {len(rutin)}\n\n"
            if not rutin:
                msg += "_Tidak ada customer yang memenuhi kriteria rutin. Coba longgarkan (mis. minimal 4 bulan)._"
            else:
                for i, (nama, n_bln, n_order) in enumerate(rutin[:40], 1):
                    # tampilkan bulan mana saja customer ini order
                    blns = sorted(cust_bulan[nama])
                    bln_txt = ", ".join(_NAMA_BLN.get(b.split("-")[1], b) for b in blns)
                    msg += f"{i}. *{nama}* — {n_bln}/{jml_bulan} bln | {n_order} order\n   ({bln_txt})\n"
                if len(rutin) > 40:
                    msg += f"\n_...dan {len(rutin)-40} customer lainnya_"
            msg += "\n\n_Rutin = customer yang ordernya muncul di hampir tiap bulan. Marketplace (Shopee/Tokopedia) dilewati._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek customer rutin: {str(e)[:120]}")
            print(f"[CUST RUTIN ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_customer_reguler(host, chat_id, date_from, date_to, keyword="chromo,vinyl,art paper,art carton,ivory", label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Keyword adalah daftar frasa dipisah KOMA (bukan spasi), supaya frasa
            # multi-kata seperti "art paper" tetap utuh. Cocok jika nama item
            # mengandung SALAH SATU frasa.
            kws = [k.strip().lower() for k in keyword.split(",") if k.strip()]
            if not kws:
                # default: jenis stiker (chromo, vinyl) + kertas (art paper, art carton, ivory)
                kws = ["chromo", "vinyl", "art paper", "art carton", "ivory"]

            # 1. Ambil semua invoice + tanggalnya
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate,retailWpName", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            # 2. Scan detail tiap invoice: kalau ada item yang cocok keyword,
            #    catat customer -> set bulan (YYYY-MM) & set minggu (YYYY-WW)
            lock = threading.Lock()
            cust_bulan = {}   # nama -> set bulan
            cust_minggu = {}  # nama -> set minggu
            cust_total = {}   # nama -> total nilai item cocok

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return datetime.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            def scan(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=12)
                    det = r2.json().get("d", {})
                    items = det.get("detailItem", [])
                    if not isinstance(items, list): return
                    # nama customer dengan fallback lengkap
                    customer = det.get("customer")
                    if isinstance(customer, dict): cname = customer.get("name")
                    elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                    else: cname = None
                    nama = det.get("retailWpName") or det.get("customerName") or cname or "Tanpa Nama"
                    tgl = parse_tgl(det.get("transDate") or inv.get("transDate"))
                    if tgl is None: return
                    bulan_key = tgl.strftime("%Y-%m")
                    minggu_key = tgl.strftime("%Y-W%W")
                    cocok_nilai = 0.0
                    ada_cocok = False
                    for item in items:
                        if not isinstance(item, dict): continue
                        item_obj = item.get("item", {})
                        if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                        nm = (item.get("itemName") or (item_obj.get("name") if isinstance(item_obj, dict) else None) or "").lower()
                        if any(k in nm for k in kws):
                            ada_cocok = True
                            qty = float(item.get("quantity") or item.get("qty") or 0)
                            unit_jual = float(item.get("unitPrice") or item.get("price") or 0)
                            amount = float(item.get("amount") or item.get("totalAmount") or item.get("totalPrice") or 0)
                            if amount <= 0 and unit_jual > 0:
                                amount = unit_jual * qty
                            cocok_nilai += amount
                    if ada_cocok:
                        with lock:
                            cust_bulan.setdefault(nama, set()).add(bulan_key)
                            cust_minggu.setdefault(nama, set()).add(minggu_key)
                            cust_total[nama] = cust_total.get(nama, 0.0) + cocok_nilai
                except: pass

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(scan, all_inv))

            if not cust_bulan:
                send_message(chat_id, f"❌ Tidak ada customer yang order produk ({keyword}) di periode {date_from} - {date_to}.")
                return

            jml_bulan = _hitung_jumlah_bulan(date_from, date_to)
            # ambang reguler bulanan: minimal ~70% dari jumlah bulan (mis. 7 bln -> 5)
            ambang_bulan = max(2, round(jml_bulan * 0.7)) if jml_bulan >= 3 else max(1, jml_bulan)
            # perkiraan jumlah minggu dalam periode
            from datetime import datetime as _dt
            d1 = parse_tgl(date_from); d2 = parse_tgl(date_to)
            jml_minggu = max(1, round(((d2 - d1).days + 1) / 7)) if (d1 and d2) else jml_bulan * 4
            # ambang mingguan: minimal ~60% minggu (order hampir tiap minggu tapi tidak harus sempurna)
            ambang_minggu = max(2, round(jml_minggu * 0.6))

            reguler_bulanan = []
            for nama, bset in cust_bulan.items():
                if len(bset) >= ambang_bulan:
                    reguler_bulanan.append((nama, len(bset), cust_total.get(nama, 0)))
            reguler_mingguan = []
            for nama, mset in cust_minggu.items():
                if len(mset) >= ambang_minggu:
                    reguler_mingguan.append((nama, len(mset), cust_total.get(nama, 0)))

            reguler_bulanan.sort(key=lambda x: x[1], reverse=True)
            reguler_mingguan.sort(key=lambda x: x[1], reverse=True)

            judul = label or f"{date_from} - {date_to}"
            msg = f"🔁 *Customer Reguler Order ({keyword}) - {judul}*\n"
            msg += f"Periode: {jml_bulan} bulan (~{jml_minggu} minggu) | Total customer beli: {len(cust_bulan)}\n\n"

            msg += f"*📅 Reguler Bulanan* (order di ≥{ambang_bulan} dari {jml_bulan} bulan):\n"
            if reguler_bulanan:
                for i, (nama, n, nilai) in enumerate(reguler_bulanan[:30], 1):
                    msg += f"{i}. {nama} — {n}/{jml_bulan} bulan | Rp {nilai:,.0f}\n"
                if len(reguler_bulanan) > 30:
                    msg += f"_...dan {len(reguler_bulanan)-30} lainnya_\n"
            else:
                msg += "_(tidak ada yang memenuhi)_\n"

            msg += f"\n*🗓️ Reguler Mingguan* (order di ≥{ambang_minggu} dari ~{jml_minggu} minggu):\n"
            if reguler_mingguan:
                for i, (nama, n, nilai) in enumerate(reguler_mingguan[:30], 1):
                    msg += f"{i}. {nama} — {n}/~{jml_minggu} minggu | Rp {nilai:,.0f}\n"
                if len(reguler_mingguan) > 30:
                    msg += f"_...dan {len(reguler_mingguan)-30} lainnya_\n"
            else:
                msg += "_(tidak ada yang memenuhi)_\n"

            msg += f"\n_Reguler = customer yang ada orderan produk ({keyword}) konsisten. Bulanan: ada order di hampir tiap bulan. Mingguan: ada order di hampir tiap minggu._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek customer reguler: {str(e)[:120]}")
            print(f"[CUST REGULER ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_customer_terbanyak(host, chat_id, date_from, date_to, label="", urut_by="nilai"):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            # Ambil semua invoice di periode (semua halaman) beserta nama customer & nilai
            all_inv = []
            page = 1
            total_invoice = 0
            while True:
                params = {"fields": "id,number,retailWpName,totalAmount,subTotal", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1: total_invoice = sp.get("rowCount", 0)
                all_inv.extend(page_data)
                print(f"[CUST TERBANYAK] page {page}/{sp.get('pageCount',1)} loaded={len(all_inv)}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            # PENTING: totalAmount di endpoint LIST sering 0/kosong, jadi tidak bisa dipakai
            # untuk menjumlahkan nilai. Kita buka DETAIL tiap invoice untuk nilai yang akurat
            # (sama seperti tool omset). Nama customer juga diambil dari detail dengan fallback.
            lock = threading.Lock()
            cust = {}  # nama -> {"count": x, "total": y}

            def catat(name, nilai):
                if not name or not str(name).strip():
                    name = "Tanpa Nama"
                with lock:
                    if name not in cust:
                        cust[name] = {"count": 0, "total": 0.0}
                    cust[name]["count"] += 1
                    cust[name]["total"] += nilai

            def ambil_detail(inv):
                if not isinstance(inv, dict):
                    return
                detail = None
                for attempt in range(4):
                    try:
                        r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=15)
                        d = r2.json()
                        if d.get("s") and d.get("d"):
                            detail = d["d"]; break
                    except Exception: pass
                    import time as _t; _t.sleep(0.4 * (attempt + 1))
                if detail is None:
                    # gagal baca detail: pakai nama dari list kalau ada, nilai 0 (jangan ngarang)
                    catat(inv.get("retailWpName") or "Tanpa Nama", 0.0)
                    return
                customer = detail.get("customer")
                if isinstance(customer, dict): cname = customer.get("name")
                elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                else: cname = None
                nama = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                # Nilai invoice dari detail: totalAmount paling akurat, fallback ke subTotal
                nilai = _resolve_nilai_invoice(detail)
                catat(nama, nilai)

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(ambil_detail, all_inv))

            if not cust:
                send_message(chat_id, f"❌ Tidak ada data order di periode {date_from} - {date_to}.")
                return

            # Pisahkan customer asli vs channel/marketplace (Shopee, Tokopedia, Tanpa Nama, dll)
            customer_asli = {nm: d for nm, d in cust.items() if not _is_marketplace(nm)}
            channel = {nm: d for nm, d in cust.items() if _is_marketplace(nm)}

            def _sort(dic):
                if urut_by == "nilai":
                    return sorted(dic.items(), key=lambda x: x[1]["total"], reverse=True)
                return sorted(dic.items(), key=lambda x: x[1]["count"], reverse=True)

            judul_urut = "Total Belanja Terbesar" if urut_by == "nilai" else "Order Terbanyak"
            urut_asli = _sort(customer_asli)
            urut_channel = _sort(channel)

            grand_total = sum(v["total"] for v in cust.values())
            judul = label or f"{date_from} - {date_to}"
            msg = f"🏅 *Customer {judul_urut} - {judul}*\n"
            msg += f"Total invoice: {total_invoice} | Total nilai: Rp {grand_total:,.0f}\n"
            msg += f"Customer asli: {len(customer_asli)} | Channel/marketplace: {len(channel)}\n\n"

            msg += f"*👤 Customer Asli (Top 30):*\n"
            if urut_asli:
                for i, (nama, d) in enumerate(urut_asli[:30], 1):
                    msg += f"{i}. {nama} — Rp {d['total']:,.0f} | {d['count']} order\n"
                if len(urut_asli) > 30:
                    msg += f"_...dan {len(urut_asli)-30} customer lainnya_\n"
            else:
                msg += "_(tidak ada)_\n"

            if urut_channel:
                msg += f"\n━━━━━━━━━━\n*🛒 Channel / Marketplace (terpisah):*\n"
                for nama, d in urut_channel:
                    msg += f"• {nama} — Rp {d['total']:,.0f} | {d['count']} order\n"
                msg += "_Ini channel penjualan, bukan nama pelanggan, jadi dipisah dari ranking customer._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap customer: {str(e)[:120]}")
            print(f"[CUST TERBANYAK ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_get_produk_terlaku(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            # Ambil semua invoice di rentang
            all_ids = []
            page = 1
            while True:
                params = {"fields": "id", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_ids.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            lock = threading.Lock()
            qty_map = {}
            nilai_map = {}
            inv_map = {}
            def scan(inv):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=12)
                    detail = r2.json().get("d", {})
                    items = detail.get("detailItem", [])
                    if not isinstance(items, list): return
                    produk_di_inv = set()
                    for item in items:
                        if not isinstance(item, dict): continue
                        item_obj = item.get("item", {})
                        if isinstance(item_obj, list): item_obj = item_obj[0] if item_obj else {}
                        nm = item.get("itemName") or (item_obj.get("name") if isinstance(item_obj, dict) else None) or "(tanpa nama)"
                        qty = float(item.get("quantity") or item.get("qty") or 0)
                        unit_jual = float(item.get("unitPrice") or item.get("price") or 0)
                        amount = float(item.get("amount") or item.get("totalAmount") or item.get("totalPrice") or 0)
                        if amount <= 0 and unit_jual > 0:
                            amount = unit_jual * qty
                        with lock:
                            qty_map[nm] = qty_map.get(nm, 0) + qty
                            nilai_map[nm] = nilai_map.get(nm, 0) + amount
                        produk_di_inv.add(nm)
                    with lock:
                        for nm in produk_di_inv:
                            inv_map[nm] = inv_map.get(nm, 0) + 1
                except: pass

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(scan, all_ids))

            if not qty_map:
                send_message(chat_id, f"❌ Tidak ada produk terjual di periode {date_from} - {date_to}.")
                return

            # Urutkan dari NILAI Rp tertinggi (nominal terbesar)
            urut = sorted(nilai_map.items(), key=lambda x: x[1], reverse=True)
            total_qty = sum(qty_map.values())
            total_nilai = sum(nilai_map.values())
            judul = label or f"{date_from} - {date_to}"
            msg = f"🏆 *Produk Terlaku (by Nominal) - {judul}*\n"
            msg += f"Dari {len(all_ids)} invoice | Total {total_qty:,.0f} pcs | Rp {total_nilai:,.0f}\n\n"
            for i, (nm, nilai) in enumerate(urut, 1):
                qty = qty_map.get(nm, 0)
                jml_inv = inv_map.get(nm, 0)
                msg += f"{i}. {nm}: Rp {nilai:,.0f} | {qty:,.0f} pcs | {jml_inv} inv\n"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap produk: {str(e)[:120]}")
            print(f"[PRODUK TERLAKU ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_piutang_per_sales(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Ambil semua invoice di periode
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate,dueDate", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            lock = threading.Lock()
            per_sales = {}  # nama_sales -> {"total": x, "items": [...]}

            def cek(inv):
                detail = None
                for attempt in range(5):
                    try:
                        r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=20)
                        d = r2.json()
                        if d.get("s") and d.get("d"):
                            detail = d["d"]; break
                    except Exception: pass
                    import time as _t; _t.sleep(0.4 * (attempt + 1))
                if detail is None: return
                owing = float(detail.get("primeOwing") or 0)
                if owing <= 0: return  # cuma yang masih ada piutang
                sales = detail.get("masterSalesmanName") or "Tanpa Sales"
                # Ambil nama customer dengan fallback lengkap (sama seperti fungsi lain),
                # supaya tidak muncul '?' kalau retailWpName/customerName kosong.
                customer = detail.get("customer")
                if isinstance(customer, dict): cname = customer.get("name")
                elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                else: cname = None
                cust = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                due = detail.get("dueDate") or inv.get("dueDate") or ""
                tgl = detail.get("transDate") or inv.get("transDate") or ""
                umur = None
                acuan = due or tgl
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        dd = datetime.strptime(acuan, fmt); umur = (datetime.now() - dd).days; break
                    except: pass
                with lock:
                    if sales not in per_sales:
                        per_sales[sales] = {"total": 0.0, "items": []}
                    per_sales[sales]["total"] += owing
                    per_sales[sales]["items"].append({"number": inv.get("number"), "cust": cust, "owing": owing, "umur": umur, "due": due})

            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(cek, all_inv))

            if not per_sales:
                send_message(chat_id, f"✅ Tidak ada piutang di periode {label or (date_from + ' - ' + date_to)}.")
                return

            grand = sum(v["total"] for v in per_sales.values())
            judul = label or f"{date_from} - {date_to}"
            msg = f"💰 *Piutang per Sales - {judul}*\n"
            msg += f"Total piutang: Rp {grand:,.0f}\n\n"
            # urut sales dari piutang terbesar
            for sales in sorted(per_sales, key=lambda s: per_sales[s]["total"], reverse=True):
                d = per_sales[sales]
                msg += f"━━━━━━━━━━\n👤 *{sales}* — Rp {d['total']:,.0f} ({len(d['items'])} inv)\n"
                # urut invoice dari umur terlama
                for it in sorted(d["items"], key=lambda x: (x["umur"] if x["umur"] is not None else -1), reverse=True):
                    umur_txt = f"{it['umur']} hari" if it["umur"] is not None else "?"
                    msg += f"  • {it['number']} | {it['cust']} | Rp {it['owing']:,.0f} | {umur_txt}\n"
            msg += f"\n_Umur dari jatuh tempo sampai hari ini. Sisa tagihan = primeOwing._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap piutang per sales: {str(e)[:120]}")
            print(f"[PIUTANG SALES ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_cek_piutang_customer(host, chat_id, nama_customer):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            from datetime import datetime
            # Ambil SEMUA invoice customer ini, sekaligus minta primeOwing di list (tanpa buka detail satu-satu)
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,transDate,dueDate,primeOwing,retailWpName",
                    "filter.keywords": nama_customer, "sp.pageSize": 100, "sp.page": page}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1
                if page > 50: break  # pengaman, maksimal 5000 invoice

            if not all_inv:
                send_message(chat_id, f"❌ Tidak ditemukan invoice untuk customer '{nama_customer}'. Coba cek ejaan namanya.")
                return

            # Cek apakah primeOwing tersedia di list. Kalau semua None/tidak ada, fallback ke detail.
            ada_owing_di_list = any(isinstance(i, dict) and i.get("primeOwing") is not None for i in all_inv)

            lock = threading.Lock()
            piutang = []
            nama_terdeteksi = set()

            def proses_satu(inv, owing):
                cust = inv.get("retailWpName") or "Tanpa Nama"
                with lock:
                    nama_terdeteksi.add(cust)
                if owing and owing > 0:
                    tgl = inv.get("transDate") or ""
                    due = inv.get("dueDate") or ""
                    umur = None
                    acuan = due or tgl
                    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                        try:
                            dd = datetime.strptime(acuan, fmt); umur = (datetime.now() - dd).days; break
                        except: pass
                    with lock:
                        piutang.append({"number": inv.get("number"), "owing": owing, "due": due, "umur": umur})

            if ada_owing_di_list:
                # cepat: pakai primeOwing dari list langsung
                for inv in all_inv:
                    if isinstance(inv, dict):
                        proses_satu(inv, float(inv.get("primeOwing") or 0))
            else:
                # fallback: buka detail (lebih lambat), tetap proses semua
                def cek(inv):
                    try:
                        r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=12)
                        det = r2.json().get("d", {})
                        proses_satu({**inv, "retailWpName": det.get("retailWpName")}, float(det.get("primeOwing") or 0))
                    except: pass
                with ThreadPoolExecutor(max_workers=6) as ex:
                    list(ex.map(cek, all_inv))

            if not piutang:
                nama_str = ", ".join(list(nama_terdeteksi)[:5])
                send_message(chat_id, f"✅ Customer '{nama_customer}' ({nama_str}) tidak punya piutang. Semua lunas.\n(Dicek dari {len(all_inv)} invoice)")
                return

            piutang.sort(key=lambda x: (x["umur"] if x["umur"] is not None else -1), reverse=True)
            total = sum(p["owing"] for p in piutang)
            nama_str = ", ".join(list(nama_terdeteksi)[:5])
            msg = f"💰 *Piutang Customer: {nama_customer}*\n"
            msg += f"(cocok: {nama_str})\n\n"
            msg += f"Total piutang: Rp {total:,.0f} dari {len(piutang)} invoice (dicek {len(all_inv)} invoice)\n\n"
            for p in piutang[:60]:
                umur_txt = f"{p['umur']} hari" if p["umur"] is not None else "?"
                tempo = f" (JT {p['due']})" if p["due"] else ""
                msg += f"• {p['number']}: Rp {p['owing']:,.0f} — {umur_txt}{tempo}\n"
            if len(piutang) > 60:
                msg += f"... dan {len(piutang)-60} invoice lagi\n"
            msg += f"\n_Sisa tagihan = primeOwing. Umur dari jatuh tempo sampai hari ini._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek piutang customer: {str(e)[:120]}")
            print(f"[PIUTANG CUST ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_bukti_tidak_cocok_per_sales(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            token = get_drive_token()
            # 1. Kumpulkan file Drive: nomor(upper) -> {id, mimeType}
            file_map = {}
            def daftar(folder_id):
                page_token = None
                while True:
                    params = {"q": f"'{folder_id}' in parents and trashed=false",
                              "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000,
                              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                    if page_token: params["pageToken"] = page_token
                    rs = requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                    ds = rs.json()
                    for x in ds.get("files", []):
                        if x.get("mimeType") == "application/vnd.google-apps.folder":
                            daftar(x["id"])
                        else:
                            key = x["name"].rsplit(".", 1)[0].strip().upper()
                            file_map[key] = {"id": x["id"], "mimeType": x.get("mimeType", "image/png")}
                    page_token = ds.get("nextPageToken")
                    if not page_token: break
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false", "fields": "files(id,name,mimeType)",
                        "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    daftar(f["id"])
                else:
                    key = f["name"].rsplit(".", 1)[0].strip().upper()
                    file_map[key] = {"id": f["id"], "mimeType": f.get("mimeType", "image/png")}

            # 2. Ambil invoice periode + nilai
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                all_inv.extend(dd.get("d", []))
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            target = []
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                nomor = (inv.get("number") or "").strip().upper()
                if nomor in file_map:
                    target.append({"id": inv.get("id"), "number": inv.get("number"),
                                   "nilai": float(inv.get("totalAmount") or 0), "file": file_map[nomor]})
            if not target:
                send_message(chat_id, f"Tidak ada invoice {label} yang ada bukti di Drive.")
                return

            # 3. Baca nominal paralel; kumpulkan yang BEDA
            lock = threading.Lock()
            beda = []
            def proses(t):
                try:
                    img = drive_download_file(t["file"]["id"])
                    nominal, _ = baca_nominal_dari_gambar(img, t["file"]["mimeType"])
                    if nominal > 0 and abs(nominal - t["nilai"]) >= 1:
                        with lock:
                            beda.append({"id": t["id"], "number": t["number"], "invoice": t["nilai"], "bukti": nominal})
                except: pass
            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(proses, target))

            if not beda:
                send_message(chat_id, f"✅ Semua bukti bayar {label} yang terbaca COCOK dengan invoice. Tidak ada yang berbeda.")
                return

            # 4. Untuk yang beda, ambil nama sales dari detail
            def ambil_sales(b):
                try:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": b["id"]}, timeout=12)
                    det = r2.json().get("d", {})
                    b["sales"] = det.get("masterSalesmanName") or "Tanpa Sales"
                except:
                    b["sales"] = "?"
            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(ambil_sales, beda))

            # 5. Kelompokkan per sales
            per_sales = {}
            for b in beda:
                per_sales.setdefault(b["sales"], []).append(b)

            judul = label or f"{date_from} - {date_to}"
            msg = f"⚠️ *Bukti Bayar Tidak Cocok per Sales - {judul}*\n"
            msg += f"Total tidak cocok: {len(beda)} invoice\n\n"
            for sales in sorted(per_sales, key=lambda s: len(per_sales[s]), reverse=True):
                items = per_sales[sales]
                msg += f"━━━━━━━━━━\n👤 *{sales}* ({len(items)} invoice)\n"
                for b in items:
                    selisih = b["bukti"] - b["invoice"]
                    msg += f"  • {b['number']}: invoice Rp {b['invoice']:,.0f} vs bukti Rp {b['bukti']:,.0f} (selisih Rp {selisih:,.0f})\n"
            msg += "\n_Nominal foto dibaca AI, selisih kecil bisa karena salah baca digit. Selisih besar perlu dicek manual._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal: {str(e)[:150]}")
            print(f"[BEDA PER SALES ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_cek_nominal_gabungan(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            import re as _re
            h = host if host.startswith("http") else f"https://{host}"
            token = get_drive_token()
            # 1. Kumpulkan SEMUA file bukti di Drive (rekursif) -> list {id, mimeType, nama_tanpa_ext}
            files = []
            def daftar(folder_id):
                page_token = None
                while True:
                    params = {"q": f"'{folder_id}' in parents and trashed=false",
                              "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000,
                              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                    if page_token: params["pageToken"] = page_token
                    rs = requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                    ds = rs.json()
                    for x in ds.get("files", []):
                        if x.get("mimeType") == "application/vnd.google-apps.folder":
                            daftar(x["id"])
                        else:
                            files.append({"id": x["id"], "mimeType": x.get("mimeType", "image/png"),
                                          "nama": x["name"].rsplit(".", 1)[0].strip()})
                    page_token = ds.get("nextPageToken")
                    if not page_token: break
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false", "fields": "files(id,name,mimeType)",
                        "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    daftar(f["id"])
                else:
                    files.append({"id": f["id"], "mimeType": f.get("mimeType", "image/png"),
                                  "nama": f["name"].rsplit(".", 1)[0].strip()})

            # 2. Peta nilai invoice periode: nomor(UPPER) -> nilai
            nilai_map = {}
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount,subTotal", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                for inv in dd.get("d", []):
                    if isinstance(inv, dict):
                        nomor = (inv.get("number") or "").strip().upper()
                        if nomor:
                            nilai_map[nomor] = float(inv.get("totalAmount") or inv.get("subTotal") or 0)
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            # 3. Pola nomor invoice: SI.YYYY.MM.NNNNN (fleksibel jumlah digit akhir)
            pola = _re.compile(r"SI\.\d{4}\.\d{2}\.\d+", _re.IGNORECASE)

            # 4. Untuk tiap file, ekstrak SEMUA nomor invoice di namanya.
            #    Ambil hanya file yang minimal satu nomornya ada di periode ini.
            target = []
            for f in files:
                nomor_ditemukan = [n.upper() for n in pola.findall(f["nama"])]
                # unik, jaga urutan
                seen = set(); nomor_unik = []
                for n in nomor_ditemukan:
                    if n not in seen:
                        seen.add(n); nomor_unik.append(n)
                if not nomor_unik:
                    continue
                # relevan kalau ada minimal satu nomor yang termasuk periode
                if not any(n in nilai_map for n in nomor_unik):
                    continue
                target.append({"file": f, "nomor": nomor_unik})

            # Hanya proses file GABUNGAN (2+ nomor). File satuan sudah ditangani cek_nominal_massal,
            # tapi kita ikutkan juga file satuan yang relevan agar satu pengecekan menyeluruh.
            if not target:
                send_message(chat_id, f"Tidak ada file bukti untuk {label or (date_from + ' - ' + date_to)} yang nomornya cocok periode ini.")
                return

            lock = threading.Lock()
            hasil = []
            def proses(t):
                f = t["file"]; nomor_unik = t["nomor"]
                # total invoice = jumlah nilai semua nomor yang dikenal
                total_inv = 0.0
                dikenal = []
                tak_dikenal = []
                for n in nomor_unik:
                    if n in nilai_map:
                        total_inv += nilai_map[n]; dikenal.append(n)
                    else:
                        tak_dikenal.append(n)
                try:
                    img = drive_download_file(f["id"])
                    nominal, _ = baca_nominal_dari_gambar(img, f["mimeType"])
                except Exception:
                    nominal = -1
                with lock:
                    hasil.append({"nama": f["nama"], "nomor": nomor_unik, "gabungan": len(nomor_unik) > 1,
                                  "dikenal": dikenal, "tak_dikenal": tak_dikenal,
                                  "total_invoice": total_inv, "bukti": nominal})
            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(proses, target))

            # 5. Susun laporan (fokus ke yang gabungan; toleransi Rp 0 = harus sama persis)
            gab = [x for x in hasil if x["gabungan"]]
            cocok = [x for x in gab if x["bukti"] >= 0 and x["bukti"] == x["total_invoice"]]
            beda = [x for x in gab if x["bukti"] >= 0 and x["bukti"] != x["total_invoice"]]
            gagal = [x for x in gab if x["bukti"] < 0]

            judul = label or f"{date_from} - {date_to}"
            msg = f"🧾 *Cek Bukti Bayar Gabungan - {judul}*\n"
            msg += f"File bukti gabungan (2+ invoice) ditemukan: {len(gab)}\n"
            msg += f"✅ Cocok: {len(cocok)} | ⚠️ Beda: {len(beda)} | ❓ Gagal baca: {len(gagal)}\n\n"
            if beda:
                msg += "*⚠️ Total invoice ≠ nominal bukti:*\n"
                for x in beda:
                    selisih = x["bukti"] - x["total_invoice"]
                    msg += f"• {' + '.join(x['dikenal'])}\n"
                    msg += f"   total invoice Rp {x['total_invoice']:,.0f} vs bukti Rp {x['bukti']:,.0f} (selisih Rp {selisih:,.0f})\n"
                    if x["tak_dikenal"]:
                        msg += f"   _catatan: {', '.join(x['tak_dikenal'])} tidak ada di periode ini, tidak ikut dijumlah_\n"
                msg += "\n"
            if cocok:
                msg += f"*✅ Cocok ({len(cocok)}):*\n"
                for x in cocok[:20]:
                    msg += f"• {' + '.join(x['dikenal'])} = Rp {x['total_invoice']:,.0f}\n"
                if len(cocok) > 20: msg += f"_...dan {len(cocok)-20} lagi_\n"
                msg += "\n"
            if gagal:
                msg += "*❓ Gagal baca foto:* " + ", ".join(" + ".join(x["dikenal"]) for x in gagal[:10]) + "\n\n"
            if not gab:
                msg += "_Tidak ada file bukti gabungan (nama file dengan 2+ nomor invoice) di periode ini._\n\n"
            msg += "_Bukti gabungan dikenali dari nama file yang memuat 2+ nomor invoice. Total nilai invoice dijumlahkan lalu dicocokkan persis dengan nominal bukti._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek bukti gabungan: {str(e)[:150]}")
            print(f"[NOMINAL GABUNGAN ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_cek_nominal_massal(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            token = get_drive_token()
            # 1. Kumpulkan file Drive: map nomor_invoice(upper) -> {id, mimeType}
            file_map = {}
            q_root = f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false"
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": q_root, "fields": "files(id,name,mimeType)", "pageSize": 1000,
                        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            def daftar(folder_id):
                page_token = None
                while True:
                    params = {"q": f"'{folder_id}' in parents and trashed=false",
                              "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000,
                              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                    if page_token: params["pageToken"] = page_token
                    rs = requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                    ds = rs.json()
                    for x in ds.get("files", []):
                        if x.get("mimeType") == "application/vnd.google-apps.folder":
                            daftar(x["id"])
                        else:
                            key = x["name"].rsplit(".", 1)[0].strip().upper()
                            file_map[key] = {"id": x["id"], "mimeType": x.get("mimeType", "image/png")}
                    page_token = ds.get("nextPageToken")
                    if not page_token: break
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    daftar(f["id"])
                else:
                    key = f["name"].rsplit(".", 1)[0].strip().upper()
                    file_map[key] = {"id": f["id"], "mimeType": f.get("mimeType", "image/png")}

            # 2. Ambil invoice periode + nilainya
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                all_inv.extend(dd.get("d", []))
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            # 3. Hanya invoice yang ADA buktinya
            target = []
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                nomor = (inv.get("number") or "").strip().upper()
                if nomor in file_map:
                    target.append({"number": inv.get("number"), "nilai": float(inv.get("totalAmount") or 0),
                                   "file": file_map[nomor]})
            if not target:
                send_message(chat_id, f"Tidak ada invoice {label} yang punya file bukti di Drive.")
                return

            # 4. Baca nominal tiap foto secara PARALEL
            lock = threading.Lock()
            hasil = []
            def proses(t):
                try:
                    img = drive_download_file(t["file"]["id"])
                    nominal, _ = baca_nominal_dari_gambar(img, t["file"]["mimeType"])
                    with lock:
                        hasil.append({"number": t["number"], "invoice": t["nilai"], "bukti": nominal})
                except Exception as e:
                    with lock:
                        hasil.append({"number": t["number"], "invoice": t["nilai"], "bukti": -1})
            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(proses, target))

            # 5. Susun laporan
            cocok, beda, gagal = [], [], []
            for x in hasil:
                if x["bukti"] < 0:
                    gagal.append(x)
                elif abs(x["bukti"] - x["invoice"]) < 1:
                    cocok.append(x)
                else:
                    beda.append(x)
            judul = label or f"{date_from} - {date_to}"
            msg = f"🔍 *Cek Nominal Bukti vs Invoice - {judul}*\n"
            msg += f"Dicek: {len(hasil)} invoice (yang ada bukti)\n"
            msg += f"✅ Cocok: {len(cocok)} | ⚠️ Beda: {len(beda)} | ❓ Gagal baca: {len(gagal)}\n\n"
            if beda:
                msg += "*⚠️ Nominal BERBEDA:*\n"
                for x in beda:
                    msg += f"• {x['number']}: invoice Rp {x['invoice']:,.0f} vs bukti Rp {x['bukti']:,.0f}\n"
                msg += "\n"
            if gagal:
                msg += "*❓ Gagal baca foto:* " + ", ".join(x["number"] for x in gagal) + "\n\n"
            if cocok:
                msg += f"*✅ Cocok ({len(cocok)}):* " + ", ".join(x["number"] for x in cocok[:30])
                if len(cocok) > 30: msg += f" ... +{len(cocok)-30} lagi"
            msg += "\n\n_Nominal foto dibaca AI, bisa kurang akurat untuk foto buram. 'Beda' bisa wajar (bayar sebagian, gabung invoice, dll)._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek nominal massal: {str(e)[:150]}")
            print(f"[NOMINAL MASSAL ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_bukti_belum_ada_per_sales(host, chat_id, date_from, date_to, label="", hanya_lunas=True):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            token = get_drive_token()
            # 1. Kumpulkan semua nama file bukti di Drive (semua subfolder) -> set nomor invoice (UPPER)
            nama_file_drive = set()
            def daftar(folder_id):
                page_token = None
                while True:
                    params = {"q": f"'{folder_id}' in parents and trashed=false",
                              "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000,
                              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                    if page_token: params["pageToken"] = page_token
                    rs = requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                    ds = rs.json()
                    for x in ds.get("files", []):
                        if x.get("mimeType") == "application/vnd.google-apps.folder":
                            daftar(x["id"])
                        else:
                            nama_file_drive.add(x["name"].rsplit(".", 1)[0].strip().upper())
                    page_token = ds.get("nextPageToken")
                    if not page_token: break
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false", "fields": "files(id,name,mimeType)",
                        "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    daftar(f["id"])
                else:
                    nama_file_drive.add(f["name"].rsplit(".", 1)[0].strip().upper())

            # 2. Ambil semua invoice periode
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount,statusName", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                all_inv.extend(dd.get("d", []))
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            # 3. Cari yang BELUM ada bukti di Drive. Kalau hanya_lunas, ambil yang status Lunas saja.
            belum = []
            n_lunas_total = 0
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                status = (inv.get("statusName") or "").upper()
                is_lunas = ("LUNAS" in status or "PAID" in status or "CLOSE" in status)
                if hanya_lunas and not is_lunas:
                    continue
                if is_lunas:
                    n_lunas_total += 1
                nomor = (inv.get("number") or "").strip().upper()
                if nomor and nomor not in nama_file_drive:
                    belum.append({"id": inv.get("id"), "number": inv.get("number")})

            if not belum:
                if hanya_lunas:
                    send_message(chat_id, f"✅ Semua invoice LUNAS di {label or (date_from + ' - ' + date_to)} sudah ada bukti bayarnya di Drive. Tidak ada yang belum.")
                else:
                    send_message(chat_id, f"✅ Semua invoice {label or (date_from + ' - ' + date_to)} sudah ada bukti bayarnya di Drive. Tidak ada yang belum.")
                return

            # 4. Ambil nama sales tiap invoice yang belum ada bukti (dari detail)
            lock = threading.Lock()
            def ambil_sales(b):
                detail = None
                for attempt in range(4):
                    try:
                        r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": b["id"]}, timeout=15)
                        d = r2.json()
                        if d.get("s") and d.get("d"):
                            detail = d["d"]; break
                    except Exception: pass
                    import time as _t; _t.sleep(0.4 * (attempt + 1))
                if detail is None:
                    b["sales"] = "(gagal baca)"
                    b["nilai"] = 0.0
                    return
                b["sales"] = _resolve_sales_name(detail)
                b["nilai"] = _resolve_nilai_invoice(detail)
                b["cust"] = detail.get("retailWpName") or detail.get("customerName") or "Tanpa Nama"
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(ambil_sales, belum))

            # 5. Kelompokkan per sales
            per_sales = {}
            for b in belum:
                per_sales.setdefault(b.get("sales", "Tanpa Sales"), []).append(b)

            total_nilai = sum(b.get("nilai", 0) for b in belum)
            judul = label or f"{date_from} - {date_to}"
            if hanya_lunas:
                msg = f"📌 *Invoice LUNAS Tapi Belum Ada Bukti - {judul}*\n"
                msg += f"Invoice lunas belum ada bukti: {len(belum)} dari {n_lunas_total} invoice lunas | Nilai: Rp {total_nilai:,.0f}\n\n"
            else:
                msg = f"📌 *Bukti Bayar BELUM ADA per Sales - {judul}*\n"
                msg += f"Total invoice belum ada bukti: {len(belum)} | Nilai: Rp {total_nilai:,.0f}\n\n"
            # urut sales dari jumlah invoice belum ada bukti terbanyak
            for sales in sorted(per_sales, key=lambda s: len(per_sales[s]), reverse=True):
                items = per_sales[sales]
                subtotal = sum(x.get("nilai", 0) for x in items)
                msg += f"━━━━━━━━━━\n👤 *{sales}* — {len(items)} invoice | Rp {subtotal:,.0f}\n"
                # urut invoice dari nilai terbesar
                for x in sorted(items, key=lambda i: i.get("nilai", 0), reverse=True):
                  msg += f"  • {x['number']} | {x.get('cust','-')} | Rp {x.get('nilai',0):,.0f}\n"
            if hanya_lunas:
                msg += "\n_Yang ditampilkan: invoice berstatus LUNAS tapi nomornya tidak ditemukan sebagai nama file bukti di Google Drive. Ini yang perlu ditagih buktinya ke sales._"
            else:
                msg += "\n_Belum ada bukti = nomor invoice tidak ditemukan sebagai nama file di Google Drive._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek bukti belum ada per sales: {str(e)[:150]}")
            print(f"[BUKTI BELUM ADA PER SALES ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_hitung_lunas_metode(host, chat_id, date_from, date_to, label=""):
    """Hitung invoice LUNAS satu periode, dipisah metode bayar Tunai/Cash vs Transfer.
    Metode dibaca dari DETAIL invoice, field receiptHistory -> historyPaymentName.
    Nilainya: 'Tunai' -> Tunai, 'Transfer Bank' -> Transfer. receiptHistory di Accurate
    PTM berbentuk STRING gaya Python (kutip tunggal, True/False/None) sehingga di-parse
    pakai ast.literal_eval, bukan json.loads. Invoice tanpa histori terbaca masuk
    'tidak diketahui'. Perlu 1 detail call per invoice (mirip tool sales), jadi lambat
    untuk periode besar -> worker sedikit + jeda supaya Accurate tidak memutus koneksi."""
    def run():
        try:
            import time as _t
            h = host if host.startswith("http") else f"https://{host}"

            # 1. Ambil semua invoice periode (dengan field nilai + status)
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount,salesAmount,subTotal,statusName",
                    "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                all_inv.extend(dd.get("d", []))
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            # 2. Ambil hanya invoice LUNAS
            def nilai_dari_list(inv):
                for k in ("totalAmount", "salesAmount", "subTotal"):
                    v = inv.get(k)
                    try:
                        fv = float(v)
                        if fv != 0:
                            return fv
                    except: pass
                return 0.0

            lunas = []
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                status = (inv.get("statusName") or "").upper()
                if "LUNAS" in status or "PAID" in status or "CLOSE" in status:
                    lunas.append({
                        "id": inv.get("id"),
                        "number": inv.get("number"),
                        "nilai": nilai_dari_list(inv)
                    })

            if not lunas:
                send_message(chat_id, f"Tidak ada invoice LUNAS di {label or (date_from + ' - ' + date_to)}.")
                return

            # 3. Untuk tiap invoice lunas, baca metode bayar dari DETAIL invoice
            #    (receiptHistory -> historyPaymentName). Beban ringan: worker sedikit + jeda.
            import ast as _ast
            lock = threading.Lock()

            def _parse_receipt_history(raw):
                """receiptHistory di PTM = STRING gaya Python. Parse -> list dict.
                Kalau parse gagal (mis. string ke-truncate), fallback deteksi substring."""
                if not raw:
                    return []
                if isinstance(raw, list):
                    return raw
                if isinstance(raw, str):
                    try:
                        val = _ast.literal_eval(raw)
                        return val if isinstance(val, list) else []
                    except (ValueError, SyntaxError):
                        return raw  # kembalikan string mentah utk fallback substring
                return []

            def _metode_dari_nama(nama):
                n = (nama or "").lower()
                if any(k in n for k in ("tunai", "cash", "kas")):
                    return "tunai"
                if "transfer" in n or "bank" in n:
                    return "transfer"
                return None

            def klasifikasi_metode(item):
                iid = item["id"]
                detail = None
                for attempt in range(5):
                    try:
                        rr = requests.get(f"{h}/accurate/api/sales-invoice/detail.do",
                            headers=accurate_headers(), params={"id": iid}, timeout=30)
                        dj = rr.json()
                        if dj.get("s") and dj.get("d"):
                            detail = dj["d"]; break
                    except Exception:
                        pass
                    _t.sleep(0.6 * (attempt + 1))
                if detail is None:
                    item["gagal_baca"] = True
                    return "tidak_diketahui"

                # simpan customer & sales dari detail yang sama (tanpa call tambahan)
                cust = detail.get("customer")
                cname = None
                if isinstance(cust, dict): cname = cust.get("name")
                elif isinstance(cust, list) and cust and isinstance(cust[0], dict): cname = cust[0].get("name")
                item["customer"] = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
                item["sales"] = _resolve_sales_name(detail)

                histori = _parse_receipt_history(detail.get("receiptHistory"))

                # Fallback: parse gagal, histori masih berupa string -> cek substring
                if isinstance(histori, str):
                    m = _metode_dari_nama(histori)
                    return m if m else "tidak_diketahui"

                metode = set()
                for hh_ in histori:
                    if not isinstance(hh_, dict):
                        continue
                    if hh_.get("historyIsVoidReceipt"):
                        continue
                    if hh_.get("approvalStatus") not in (None, "APPROVED"):
                        continue
                    m = _metode_dari_nama(hh_.get("historyPaymentName"))
                    if m:
                        metode.add(m)

                if not metode: return "tidak_diketahui"
                if metode == {"tunai"}: return "tunai"
                if metode == {"transfer"}: return "transfer"
                return "campuran"

            def proses(item):
                item["metode"] = klasifikasi_metode(item)
                _t.sleep(0.05)

            gagal_retry = []
            def proses_safe(item):
                try:
                    proses(item)
                    if item.get("metode") == "tidak_diketahui" and item.get("gagal_baca"):
                        with lock: gagal_retry.append(item)
                except Exception:
                    with lock: gagal_retry.append(item)

            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(proses_safe, lunas))
            # putaran ke-2 untuk yang gagal baca detail, 2 worker
            if gagal_retry:
                ulang = list(gagal_retry); gagal_retry.clear()
                for it in ulang: it.pop("gagal_baca", None)
                with ThreadPoolExecutor(max_workers=2) as ex:
                    list(ex.map(proses_safe, ulang))
            # putaran ke-3 (terakhir) untuk yang MASIH gagal, 1 worker berurutan
            if gagal_retry:
                ulang = list(gagal_retry); gagal_retry.clear()
                for it in ulang:
                    it.pop("gagal_baca", None)
                    proses(it)

            # 4. Rekap per metode
            grup = {"tunai": [], "transfer": [], "campuran": [], "tidak_diketahui": []}
            for item in lunas:
                grup.get(item.get("metode", "tidak_diketahui"), grup["tidak_diketahui"]).append(item)

            def blok(judul_grup, key, emoji):
                items = grup[key]
                total = sum(x.get("nilai", 0) for x in items)
                return len(items), total, f"{emoji} *{judul_grup}*: {len(items)} invoice | Rp {total:,.0f}\n"

            n_tunai, t_tunai, s_tunai = blok("Tunai / Cash", "tunai", "💵")
            n_tf, t_tf, s_tf = blok("Transfer", "transfer", "🏦")
            n_cmp, t_cmp, s_cmp = blok("Campuran (tunai + transfer)", "campuran", "🔀")
            n_tt, t_tt, s_tt = blok("Tidak diketahui", "tidak_diketahui", "❓")

            judul = label or f"{date_from} - {date_to}"
            total_semua = sum(x.get("nilai", 0) for x in lunas)
            msg = f"💰 *Pembayaran Lunas per Metode - {judul}*\n"
            msg += f"Total invoice lunas: {len(lunas)} | Nilai: Rp {total_semua:,.0f}\n\n"
            msg += s_tunai + s_tf
            if n_cmp: msg += s_cmp
            if n_tt: msg += s_tt
            if n_tt:
                msg += "\n_'Tidak diketahui' = detail/receiptHistory invoice gagal dibaca / metode di luar Tunai & Transfer. Lihat daftar di bawah._"
            else:
                msg += "\n_Metode dibaca dari receiptHistory (historyPaymentName) tiap invoice._"
            send_message(chat_id, msg)

            # Kirim daftar rinci secara bertahap (hindari batas panjang Telegram)
            def kirim_daftar(judul_blok, items, with_sales):
                if not items:
                    return
                items_sorted = sorted(items, key=lambda x: x.get("nilai", 0), reverse=True)
                baris = [judul_blok]
                for it in items_sorted:
                    cust = it.get("customer", "?")
                    line = f"• {it.get('number','?')} | {cust} | Rp {it.get('nilai',0):,.0f}"
                    if with_sales:
                        line += f" | sales: {it.get('sales','?')}"
                    baris.append(line)
                # potong per ~40 baris supaya muat
                blok_kirim = []
                for b in baris:
                    blok_kirim.append(b)
                    if len(blok_kirim) >= 41:
                        send_message(chat_id, "\n".join(blok_kirim))
                        blok_kirim = [judul_blok + " (lanjutan)"]
                if len(blok_kirim) > 1:
                    send_message(chat_id, "\n".join(blok_kirim))

            kirim_daftar(f"💵 *Daftar Tunai/Cash ({n_tunai} invoice)*", grup["tunai"], with_sales=True)
            if n_cmp:
                kirim_daftar(f"🔀 *Daftar Campuran ({n_cmp} invoice)*", grup["campuran"], with_sales=True)
            if n_tt:
                kirim_daftar(f"❓ *Daftar Tidak Diketahui ({n_tt} invoice)*", grup["tidak_diketahui"], with_sales=True)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung lunas per metode: {str(e)[:150]}")
            print(f"[LUNAS METODE ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_rekap_bayar_sales(host, chat_id, date_from, date_to, label=""):
    """Rekap pembayaran invoice LUNAS, dikelompokkan PER BULAN -> PER SALES -> per metode
    (Tunai vs Transfer), lengkap daftar nomor invoice. Metode dibaca dari receiptHistory
    (historyPaymentName), sales dari _resolve_sales_name, keduanya dari 1 detail call."""
    def run():
        try:
            import time as _t
            import ast as _ast
            from datetime import datetime as _dt
            h = host if host.startswith("http") else f"https://{host}"

            def parse_tgl(s):
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try: return _dt.strptime((s or "").split(" ")[0], fmt)
                    except: pass
                return None

            NAMA_BULAN = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                          "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
            def label_bulan(bkey):  # bkey = "YYYY-MM"
                try:
                    th, bl = bkey.split("-")
                    return f"{NAMA_BULAN[int(bl)]} {th}"
                except: return bkey

            def _parse_receipt_history(raw):
                if not raw: return []
                if isinstance(raw, list): return raw
                if isinstance(raw, str):
                    try:
                        val = _ast.literal_eval(raw)
                        return val if isinstance(val, list) else []
                    except (ValueError, SyntaxError):
                        return raw
                return []

            def _metode_dari_nama(nama):
                n = (nama or "").lower()
                if any(k in n for k in ("tunai", "cash", "kas")): return "tunai"
                if "transfer" in n or "bank" in n: return "transfer"
                return None

            # 1. Ambil semua invoice periode
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount,salesAmount,subTotal,statusName,transDate",
                    "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                dd = rr.json()
                if not dd.get("s"): break
                all_inv.extend(dd.get("d", []))
                sp = dd.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            def nilai_dari_list(inv):
                for k in ("totalAmount", "salesAmount", "subTotal"):
                    try:
                        fv = float(inv.get(k))
                        if fv != 0: return fv
                    except: pass
                return 0.0

            lunas = []
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                status = (inv.get("statusName") or "").upper()
                if "LUNAS" in status or "PAID" in status or "CLOSE" in status:
                    lunas.append({"id": inv.get("id"), "number": inv.get("number"),
                                  "nilai": nilai_dari_list(inv), "transDate": inv.get("transDate")})

            if not lunas:
                send_message(chat_id, f"Tidak ada invoice LUNAS di {label or (date_from + ' - ' + date_to)}.")
                return

            send_message(chat_id, f"⏳ Memproses {len(lunas)} invoice lunas untuk rekap per sales & metode... (bisa beberapa menit)")

            lock = threading.Lock()

            def proses(item):
                iid = item["id"]
                detail = None
                for attempt in range(5):
                    try:
                        rr = requests.get(f"{h}/accurate/api/sales-invoice/detail.do",
                            headers=accurate_headers(), params={"id": iid}, timeout=30)
                        dj = rr.json()
                        if dj.get("s") and dj.get("d"):
                            detail = dj["d"]; break
                    except Exception:
                        pass
                    _t.sleep(0.6 * (attempt + 1))
                if detail is None:
                    item["gagal_baca"] = True
                    item["sales"] = "Tanpa Sales"; item["metode"] = "tidak_diketahui"
                    return
                item["sales"] = _resolve_sales_name(detail)
                tgl = parse_tgl(detail.get("transDate") or item.get("transDate"))
                item["bulan"] = tgl.strftime("%Y-%m") if tgl else "????-??"
                histori = _parse_receipt_history(detail.get("receiptHistory"))
                if isinstance(histori, str):
                    m = _metode_dari_nama(histori)
                    item["metode"] = m or "tidak_diketahui"; return
                metode = set()
                for hh_ in histori:
                    if not isinstance(hh_, dict): continue
                    if hh_.get("historyIsVoidReceipt"): continue
                    if hh_.get("approvalStatus") not in (None, "APPROVED"): continue
                    m = _metode_dari_nama(hh_.get("historyPaymentName"))
                    if m: metode.add(m)
                if not metode: item["metode"] = "tidak_diketahui"
                elif metode == {"tunai"}: item["metode"] = "tunai"
                elif metode == {"transfer"}: item["metode"] = "transfer"
                else: item["metode"] = "campuran"
                _t.sleep(0.05)

            gagal_retry = []
            def proses_safe(item):
                try:
                    proses(item)
                    if item.get("gagal_baca"):
                        with lock: gagal_retry.append(item)
                except Exception:
                    with lock: gagal_retry.append(item)

            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(proses_safe, lunas))
            if gagal_retry:
                ulang = list(gagal_retry); gagal_retry.clear()
                for it in ulang: it.pop("gagal_baca", None)
                with ThreadPoolExecutor(max_workers=2) as ex:
                    list(ex.map(proses_safe, ulang))
            if gagal_retry:
                ulang = list(gagal_retry); gagal_retry.clear()
                for it in ulang:
                    it.pop("gagal_baca", None)
                    proses(it)

            # 2. Kelompokkan: bulan -> sales -> metode -> list item
            data = {}  # bulan -> sales -> {"tunai":[], "transfer":[], "campuran":[], "tidak_diketahui":[]}
            for it in lunas:
                b = it.get("bulan", "????-??")
                s = it.get("sales", "Tanpa Sales")
                m = it.get("metode", "tidak_diketahui")
                data.setdefault(b, {}).setdefault(s, {"tunai": [], "transfer": [], "campuran": [], "tidak_diketahui": []})
                data[b][s][m].append(it)

            judul = label or f"{date_from} - {date_to}"
            n_bulan = len(data)
            head = f"📊 *Rekap Pembayaran per Sales - {judul}*\n"
            head += f"Total invoice lunas: {len(lunas)} | {n_bulan} bulan\n"
            send_message(chat_id, head)

            EMO = {"tunai": "💵", "transfer": "🏦", "campuran": "🔀", "tidak_diketahui": "❓"}
            LBL = {"tunai": "Tunai", "transfer": "Transfer", "campuran": "Campuran", "tidak_diketahui": "Tidak diketahui"}

            # 3. Kirim per bulan (tiap bulan bisa jadi beberapa pesan)
            for bkey in sorted(data.keys()):
                sales_map = data[bkey]
                # ringkasan bulan
                ring = [f"🗓️ *{label_bulan(bkey)}*"]
                for sname in sorted(sales_map.keys()):
                    g = sales_map[sname]
                    nt = len(g["tunai"]); tt = sum(x["nilai"] for x in g["tunai"])
                    ntf = len(g["transfer"]); ttf = sum(x["nilai"] for x in g["transfer"])
                    extra = len(g["campuran"]) + len(g["tidak_diketahui"])
                    baris = f"• *{sname}*: 💵 {nt} inv Rp {tt:,.0f} | 🏦 {ntf} inv Rp {ttf:,.0f}"
                    if extra: baris += f" | +{extra} lainnya"
                    ring.append(baris)
                send_message(chat_id, "\n".join(ring))

                # rincian nomor invoice per sales
                for sname in sorted(sales_map.keys()):
                    g = sales_map[sname]
                    baris = [f"🧾 *{label_bulan(bkey)} — {sname}*"]
                    for mkey in ("tunai", "transfer", "campuran", "tidak_diketahui"):
                        items = g[mkey]
                        if not items: continue
                        subtot = sum(x["nilai"] for x in items)
                        baris.append(f"{EMO[mkey]} _{LBL[mkey]}_ ({len(items)} inv | Rp {subtot:,.0f}):")
                        for it in sorted(items, key=lambda x: x["nilai"], reverse=True):
                            baris.append(f"   {it.get('number','?')} — Rp {it.get('nilai',0):,.0f}")
                        # potong tiap ~40 baris
                        if len(baris) >= 40:
                            send_message(chat_id, "\n".join(baris))
                            baris = [f"🧾 *{label_bulan(bkey)} — {sname}* (lanjutan)"]
                    if len(baris) > 1:
                        send_message(chat_id, "\n".join(baris))

            send_message(chat_id, "✅ Rekap selesai.")
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap bayar per sales: {str(e)[:150]}")
            print(f"[REKAP BAYAR SALES ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def _lunas_dengan_metode(h, date_from, date_to):
    """Helper bersama: ambil invoice LUNAS periode, baca metode (tunai/transfer/dll) +
    sales + tanggal dari detail (retry 3 lapis). Kembalikan list dict item."""
    import time as _t
    import ast as _ast
    from datetime import datetime as _dt

    def parse_tgl(s):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try: return _dt.strptime((s or "").split(" ")[0], fmt)
            except: pass
        return None

    def _parse_receipt_history(raw):
        if not raw: return []
        if isinstance(raw, list): return raw
        if isinstance(raw, str):
            try:
                val = _ast.literal_eval(raw)
                return val if isinstance(val, list) else []
            except (ValueError, SyntaxError):
                return raw
        return []

    def _metode_dari_nama(nama):
        n = (nama or "").lower()
        if any(k in n for k in ("tunai", "cash", "kas")): return "tunai"
        if "transfer" in n or "bank" in n: return "transfer"
        return None

    all_inv = []
    page = 1
    while True:
        params = {"fields": "id,number,totalAmount,salesAmount,subTotal,statusName,transDate",
            "sp.pageSize": 200, "sp.page": page,
            "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
        rr = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
        dd = rr.json()
        if not dd.get("s"): break
        all_inv.extend(dd.get("d", []))
        sp = dd.get("sp", {})
        if page >= sp.get("pageCount", 1): break
        page += 1

    def nilai_dari_list(inv):
        for k in ("totalAmount", "salesAmount", "subTotal"):
            try:
                fv = float(inv.get(k))
                if fv != 0: return fv
            except: pass
        return 0.0

    lunas = []
    for inv in all_inv:
        if not isinstance(inv, dict): continue
        status = (inv.get("statusName") or "").upper()
        if "LUNAS" in status or "PAID" in status or "CLOSE" in status:
            lunas.append({"id": inv.get("id"), "number": inv.get("number"),
                          "nilai": nilai_dari_list(inv), "transDate": inv.get("transDate")})

    lock = threading.Lock()
    def proses(item):
        iid = item["id"]
        detail = None
        for attempt in range(5):
            try:
                rr = requests.get(f"{h}/accurate/api/sales-invoice/detail.do",
                    headers=accurate_headers(), params={"id": iid}, timeout=30)
                dj = rr.json()
                if dj.get("s") and dj.get("d"):
                    detail = dj["d"]; break
            except Exception:
                pass
            _t.sleep(0.6 * (attempt + 1))
        if detail is None:
            item["gagal_baca"] = True
            item["sales"] = "Tanpa Sales"; item["metode"] = "tidak_diketahui"
            item["bulan"] = "????-??"; item["tgl"] = None
            return
        item["sales"] = _resolve_sales_name(detail)
        cust = detail.get("customer")
        cname = None
        if isinstance(cust, dict): cname = cust.get("name")
        elif isinstance(cust, list) and cust and isinstance(cust[0], dict): cname = cust[0].get("name")
        item["cust"] = detail.get("retailWpName") or detail.get("customerName") or cname or "Tanpa Nama"
        tgl = parse_tgl(detail.get("transDate") or item.get("transDate"))
        item["tgl"] = tgl
        item["bulan"] = tgl.strftime("%Y-%m") if tgl else "????-??"
        histori = _parse_receipt_history(detail.get("receiptHistory"))
        if isinstance(histori, str):
            m = _metode_dari_nama(histori)
            item["metode"] = m or "tidak_diketahui"; return
        metode = set()
        for hh_ in histori:
            if not isinstance(hh_, dict): continue
            if hh_.get("historyIsVoidReceipt"): continue
            if hh_.get("approvalStatus") not in (None, "APPROVED"): continue
            m = _metode_dari_nama(hh_.get("historyPaymentName"))
            if m: metode.add(m)
        if not metode: item["metode"] = "tidak_diketahui"
        elif metode == {"tunai"}: item["metode"] = "tunai"
        elif metode == {"transfer"}: item["metode"] = "transfer"
        else: item["metode"] = "campuran"
        _t.sleep(0.05)

    gagal_retry = []
    def proses_safe(item):
        try:
            proses(item)
            if item.get("gagal_baca"):
                with lock: gagal_retry.append(item)
        except Exception:
            with lock: gagal_retry.append(item)

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(proses_safe, lunas))
    if gagal_retry:
        ulang = list(gagal_retry); gagal_retry.clear()
        for it in ulang: it.pop("gagal_baca", None)
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(proses_safe, ulang))
    if gagal_retry:
        ulang = list(gagal_retry); gagal_retry.clear()
        for it in ulang:
            it.pop("gagal_baca", None)
            proses(it)
    return lunas


def tool_rekap_cash(host, chat_id, date_from, date_to, label=""):
    """Rekap total uang CASH (Tunai) diterima, dipecah PER MINGGU dan PER BULAN.
    Cash tidak perlu bukti (uang langsung diterima)."""
    def run():
        try:
            from datetime import datetime as _dt
            h = host if host.startswith("http") else f"https://{host}"
            NAMA_BULAN = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                          "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
            def label_bulan(bkey):
                try:
                    th, bl = bkey.split("-"); return f"{NAMA_BULAN[int(bl)]} {th}"
                except: return bkey

            lunas = _lunas_dengan_metode(h, date_from, date_to)
            cash = [it for it in lunas if it.get("metode") == "tunai"]
            if not cash:
                send_message(chat_id, f"Tidak ada invoice TUNAI/cash di {label or (date_from + ' - ' + date_to)}.")
                return

            total_cash = sum(x["nilai"] for x in cash)
            judul = label or f"{date_from} - {date_to}"
            head = f"💵 *Rekap Uang Cash Diterima - {judul}*\n"
            head += f"Total invoice tunai: {len(cash)} | Total cash: Rp {total_cash:,.0f}\n"
            send_message(chat_id, head)

            # PER BULAN
            per_bulan = {}
            for it in cash:
                per_bulan.setdefault(it.get("bulan", "????-??"), []).append(it)
            msg_b = ["🗓️ *Per Bulan:*"]
            for bkey in sorted(per_bulan):
                items = per_bulan[bkey]
                msg_b.append(f"• {label_bulan(bkey)}: {len(items)} inv | Rp {sum(x['nilai'] for x in items):,.0f}")
            send_message(chat_id, "\n".join(msg_b))

            # PER MINGGU (ISO week: tahun-Www, Senin-Minggu)
            per_minggu = {}
            tanpa_tgl = []
            for it in cash:
                tgl = it.get("tgl")
                if tgl is None:
                    tanpa_tgl.append(it); continue
                iso = tgl.isocalendar()  # (year, week, weekday)
                # rentang tanggal minggu itu
                senin = tgl.fromordinal(tgl.toordinal() - (tgl.weekday()))
                minggu = tgl.fromordinal(senin.toordinal() + 6)
                wkey = f"{iso[0]}-W{iso[1]:02d}"
                per_minggu.setdefault(wkey, {"items": [], "senin": senin, "minggu": minggu})
                per_minggu[wkey]["items"].append(it)
            msg_w = ["📆 *Per Minggu (Senin–Minggu):*"]
            for wkey in sorted(per_minggu):
                d = per_minggu[wkey]
                rng = f"{d['senin'].strftime('%d/%m')}–{d['minggu'].strftime('%d/%m/%Y')}"
                msg_w.append(f"• Minggu {wkey.split('-W')[1]} ({rng}): {len(d['items'])} inv | Rp {sum(x['nilai'] for x in d['items']):,.0f}")
            if tanpa_tgl:
                msg_w.append(f"• (tanggal tidak terbaca): {len(tanpa_tgl)} inv | Rp {sum(x['nilai'] for x in tanpa_tgl):,.0f}")
            # potong tiap ~40 baris
            blok = []
            for b in msg_w:
                blok.append(b)
                if len(blok) >= 41:
                    send_message(chat_id, "\n".join(blok)); blok = ["📆 *Per Minggu (lanjutan):*"]
            if blok: send_message(chat_id, "\n".join(blok))

            send_message(chat_id, "_Cash = pembayaran Tunai; tidak memerlukan bukti transfer._")
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap cash: {str(e)[:150]}")
            print(f"[REKAP CASH ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_rekap_transfer_bukti(host, chat_id, date_from, date_to, label=""):
    """Rekap invoice TRANSFER, dipisah SUDAH vs BELUM ada bukti di Google Drive,
    dikelompokkan per sales. Bukti transfer = screenshot Shopee/Tokopedia atau
    transfer ke rekening Six Pratama / PT Pratama Talenta Media (disimpan di Drive)."""
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            token = get_drive_token()

            # 1. Kumpulkan semua nama file bukti di Drive -> set nomor invoice (UPPER)
            nama_file_drive = set()
            def daftar(folder_id):
                page_token = None
                while True:
                    params = {"q": f"'{folder_id}' in parents and trashed=false",
                              "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000,
                              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                    if page_token: params["pageToken"] = page_token
                    rs = requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                    ds = rs.json()
                    for x in ds.get("files", []):
                        if x.get("mimeType") == "application/vnd.google-apps.folder":
                            daftar(x["id"])
                        else:
                            nama_file_drive.add(x["name"].rsplit(".", 1)[0].strip().upper())
                    page_token = ds.get("nextPageToken")
                    if not page_token: break
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false", "fields": "files(id,name,mimeType)",
                        "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    daftar(f["id"])
                else:
                    nama_file_drive.add(f["name"].rsplit(".", 1)[0].strip().upper())

            # 2. Ambil invoice lunas + metode, ambil yang TRANSFER (campuran ikut dicek juga)
            lunas = _lunas_dengan_metode(h, date_from, date_to)
            transfer = [it for it in lunas if it.get("metode") in ("transfer", "campuran")]
            if not transfer:
                send_message(chat_id, f"Tidak ada invoice TRANSFER di {label or (date_from + ' - ' + date_to)}.")
                return

            # 3. Pisah sudah/belum ada bukti
            sudah, belum = [], []
            for it in transfer:
                nomor = (it.get("number") or "").strip().upper()
                if nomor and nomor in nama_file_drive:
                    sudah.append(it)
                else:
                    belum.append(it)

            judul = label or f"{date_from} - {date_to}"
            total_tf = sum(x["nilai"] for x in transfer)
            head = f"🏦 *Rekap Transfer & Bukti Bayar - {judul}*\n"
            head += f"Total invoice transfer: {len(transfer)} | Rp {total_tf:,.0f}\n"
            head += f"✅ Sudah ada bukti: {len(sudah)} inv | Rp {sum(x['nilai'] for x in sudah):,.0f}\n"
            head += f"❌ Belum ada bukti: {len(belum)} inv | Rp {sum(x['nilai'] for x in belum):,.0f}\n"
            head += "\n_Bukti transfer: screenshot Shopee/Tokopedia atau transfer ke rek. Six Pratama / PT Pratama Talenta Media di Google Drive._"
            send_message(chat_id, head)

            # 4. Daftar BELUM ada bukti, per sales (yang perlu ditagih)
            def kirim_per_sales(judul_blok, arr):
                if not arr:
                    return
                per_sales = {}
                for it in arr:
                    per_sales.setdefault(it.get("sales", "Tanpa Sales"), []).append(it)
                baris = [judul_blok]
                for sales in sorted(per_sales, key=lambda s: len(per_sales[s]), reverse=True):
                    items = per_sales[sales]
                    subtot = sum(x["nilai"] for x in items)
                    baris.append(f"━━━━━━━━━━\n👤 *{sales}* — {len(items)} inv | Rp {subtot:,.0f}")
                    for x in sorted(items, key=lambda i: i["nilai"], reverse=True):
                        baris.append(f"  • {x['number']} | {x.get('cust','-')} | Rp {x['nilai']:,.0f}")
                    if len(baris) >= 40:
                        send_message(chat_id, "\n".join(baris)); baris = [judul_blok + " (lanjutan)"]
                if len(baris) > 1:
                    send_message(chat_id, "\n".join(baris))

            kirim_per_sales(f"❌ *Transfer BELUM ada bukti ({len(belum)} inv)* — perlu ditagih:", belum)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal rekap transfer & bukti: {str(e)[:150]}")
            print(f"[REKAP TRANSFER BUKTI ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_cek_bukti_bayar_massal(host, chat_id, date_from, date_to, label=""):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            # 1. Ambil semua nama file di Drive (semua subfolder), kumpulkan nomor invoice yang ADA buktinya
            token = get_drive_token()
            nama_file_drive = set()
            # folder utama
            q_root = f"'{GDRIVE_FOLDER_ID}' in parents and trashed=false"
            r = requests.get("https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": q_root, "fields": "files(id,name,mimeType)", "pageSize": 1000,
                        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=30)
            for f in r.json().get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    # masuk subfolder
                    qsub = f"'{f['id']}' in parents and trashed=false"
                    page_token = None
                    while True:
                        params = {"q": qsub, "fields": "nextPageToken,files(name)", "pageSize": 1000,
                                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
                        if page_token: params["pageToken"] = page_token
                        rs = requests.get("https://www.googleapis.com/drive/v3/files",
                            headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
                        ds = rs.json()
                        for x in ds.get("files", []):
                            nama_file_drive.add(x["name"].rsplit(".", 1)[0].strip().upper())
                        page_token = ds.get("nextPageToken")
                        if not page_token: break
                else:
                    nama_file_drive.add(f["name"].rsplit(".", 1)[0].strip().upper())

            # 2. Ambil semua invoice di periode
            all_inv = []
            page = 1
            while True:
                params = {"fields": "id,number,totalAmount,statusName", "sp.pageSize": 200, "sp.page": page,
                    "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": date_from, "filter.transDate.val[1]": date_to}
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
                data = r.json()
                if not data.get("s"): break
                all_inv.extend(data.get("d", []))
                sp = data.get("sp", {})
                if page >= sp.get("pageCount", 1): break
                page += 1

            # 3. Cocokkan nomor invoice dengan nama file Drive
            ada, belum = [], []
            for inv in all_inv:
                if not isinstance(inv, dict): continue
                nomor = (inv.get("number") or "").strip().upper()
                if nomor in nama_file_drive:
                    ada.append(nomor)
                else:
                    belum.append({"number": inv.get("number"), "nilai": float(inv.get("totalAmount") or 0)})

            judul = label or f"{date_from} - {date_to}"
            msg = f"📎 *Cek Bukti Bayar Massal - {judul}*\n\n"
            msg += f"Total invoice: {len(all_inv)}\n"
            msg += f"✅ Sudah ada bukti di Drive: {len(ada)}\n"
            msg += f"❌ Belum ada bukti: {len(belum)}\n\n"
            if belum:
                msg += "*Invoice belum ada bukti (urut nilai terbesar):*\n"
                belum.sort(key=lambda x: x["nilai"], reverse=True)
                for b in belum[:40]:
                    msg += f"• {b['number']}: Rp {b['nilai']:,.0f}\n"
                if len(belum) > 40:
                    msg += f"... dan {len(belum)-40} invoice lainnya\n"
            msg += "\n_Ini cek KEBERADAAN file bukti (nama file = nomor invoice). Untuk cek nominal cocok/tidak, gunakan 'cek bukti bayar [nomor]' per invoice._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek bukti bayar massal: {str(e)[:150]}")
            print(f"[BUKTI MASSAL ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


def tool_cek_bukti_bayar(host, chat_id, nomor_invoice):
    def run():
        try:
            h = host if host.startswith("http") else f"https://{host}"
            # 1. Ambil nilai invoice dari Accurate
            r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
                params={"fields": "id,number", "filter.keywords": nomor_invoice, "sp.pageSize": 1}, timeout=15)
            lst = r.json().get("d", [])
            nilai_invoice = None
            if lst and isinstance(lst[0], dict):
                inv_id = lst[0].get("id")
                if inv_id:
                    r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv_id}, timeout=15)
                    det = r2.json().get("d", {})
                    if isinstance(det, dict):
                        nilai_invoice = _resolve_nilai_invoice(det)

            # 2. Cari file bukti bayar di Drive
            files = drive_cari_file_invoice(nomor_invoice)
            if not files:
                msg = f"❌ Bukti bayar untuk {nomor_invoice} TIDAK ditemukan di Google Drive."
                if nilai_invoice is not None:
                    msg += f"\nNilai invoice di Accurate: Rp {nilai_invoice:,.0f}"
                send_message(chat_id, msg)
                return

            # 3. Download file pertama, baca nominal
            f = files[0]
            if not isinstance(f, dict) or not f.get("id"):
                send_message(chat_id, f"❌ File bukti {nomor_invoice} ditemukan tapi formatnya tidak terbaca.")
                return
            img = drive_download_file(f["id"])
            nominal_foto, penjelasan = baca_nominal_dari_gambar(img, f.get("mimeType", "image/png"))

            # 4. Bandingkan
            msg = f"📎 *Cek Bukti Bayar {nomor_invoice}*\n\n"
            msg += f"File ditemukan: {f['name']}\n"
            if nilai_invoice is not None:
                msg += f"Nilai invoice (Accurate): Rp {nilai_invoice:,.0f}\n"
            msg += f"Nominal terbaca di bukti: Rp {nominal_foto:,.0f}\n"
            if penjelasan:
                msg += f"_(yang dibaca: {penjelasan})_\n"
            msg += "\n"
            if nilai_invoice is not None and nominal_foto > 0:
                selisih = nominal_foto - nilai_invoice
                if abs(selisih) < 1:
                    msg += "✅ COCOK - nominal bukti sama dengan invoice."
                else:
                    msg += f"⚠️ BERBEDA - selisih Rp {selisih:,.0f}.\n_Bisa karena bayar sebagian, digabung invoice lain, biaya admin, atau foto terbaca kurang akurat._"
            else:
                msg += "_Tidak bisa membandingkan (nilai invoice atau nominal foto tidak terbaca)._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal cek bukti bayar: {str(e)[:150]}")
            print(f"[BUKTI BAYAR ERROR] {e}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started"})


@app.route("/debug-env", methods=["GET"])
def debug_env():
    return {
        "ada_GOOGLE_CREDENTIALS_B64": bool(GOOGLE_CREDENTIALS_B64),
        "panjang_B64": len(GOOGLE_CREDENTIALS_B64),
        "ada_GOOGLE_CREDENTIALS_JSON": bool(GOOGLE_CREDENTIALS_JSON),
        "ada_GDRIVE_FOLDER_ID": bool(GDRIVE_FOLDER_ID),
        "kode_versi": "baca-base64"
    }


@app.route("/debug-drive", methods=["GET"])
def debug_drive():
    try:
        if not GOOGLE_CREDENTIALS_B64 and not GOOGLE_CREDENTIALS_JSON:
            return {"error": "Kredensial Google belum diset"}, 500
        if not GDRIVE_FOLDER_ID:
            return {"error": "GDRIVE_FOLDER_ID belum diset"}, 500
        files = drive_list_files()
        hasil = {"folder_utama_isi": [], "subfolder_isi": {}}
        for f in files:
            is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
            hasil["folder_utama_isi"].append({"nama": f["name"], "tipe": "folder" if is_folder else "file"})
            # Kalau ini folder, intip isinya
            if is_folder:
                token = get_drive_token()
                q = f"'{f['id']}' in parents and trashed=false"
                r = requests.get("https://www.googleapis.com/drive/v3/files",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": q, "fields": "files(id,name,mimeType)", "pageSize": 20,
                            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=20)
                sub = r.json().get("files", [])
                hasil["subfolder_isi"][f["name"]] = {
                    "jumlah": len(sub),
                    "contoh": [x["name"] for x in sub[:15]]
                }
        return hasil
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}, 500


@app.route("/debug-finance", methods=["GET"])
def debug_finance():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        all_inv = []
        page = 1
        while True:
            params = {"fields": "id,number,transDate", "filter.keywords": "PRINTIVA MULTIPACK",
                      "sp.pageSize": 100, "sp.page": page}
            r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
            data = r.json()
            if not data.get("s"): break
            all_inv.extend(data.get("d", []))
            sp = data.get("sp", {})
            if page >= sp.get("pageCount", 1): break
            page += 1
            if page > 30: break
        tgls = [i.get("transDate") for i in all_inv if isinstance(i, dict)]
        return {
            "jumlah_invoice_printiva_terdeteksi": len(all_inv),
            "contoh_5_pertama": [i.get("number") for i in all_inv[:5] if isinstance(i, dict)],
            "contoh_5_terakhir": [i.get("number") for i in all_inv[-5:] if isinstance(i, dict)],
            "rentang_tanggal": f"{min(tgls) if tgls else '?'} s/d {max(tgls) if tgls else '?'}"
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}, 500


@app.route("/debug-nosales", methods=["GET"])
def debug_nosales():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        from concurrent.futures import ThreadPoolExecutor as TPE
        all_invoices = []
        page = 1
        while True:
            params = {"fields": "id,number", "sp.pageSize": 200, "sp.page": page,
                "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": "01/06/2026", "filter.transDate.val[1]": "30/06/2026"}
            r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
            data = r.json()
            if not data.get("s"): break
            all_invoices.extend(data.get("d", []))
            sp = data.get("sp", {})
            if page >= sp.get("pageCount", 1): break
            page += 1

        lock = threading.Lock()
        no_sales = []
        error_baca = []

        def cek(inv):
            try:
                r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                detail = r2.json().get("d", {})
                nm = detail.get("masterSalesmanName")
                if not nm or not str(nm).strip():
                    with lock: no_sales.append(inv.get("number"))
            except Exception:
                with lock: error_baca.append(inv.get("number"))

        with TPE(max_workers=15) as ex:
            list(ex.map(cek, all_invoices))

        return {
            "total_invoice": len(all_invoices),
            "jumlah_tanpa_sales": len(no_sales),
            "jumlah_gagal_baca_detail": len(error_baca),
            "daftar_nomor_tanpa_sales": sorted(no_sales),
            "daftar_nomor_gagal_baca": sorted(error_baca)
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-coba-cepat", methods=["GET"])
def debug_coba_cepat():
    """Uji beberapa cara agar nama sales ikut di LIST invoice (tanpa buka detail),
    supaya rekap sales bisa cepat. Coba variasi nama field & parameter."""
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        base_filter = {
            "sp.pageSize": 3, "sp.page": 1,
            "filter.transDate.op": "BETWEEN",
            "filter.transDate.val[0]": "01/07/2026", "filter.transDate.val[1]": "31/07/2026"
        }
        percobaan = {}

        # 1. Minta field salesman versi objek/relasi (beberapa API Accurate pakai nama ini)
        variasi_fields = [
            "id,number,salesmanName",
            "id,number,salesman",
            "id,number,salesmanId",
            "id,number,detailSalesman",
            "id,number,salesmanList",
            "id,number,employee",
            "id,number,employeeName",
        ]
        for fld in variasi_fields:
            try:
                r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
                    params={**base_filter, "fields": fld}, timeout=15)
                d = r.json()
                rows = d.get("d", [])
                contoh = rows[0] if rows and isinstance(rows[0], dict) else {}
                percobaan[fld] = {"berhasil": d.get("s"), "field_kembali": sorted(list(contoh.keys())), "contoh": contoh}
            except Exception as e:
                percobaan[fld] = {"error": str(e)[:100]}

        # 2. Coba endpoint list TANPA batasi fields (mungkin default kirim lebih lengkap)
        try:
            r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
                params=base_filter, timeout=15)
            d = r.json()
            rows = d.get("d", [])
            contoh = rows[0] if rows and isinstance(rows[0], dict) else {}
            percobaan["TANPA_fields"] = {"berhasil": d.get("s"), "field_kembali": sorted(list(contoh.keys())), "contoh": contoh}
        except Exception as e:
            percobaan["TANPA_fields"] = {"error": str(e)[:100]}

        return {"percobaan": percobaan}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-metode-bayar", methods=["GET"])
def debug_metode_bayar():
    """Cek apakah metode pembayaran (tunai/transfer) sebuah invoice lunas bisa dibaca API.
    Menampilkan field pembayaran di detail invoice + coba baca sales-receipt terkait."""
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        # Ambil 3 invoice LUNAS Juli 2026
        r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
            params={"fields": "id,number,statusName", "sp.pageSize": 30, "sp.page": 1,
                "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": "01/07/2026", "filter.transDate.val[1]": "31/07/2026"}, timeout=15)
        lst = r.json().get("d", [])
        lunas = [x for x in lst if isinstance(x, dict) and "LUNAS" in (x.get("statusName") or "").upper()][:3]
        hasil = []
        for inv in lunas:
            r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(),
                params={"id": inv["id"]}, timeout=15)
            det = r2.json().get("d", {})
            if not isinstance(det, dict): continue
            # field yang mungkin berkaitan dengan metode/penerimaan bayar
            kandidat = {}
            for k, v in det.items():
                kl = k.lower()
                if any(t in kl for t in ["payment", "receipt", "bank", "cash", "tunai", "bayar", "transfer", "epayment", "paid"]):
                    if isinstance(v, (dict, list)):
                        kandidat[k] = str(v)[:250]
                    else:
                        kandidat[k] = v
            # coba ambil sales-receipt yang terkait invoice ini
            receipt_info = None
            try:
                rr = requests.get(f"{h}/accurate/api/sales-receipt/list.do", headers=accurate_headers(),
                    params={"fields": "id,number,bankName,paymentMethod,chequeAmount", "sp.pageSize": 5,
                            "filter.keywords": inv.get("number")}, timeout=15)
                receipt_info = rr.json().get("d", [])
            except Exception as e:
                receipt_info = f"gagal: {str(e)[:100]}"
            hasil.append({
                "number": inv.get("number"),
                "field_terkait_bayar": kandidat,
                "sales_receipt_terkait": receipt_info
            })
        return {"hasil": hasil}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-total-jan", methods=["GET"])
def debug_total_jan():
    """Bandingkan total nilai Januari 2026 dengan beberapa cara ambil nilai,
    untuk cari tahu kenapa total tool sales beda dari total omset."""
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        all_inv = []
        page = 1
        while True:
            params = {"fields": "id,number,totalAmount,salesAmount,subTotal", "sp.pageSize": 200, "sp.page": page,
                "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": "01/01/2026", "filter.transDate.val[1]": "31/01/2026"}
            r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(), params=params, timeout=30)
            data = r.json()
            if not data.get("s"): break
            all_inv.extend(data.get("d", []))
            sp = data.get("sp", {})
            if page >= sp.get("pageCount", 1): break
            page += 1

        jumlah = len(all_inv)
        total_totalAmount = 0.0
        total_subTotal = 0.0
        total_salesAmount = 0.0
        n_totalAmount_kosong = 0
        n_semua_kosong = 0
        contoh_kosong = []
        for inv in all_inv:
            if not isinstance(inv, dict): continue
            ta = float(inv.get("totalAmount") or 0)
            st = float(inv.get("subTotal") or 0)
            sa = float(inv.get("salesAmount") or 0)
            total_totalAmount += ta
            total_subTotal += st
            total_salesAmount += sa
            if ta == 0:
                n_totalAmount_kosong += 1
            if ta == 0 and st == 0 and sa == 0:
                n_semua_kosong += 1
                if len(contoh_kosong) < 5:
                    contoh_kosong.append(inv.get("number"))
        return {
            "jumlah_invoice": jumlah,
            "SUM_totalAmount": total_totalAmount,
            "SUM_subTotal": total_subTotal,
            "SUM_salesAmount": total_salesAmount,
            "invoice_totalAmount_0": n_totalAmount_kosong,
            "invoice_semua_nilai_0": n_semua_kosong,
            "contoh_invoice_semua_0": contoh_kosong
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-list-fields", methods=["GET"])
def debug_list_fields():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        # Minta banyak field di LIST, lihat mana yang benar-benar terkirim
        r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
            params={"fields": "id,number,transDate,totalAmount,salesAmount,subTotal,masterSalesmanName,masterSalesmanId,statusName",
                "sp.pageSize": 3, "sp.page": 1,
                "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": "01/01/2026", "filter.transDate.val[1]": "31/01/2026"}, timeout=15)
        data = r.json()
        rows = data.get("d", [])
        hasil = []
        for inv in rows:
            if isinstance(inv, dict):
                hasil.append({
                    "number": inv.get("number"),
                    "SEMUA_FIELD_YANG_TERKIRIM": sorted(list(inv.keys())),
                    "isi": inv
                })
        return {"jumlah": len(rows), "hasil": hasil}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-sales-jan", methods=["GET"])
def debug_sales_jan():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        # Ambil 5 invoice Januari 2026
        r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
            params={"fields": "id,number", "sp.pageSize": 5, "sp.page": 1,
                "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": "01/01/2026", "filter.transDate.val[1]": "31/01/2026"}, timeout=15)
        list_data = r.json().get("d", [])
        hasil = []
        for inv in list_data:
            r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(),
                params={"id": inv["id"]}, timeout=15)
            detail = r2.json().get("d", {})
            if not isinstance(detail, dict):
                continue
            # Cari semua field yang mengandung 'sales' atau 'salesman' (huруф kecil)
            field_sales = {}
            for k, v in detail.items():
                kl = k.lower()
                if "sales" in kl or "salesman" in kl:
                    # ringkas kalau value-nya objek besar
                    if isinstance(v, (dict, list)):
                        field_sales[k] = str(v)[:300]
                    else:
                        field_sales[k] = v
            hasil.append({
                "number": inv.get("number"),
                "field_mengandung_sales": field_sales,
                "detail_masterSalesmanName": detail.get("masterSalesmanName"),
                "detail_salesman": str(detail.get("salesman"))[:300] if detail.get("salesman") is not None else None,
                "semua_key_detail": sorted(list(detail.keys()))
            })
        return {"hasil": hasil}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-sales", methods=["GET"])
def debug_sales():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        # Ambil 8 invoice Juni 2026
        r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
            params={"fields": "id,number,masterSalesmanName,masterSalesmanId", "sp.pageSize": 8, "sp.page": 1,
                "filter.transDate.op": "BETWEEN", "filter.transDate.val[0]": "01/06/2026", "filter.transDate.val[1]": "30/06/2026"}, timeout=15)
        list_data = r.json().get("d", [])
        hasil = []
        for inv in list_data:
            r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(),
                params={"id": inv["id"]}, timeout=15)
            detail = r2.json().get("d", {})
            hasil.append({
                "number": inv.get("number"),
                "list_masterSalesmanName": inv.get("masterSalesmanName"),
                "detail_masterSalesmanName": detail.get("masterSalesmanName"),
                "detail_masterSalesmanId": detail.get("masterSalesmanId")
            })
        return {"hasil": hasil}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/debug-invoice", methods=["GET"])
def debug_invoice():
    try:
        host = get_host()
        if not host: return {"error": "Gagal dapat host"}, 500
        h = host if host.startswith("http") else f"https://{host}"
        # Ambil 1 invoice OPEN
        r = requests.get(f"{h}/accurate/api/sales-invoice/list.do", headers=accurate_headers(),
            params={"fields": "id,number,totalAmount,outstanding", "sp.pageSize": 1, "sp.page": 1, "filter.status": "OPEN"}, timeout=15)
        lst = r.json().get("d", [])
        if not lst:
            return {"error": "Tidak ada invoice OPEN"}, 404
        inv_id = lst[0]["id"]
        list_fields = lst[0]
        # Ambil detail lengkap invoice itu untuk lihat SEMUA field
        r2 = requests.get(f"{h}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(),
            params={"id": inv_id}, timeout=15)
        detail = r2.json().get("d", {})
        # Tampilkan hanya field bertipe angka + nama field, biar mudah dibaca
        money_fields = {k: v for k, v in detail.items() if isinstance(v, (int, float))}
        return {
            "from_list_endpoint": list_fields,
            "detail_money_fields": money_fields,
            "detail_all_keys": sorted(list(detail.keys()))
        }
    except Exception as e:
        return {"error": str(e)}, 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
