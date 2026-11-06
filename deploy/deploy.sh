#!/usr/bin/env bash
# Atualiza o código do repositório e as dependências. (o "git pull" do dia a dia)
set -euo pipefail
cd /opt/racha/app
git pull --ff-only
/opt/racha/venv/bin/pip install -q -r requirements.txt
echo "Deploy OK: código atualizado do GitHub em $(cd /opt/racha/app && git rev-parse --short HEAD)"
