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

def send_message(chat_id, text):
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"[SEND ERROR] {e}")

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
                try:
                    r2 = requests.get(f"{host}/accurate/api/sales-invoice/detail.do", headers=accurate_headers(), params={"id": inv["id"]}, timeout=10)
                    detail = r2.json().get("d", {})
                    customer = detail.get("customer")
                    if isinstance(customer, dict): cname = customer.get("name")
                    elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else str(customer[0])
                    else: cname = None
                    inv["customerName"] = detail.get("retailWpName") or detail.get("customerName") or cname or "-"
                    inv["outstanding"] = detail.get("outstanding") or 0
                    inv["totalAmount"] = detail.get("totalAmount") or inv.get("totalAmount") or 0
                except: inv["customerName"] = "-"
            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(enrich, invoices[:20]))

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
                item["unit"] = detail.get("unit") or item.get("unit") or ""
                print(f"[ITEM STOCK] {item['name']} balance={detail.get('balance')} stock={item['availableStock']}")
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
                    customer = detail.get("customer")
                    if isinstance(customer, dict): cname = customer.get("name")
                    elif isinstance(customer, list) and customer: cname = customer[0].get("name") if isinstance(customer[0], dict) else None
                    else: cname = None
                    name = detail.get("retailWpName") or detail.get("customerName") or cname or "-"
                    total = float(detail.get("totalAmount") or inv.get("totalAmount") or 0)
                    outstanding = float(detail.get("outstanding") or 0)
                    if name and name != "-":
                        with lock:
                            if name not in customer_data: customer_data[name] = {"count": 0, "total": 0.0, "outstanding": 0.0}
                            customer_data[name]["count"] += 1
                            customer_data[name]["total"] += total
                            customer_data[name]["outstanding"] += outstanding
                except: pass

            with ThreadPoolExecutor(max_workers=15) as ex:
                list(ex.map(enrich, all_invoices))

            sorted_customers = sorted(customer_data.items(), key=lambda x: x[1]["outstanding"], reverse=True)
            total_outstanding = sum(v["outstanding"] for v in customer_data.values())
            total_nilai = sum(v["total"] for v in customer_data.values())

            msg = f"✅ *Customer Belum Bayar - {label}*\n\n"
            msg += f"Total invoice OPEN: {total_invoice}\n"
            msg += f"Total nilai: Rp {total_nilai:,.0f}\n"
            if total_outstanding > 0: msg += f"Total outstanding: Rp {total_outstanding:,.0f}\n"
            msg += f"Jumlah customer: {len(customer_data)}\n\n*Daftar (urut outstanding terbesar):*\n"
            for name, d in sorted_customers[:30]:
                outstanding_str = f" | Sisa: Rp {d['outstanding']:,.0f}" if d['outstanding'] > 0 else ""
                msg += f"• {name} — {d['count']} inv{outstanding_str}\n"
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
            total_nilai = 0.0
            total_invoice = 0
            page = 1
            while True:
                params = {"fields": "id,totalAmount,subTotal", "sp.pageSize": 200, "sp.page": page, "filter.status": "OPEN"}
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
                for inv in page_data:
                    total_nilai += float(inv.get("totalAmount") or inv.get("subTotal") or 0)
                print(f"[BG PIUTANG] {page}/{sp.get('pageCount',1)} Rp {total_nilai:,.0f}")
                if page >= sp.get("pageCount", 1): break
                page += 1

            msg = f"✅ *Piutang Belum Lunas - {label}*\n\nTotal invoice: {total_invoice}\nTotal nilai: Rp {total_nilai:,.0f}\n\n_⚠️ Nilai adalah total invoice. Jika ada partial payment, angka bisa sedikit berbeda dari Accurate._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal: {str(e)[:100]}")

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
        "description": "Ambil daftar sales invoice dari Accurate Online. Filter by status (OPEN=belum lunas, CLOSED=lunas), tanggal, keyword customer.",
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
        "description": "Ambil detail lengkap satu invoice termasuk item produk.",
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
        "description": "Cari produk di Accurate: harga, stok, SKU. Nama produk di Accurate mungkin disingkat, contoh 'Tumblr' bukan 'Tumbler'.",
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
        "name": "get_top_products_background",
        "description": "Rekap SEMUA produk terlaris di periode tertentu. Background 5-10 menit. Untuk 'produk apa paling laku bulan juni', 'top produk'.",
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
    }
]

SYSTEM_PROMPT = """Kamu adalah asisten keuangan dan operasional untuk perusahaan Print Master, terhubung ke Accurate Online via API.

Gunakan tools untuk ambil data real-time dari Accurate. Jawab dalam Bahasa Indonesia, ramah, gunakan emoji.
Format angka: Rp 1.500.000. Jangan panggil tool lebih dari 3x per pertanyaan.

Tools background (hasilnya dikirim otomatis ke Telegram setelah selesai):
- get_top_products_background: rekap SEMUA produk terlaris (5-10 menit)
- get_sales_per_item: penjualan produk tertentu (3-5 menit)  
- get_unpaid_customers_background: daftar customer belum bayar (2-3 menit)
- get_piutang_summary: total piutang (2-3 menit)

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
            timeout=30
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

    requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
