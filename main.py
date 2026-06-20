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

# ============================================================
# ACCURATE API HELPERS
# ============================================================

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

def get_host():
    try:
        r = requests.post(
            f"{ACCURATE_BASE_URL}/api-token.do",
            headers=accurate_headers(),
            timeout=15
        )
        d = r.json().get("d", {})
        host = d.get("database", d.get("data usaha", {})).get("host", "")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host
    except Exception as e:
        print(f"[HOST ERROR] {e}")
        return None

def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
    })

# ============================================================
# ACCURATE API TOOLS — dipanggil oleh Claude
# ============================================================

def tool_get_invoices(host, params):
    """List sales invoice dengan filter bebas."""
    try:
        default_fields = ",".join([
            "id", "number", "transDate", "transDateView",
            "dueDate", "dueDateView", "statusName",
            "retailWpName", "totalAmount", "subTotal",
            "outstanding", "attachmentExist", "masterSalesmanName"
        ])
        api_params = {
            "fields": default_fields,
            "sp.pageSize": params.get("page_size", 50),
            "sp.page": 1,
            "sp.sort": "transDate",
            "sp.sortOrder": "DESC"
        }
        if params.get("status"):
            api_params["filter.status"] = params["status"]
        if params.get("date_from"):
            api_params["filter.transDate.op"] = "BETWEEN"
            api_params["filter.transDate.val[0]"] = params["date_from"]
            api_params["filter.transDate.val[1]"] = params.get("date_to", params["date_from"])
        if params.get("keyword"):
            api_params["filter.keywords"] = params["keyword"]
        if params.get("customer_name"):
            api_params["filter.keywords"] = params["customer_name"]

        r = requests.get(
            f"{host}/accurate/api/sales-invoice/list.do",
            headers=accurate_headers(),
            params=api_params,
            timeout=15
        )
        data = r.json()
        print(f"[TOOL invoices] status={r.status_code} count={len(data.get('d',[]))} total={data.get('sp',{}).get('rowCount',0)}")

        if data.get("s") and data.get("d"):
            # Enrich nama customer secara paralel
            invoices = data["d"]
            def enrich(inv):
                try:
                    r2 = requests.get(
                        f"{host}/accurate/api/sales-invoice/detail.do",
                        headers=accurate_headers(),
                        params={"id": inv["id"]},
                        timeout=10
                    )
                    detail = r2.json().get("d", {})
                    customer = detail.get("customer")
                    if isinstance(customer, dict):
                        cname = customer.get("name")
                    elif isinstance(customer, list) and customer:
                        cname = customer[0].get("name") if isinstance(customer[0], dict) else str(customer[0])
                    else:
                        cname = None
                    inv["customerName"] = (
                        detail.get("retailWpName") or
                        detail.get("customerName") or
                        cname or "-"
                    )
                    inv["outstanding"] = detail.get("outstanding") or 0
                    inv["totalAmount"] = detail.get("totalAmount") or inv.get("totalAmount") or 0
                except:
                    inv["customerName"] = "-"
            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(enrich, invoices))

        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_invoice_detail(host, invoice_id):
    """Detail satu invoice berdasarkan ID."""
    try:
        r = requests.get(
            f"{host}/accurate/api/sales-invoice/detail.do",
            headers=accurate_headers(),
            params={"id": invoice_id},
            timeout=15
        )
        data = r.json()
        print(f"[TOOL detail] id={invoice_id} status={r.status_code}")
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_items(host, keyword, page_size=20):
    """Cari produk/item: nama, harga, stok."""
    try:
        params = {
            "fields": "id,no,name,unitPrice,purchasePrice,availableStock,unit,buyPrice,lastPurchasePrice,type",
            "sp.pageSize": page_size,
            "sp.page": 1,
        }
        if keyword:
            params["filter.keywords"] = keyword

        r = requests.get(
            f"{host}/accurate/api/item/list.do",
            headers=accurate_headers(),
            params=params,
            timeout=15
        )
        data = r.json()
        print(f"[TOOL items] keyword='{keyword}' count={len(data.get('d',[]))} total={data.get('sp',{}).get('rowCount',0)}")
        if data.get("d"):
            print(f"[TOOL items sample] {data['d'][0]}")
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_piutang_summary(host, chat_id, date_from=None, date_to=None, label="Semua Periode"):
    """Hitung total piutang semua halaman di background thread."""
    def run():
        try:
            total_nilai = 0.0
            total_invoice = 0
            page = 1
            while True:
                params = {
                    "fields": "id,totalAmount,subTotal,statusName,retailWpName,number,dueDate",
                    "sp.pageSize": 200,
                    "sp.page": page,
                    "filter.status": "OPEN"
                }
                if date_from:
                    params["filter.transDate.op"] = "BETWEEN"
                    params["filter.transDate.val[0]"] = date_from
                    params["filter.transDate.val[1]"] = date_to or date_from
                r = requests.get(
                    f"{host}/accurate/api/sales-invoice/list.do",
                    headers=accurate_headers(),
                    params=params,
                    timeout=30
                )
                data = r.json()
                if not data.get("s"):
                    break
                page_data = data.get("d", [])
                sp = data.get("sp", {})
                if page == 1:
                    total_invoice = sp.get("rowCount", 0)
                for inv in page_data:
                    total_nilai += float(inv.get("totalAmount") or inv.get("subTotal") or 0)
                total_pages = sp.get("pageCount", 1)
                print(f"[BG PIUTANG] {page}/{total_pages} Rp {total_nilai:,.0f}")
                if page >= total_pages:
                    break
                page += 1

            msg = f"✅ *Piutang Belum Lunas - {label}*\n\n"
            msg += f"Total invoice: {total_invoice}\n"
            msg += f"Total nilai: Rp {total_nilai:,.0f}\n"
            msg += f"\n_⚠️ Nilai adalah total invoice. Jika ada partial payment, angka bisa sedikit berbeda dari Accurate._"
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Gagal hitung piutang: {str(e)[:100]}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
    return json.dumps({"status": "background_started", "message": f"Menghitung piutang {label} di background..."})


# ============================================================
# CLAUDE TOOLS DEFINITION
# ============================================================

TOOLS = [
    {
        "name": "get_invoices",
        "description": "Ambil daftar sales invoice dari Accurate Online. Gunakan untuk pertanyaan tentang invoice, penjualan, omset, customer belum bayar, dll. Bisa filter by status (OPEN=belum lunas, CLOSED=lunas), tanggal, keyword customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Tanggal mulai format DD/MM/YYYY, contoh: 01/06/2026"},
                "date_to": {"type": "string", "description": "Tanggal akhir format DD/MM/YYYY, contoh: 30/06/2026"},
                "status": {"type": "string", "description": "OPEN untuk belum lunas, CLOSED untuk lunas. Kosongkan untuk semua."},
                "keyword": {"type": "string", "description": "Keyword pencarian nama customer atau nomor invoice"},
                "page_size": {"type": "integer", "description": "Jumlah data, default 50, max 100"}
            }
        }
    },
    {
        "name": "get_invoice_detail",
        "description": "Ambil detail lengkap satu invoice termasuk item produk yang dibeli, dari ID invoice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer", "description": "ID invoice dari hasil get_invoices"}
            },
            "required": ["invoice_id"]
        }
    },
    {
        "name": "get_items",
        "description": "Cari produk/barang di Accurate Online. Gunakan untuk pertanyaan tentang harga beli, harga jual, stok, SKU produk.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama produk yang dicari, contoh: tumbler kagura, stiker vinyl"},
                "page_size": {"type": "integer", "description": "Jumlah hasil, default 20"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "get_piutang_summary",
        "description": "Hitung total piutang/belum lunas semua periode. Gunakan HANYA untuk pertanyaan total piutang keseluruhan karena prosesnya lama (background). Untuk piutang periode tertentu yang tidak terlalu banyak, gunakan get_invoices saja.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Tanggal mulai DD/MM/YYYY, kosongkan untuk semua periode"},
                "date_to": {"type": "string", "description": "Tanggal akhir DD/MM/YYYY"},
                "label": {"type": "string", "description": "Label periode untuk ditampilkan, contoh: Juni 2026"}
            }
        }
    }
]

SYSTEM_PROMPT = """Kamu adalah asisten keuangan dan operasional untuk perusahaan Print Master.
Kamu terhubung langsung ke Accurate Online via API dan bisa mengambil data real-time.

Cara kerja:
- Gunakan tool yang tersedia untuk ambil data dari Accurate
- Setelah dapat data, analisa dan jawab pertanyaan user dengan jelas
- Format angka dalam Rupiah (Rp 1.500.000)
- Jawab dalam Bahasa Indonesia yang ramah, singkat, gunakan emoji
- Jika data kosong/tidak ditemukan, sarankan keyword alternatif

Tools yang tersedia:
- get_invoices: untuk invoice, penjualan, piutang per periode, customer
- get_invoice_detail: untuk detail satu invoice termasuk produk di dalamnya
- get_items: untuk harga dan stok produk
- get_piutang_summary: untuk total piutang keseluruhan (proses di background)

Tanggal hari ini: {today}"""


# ============================================================
# MAIN HANDLER — Claude dengan tool use
# ============================================================

def handle_with_claude(chat_id, user_text, host):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%d/%m/%Y")
    system = SYSTEM_PROMPT.format(today=today)

    conversation_history[chat_id].append({"role": "user", "content": user_text})
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    messages = list(conversation_history[chat_id])

    # Loop agentic — Claude bisa panggil multiple tools
    for _ in range(5):  # max 5 iterasi tool call
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 2048,
                "system": system,
                "tools": TOOLS,
                "messages": messages
            },
            timeout=30
        )
        response = r.json()
        print(f"[CLAUDE] stop_reason={response.get('stop_reason')} content_types={[c['type'] for c in response.get('content',[])]}")

        # Tambah response Claude ke messages
        messages.append({"role": "assistant", "content": response["content"]})

        # Kalau Claude selesai (tidak ada tool call)
        if response.get("stop_reason") == "end_turn":
            text_blocks = [c["text"] for c in response["content"] if c["type"] == "text"]
            reply = "\n".join(text_blocks)
            conversation_history[chat_id].append({"role": "assistant", "content": reply})
            return reply

        # Kalau Claude minta tool
        if response.get("stop_reason") == "tool_use":
            tool_results = []
            for block in response["content"]:
                if block["type"] != "tool_use":
                    continue

                tool_name = block["name"]
                tool_input = block["input"]
                tool_use_id = block["id"]

                print(f"[TOOL CALL] {tool_name} input={json.dumps(tool_input)[:200]}")

                if tool_name == "get_invoices":
                    result = tool_get_invoices(host, tool_input)
                elif tool_name == "get_invoice_detail":
                    result = tool_get_invoice_detail(host, tool_input["invoice_id"])
                elif tool_name == "get_items":
                    result = tool_get_items(host, tool_input["keyword"], tool_input.get("page_size", 20))
                elif tool_name == "get_piutang_summary":
                    result = tool_get_piutang_summary(
                        host, chat_id,
                        tool_input.get("date_from"),
                        tool_input.get("date_to"),
                        tool_input.get("label", "Semua Periode")
                    )
                else:
                    result = json.dumps({"error": f"Unknown tool: {tool_name}"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result
                })

            messages.append({"role": "user", "content": tool_results})

    return "Maaf, tidak bisa memproses permintaan ini."


# ============================================================
# FLASK ROUTES
# ============================================================

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
            f"Halo {user_name}! Saya Accurate Checker Bot Print Master. 👋\n\n"
            "Tanya apa saja tentang data Accurate Online kamu:\n"
            "- 📄 Invoice & status pembayaran\n"
            "- 💰 Piutang & yang belum lunas\n"
            "- 📦 Stok & harga produk\n"
            "- 📊 Rekap penjualan\n"
            "- 👥 Customer terbanyak order\n\n"
            "Langsung tanya saja dengan bahasa natural! 😊"
        )
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


@app.route("/debug-item", methods=["GET"])
def debug_item():
    try:
        host = get_host()
        if not host:
            return {"error": "Gagal dapat host"}, 500

        fields = "id,no,name,unitPrice,purchasePrice,availableStock,unit"
        results = {}

        # Test 1: tanpa filter, lihat sample data
        r1 = requests.get(f"{host}/accurate/api/item/list.do",
            headers=accurate_headers(),
            params={"fields": fields, "sp.pageSize": 3, "sp.page": 1},
            timeout=15)
        results["no_filter"] = r1.json()

        # Test 2: filter.keywords
        r2 = requests.get(f"{host}/accurate/api/item/list.do",
            headers=accurate_headers(),
            params={"fields": fields, "sp.pageSize": 5, "filter.keywords": "stiker"},
            timeout=15)
        results["filter_keywords"] = {"count": len(r2.json().get("d", [])), "total": r2.json().get("sp", {}).get("rowCount", 0)}

        # Test 3: filter.name
        r3 = requests.get(f"{host}/accurate/api/item/list.do",
            headers=accurate_headers(),
            params={"fields": fields, "sp.pageSize": 5, "filter.name": "stiker"},
            timeout=15)
        results["filter_name"] = {"count": len(r3.json().get("d", [])), "total": r3.json().get("sp", {}).get("rowCount", 0)}

        # Test 4: name (tanpa filter.)
        r4 = requests.get(f"{host}/accurate/api/item/list.do",
            headers=accurate_headers(),
            params={"fields": fields, "sp.pageSize": 5, "name": "stiker"},
            timeout=15)
        results["name_param"] = {"count": len(r4.json().get("d", [])), "total": r4.json().get("sp", {}).get("rowCount", 0)}

        return {"results": results}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
