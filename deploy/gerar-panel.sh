#!/usr/bin/env bash
# Gera o painel do REA a partir do Google Sheets e publica na pasta servida pelo Caddy.
set -euo pipefail
APP=/opt/racha/app
SITE=/opt/racha/site
VENV=/opt/racha/venv

mkdir -p "$SITE"
# copia os assets estáticos (não mudam) para a pasta pública
cp -f "$APP"/favicon.png "$APP"/icon-180.png "$APP"/icon-512.png \
      "$APP"/manifest.json "$APP"/og-image.png "$SITE"/ 2>/dev/null || true

# gera o index.html direto na pasta do site (lê a config do .env do app)
cd "$APP"
OUTPUT="$SITE" "$VENV/bin/python" gerar_painel_sheets.py
echo "Painel publicado em $SITE"
