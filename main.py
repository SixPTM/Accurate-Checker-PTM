import os
import json
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Simpan riwayat percakapan per user (supaya Claude ingat konteks)
conversation_history = {}

SYSTEM_PROMPT = """Kamu adalah asisten keuangan yang membantu tim admin mengecek invoice, 
hutang piutang, dan kinerja admin. Kamu berbicara dalam Bahasa Indonesia yang ramah dan profesional.

Saat ini kamu belum terhubung ke data Accurate (akan disambungkan di tahap berikutnya). 
Untuk sementara, bantu user dengan pertanyaan umum seputar invoice dan keuangan, 
atau arahkan mereka untuk memberikan data invoice secara manual jika ingin dicek.

Jika user mengirim data invoice (nomor, jumlah, status, dll), bantu analisa dan rangkum dengan jelas.
Selalu jawab singkat, padat, dan mudah dimengerti."""


def send_message(chat_id, text):
    """Kirim pesan ke Telegram"""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)


def ask_claude(chat_id, user_message):
    """Kirim pesan ke Claude API dan dapat balasan"""
    # Inisialisasi history untuk user baru
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    # Tambah pesan user ke history
    conversation_history[chat_id].append({
        "role": "user",
        "content": user_message
    })

    # Batasi history maksimal 20 pesan supaya tidak terlalu panjang
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    # Panggil Claude API
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

    # Simpan balasan Claude ke history
    conversation_history[chat_id].append({
        "role": "assistant",
        "content": reply
    })

    return reply


@app.route(f"/webhook", methods=["POST"])
def webhook():
    """Terima pesan dari Telegram"""
    data = request.json

    # Pastikan ada pesan teks
    if "message" not in data:
        return "ok", 200

    message = data["message"]
    chat_id = message["chat"]["id"]

    if "text" not in message:
        send_message(chat_id, "Maaf, saya hanya bisa memproses pesan teks untuk saat ini.")
        return "ok", 200

    user_text = message["text"]
    user_name = message["from"].get("first_name", "")

    # Handle command /start
    if user_text == "/start":
        send_message(chat_id,
            f"Halo {user_name}! 👋\n\n"
            "Saya *Accurate Checker Bot*, asisten untuk membantu kamu:\n"
            "✅ Cek status invoice\n"
            "✅ Pantau hutang piutang\n"
            "✅ Monitor kinerja admin\n\n"
            "Ketik pertanyaan kamu langsung, contoh:\n"
            "_\"Invoice INV-001 sudah lunas belum?\"_\n"
            "_\"Rekap invoice yang belum dibayar bulan ini\"_\n\n"
            "⚠️ Saat ini masih dalam mode testing. Koneksi ke Accurate akan segera aktif."
        )
        return "ok", 200

    # Handle command /reset (reset percakapan)
    if user_text == "/reset":
        conversation_history[chat_id] = []
        send_message(chat_id, "✅ Percakapan direset. Halo lagi! Ada yang bisa saya bantu?")
        return "ok", 200

    # Kirim "mengetik..." supaya user tahu bot sedang proses
    requests.post(f"{TELEGRAM_API}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing"
    })

    # Tanya Claude dan kirim balasannya
    try:
        reply = ask_claude(chat_id, user_text)
        send_message(chat_id, reply)
    except Exception as e:
        send_message(chat_id, "⚠️ Maaf, terjadi kesalahan. Coba lagi beberapa saat.")
        print(f"Error: {e}")

    return "ok", 200


@app.route("/", methods=["GET"])
def index():
    return "Accurate Checker Bot is running! ✅", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
