#!/usr/bin/env bash
# Gera as cobranças Pix do MÊS ATUAL (1 por atleta ativo). Idempotente.
set -euo pipefail
cd /opt/racha/app
MESES=(JAN FEV MAR ABR MAI JUN JUL AGO SET OUT NOV DEZ)
IDX=$(( $(date +%-m) - 1 ))
MES="${MESES[$IDX]} $(date +%Y)"
echo "Gerando cobranças Pix para: $MES"
exec /opt/racha/venv/bin/python pix_cobranca.py "$MES"
