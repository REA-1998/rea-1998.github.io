# deploy/ — instalar o Racha REA no servidor (guia rápido para o TI)

O servidor **gera** o painel a partir do Google Sheets e o **serve** pelo Caddy. Isolado da
Riovare (usuário/pastas/serviços próprios). Detalhe completo em `_SETUP_SERVIDOR_RACHA.md`.

## Instalação (uma vez)
```bash
# 1) usuário e código (repo público → HTTPS, sem chave)
sudo useradd -r -m -d /opt/racha racha
sudo mkdir -p /opt/racha/app /opt/racha/site && sudo chown -R racha:racha /opt/racha
sudo -u racha git clone https://github.com/REA-1998/rea-1998.github.io.git /opt/racha/app

# 2) ambiente Python
sudo -u racha python3.12 -m venv /opt/racha/venv
sudo -u racha /opt/racha/venv/bin/pip install -r /opt/racha/app/requirements.txt

# 3) segredos: .env + chave da conta de serviço do Google (NÃO vão no git)
sudo -u racha cp /opt/racha/app/deploy/.env.example /opt/racha/app/.env
#   -> copiar o google-sa.json (chave da conta de serviço) para /opt/racha/app/google-sa.json
sudo chmod 600 /opt/racha/app/.env /opt/racha/app/google-sa.json

# 4) serviços (gerar painel + auto-deploy)
chmod +x /opt/racha/app/deploy/*.sh
sudo cp /opt/racha/app/deploy/racha-*.service /etc/systemd/system/
sudo cp /opt/racha/app/deploy/racha-*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now racha-panel.timer racha-deploy.timer

# gerar agora e conferir
sudo systemctl start racha-panel.service
journalctl -u racha-panel.service -n 30 --no-pager
ls -la /opt/racha/site   # deve ter index.html + assets

# 5) Caddy: acrescentar o bloco (troque o domínio) e recarregar
sudo nano /etc/caddy/Caddyfile   # cole o conteúdo de deploy/Caddyfile.racha
sudo systemctl reload caddy
```

Pronto: `https://racha.<dominio>` serve o painel, regenerado a cada 15 min (e o código se
atualiza sozinho do GitHub a cada 5 min).

## Arquivos
- `gerar-panel.sh` — gera o index.html do Sheets para `/opt/racha/site`
- `deploy.sh` — `git pull` + `pip install` (auto-deploy)
- `racha-panel.{service,timer}` — regenera o painel (15 min)
- `racha-deploy.{service,timer}` — puxa atualizações do GitHub (5 min)
- `Caddyfile.racha` — bloco do Caddy (subdomínio → /opt/racha/site)
- `.env.example` — modelo de configuração (copiar para `/opt/racha/app/.env`)
