# =====================================================================
# TEMPEL BLOK INI KE main.py
# Letakkan di mana saja SETELAH baris `app = Flask(__name__)`
# dan setelah fungsi accurate_headers() & get_host() sudah didefinisikan.
# Paling aman: tempel di BAGIAN BAWAH file (sebelum blok
# `if __name__ == "__main__":` kalau ada; kalau tidak ada, di paling bawah).
#
# Tidak perlu import ulang — os, requests, request, app sudah ada di file.
# =====================================================================

# --- Konfigurasi Supabase (dari environment variable Railway) ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SYNC_SECRET = os.environ.get("SYNC_SECRET", "")


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",  # upsert
    }


def tarik_semua_sku():
    """Ambil semua barang stok (INVENTORY) dari Accurate."""
    host = get_host()
    hasil = []
    page = 1
    while True:
        r = requests.get(
            f"{host}/accurate/api/item/list.do",
            headers=accurate_headers(),
            params={
                "fields": "id,no,name,unit,itemType,lastPurchasePrice",
                "sp.pageSize": 100,
                "sp.page": page,
            },
            timeout=20,
        )
        data = r.json()
        rows = data.get("d", []) or []
        for it in rows:
            if it.get("itemType") and it.get("itemType") != "INVENTORY":
                continue
            if not it.get("no"):
                continue
            hasil.append({
                "accurate_id": str(it.get("id")) if it.get("id") is not None else None,
                "kode_sku": it.get("no"),
                "nama_barang": it.get("name"),
                "satuan": it.get("unit"),
                "harga_beli_akhir": it.get("lastPurchasePrice"),
                "aktif": True,
            })
        sp = data.get("sp", {}) or {}
        if page >= sp.get("pageCount", 1):
            break
        page += 1
    return hasil


def simpan_sku_ke_supabase(rows):
    """Upsert ke tabel sku_master berdasarkan kode_sku."""
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/sku_master?on_conflict=kode_sku"
    r = requests.post(url, headers=_sb_headers(), json=rows, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Supabase error {r.status_code}: {r.text}")
    return len(rows)


@app.route("/sync-sku")
def route_sync_sku():
    if SYNC_SECRET and request.args.get("secret") != SYNC_SECRET:
        return {"ok": False, "error": "secret salah"}, 403
    try:
        rows = tarik_semua_sku()
        jml = simpan_sku_ke_supabase(rows)
        return {"ok": True, "jumlah_sku": jml}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =====================================================================
# SELESAI. Simpan, commit, push ke GitHub main -> Railway auto-deploy.
# Tes: https://web-production-89b2f.up.railway.app/sync-sku?secret=printmaster2026
# =====================================================================
