#!/bin/bash
# Jalankan script ini SETELAH deploy ke Railway
# Ganti YOUR_TELEGRAM_TOKEN dan YOUR_RAILWAY_URL dengan nilai asli

TELEGRAM_TOKEN="YOUR_TELEGRAM_TOKEN"
RAILWAY_URL="YOUR_RAILWAY_URL"  # contoh: https://bot-invoice.up.railway.app

curl -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"${RAILWAY_URL}/webhook\"}"
