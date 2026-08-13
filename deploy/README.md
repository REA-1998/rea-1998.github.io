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

# 5) Caddy: acrescentar o bloco (racharea.com.br) e recarregar
sudo nano /etc/caddy/Caddyfile   # cole o conteúdo de deploy/Caddyfile.racha
sudo systemctl reload caddy
```

Pronto: `https://racharea.com.br` serve o painel, regenerado a cada 15 min (e o código se
atualiza sozinho do GitHub a cada 5 min).

## Bot do Telegram (foto da súmula → IA → Sheets → painel)
O bot roda **só num lugar por vez** (o token do Telegram não aceita dois pollers ao mesmo
tempo). A partir de agora ele mora **no servidor** — a versão da máquina de trabalho fica
desligada. Passos (uma vez):
```bash
# preencher as 3 variáveis do bot no .env (descomente as linhas Fase 2)
sudo nano /opt/racha/app/.env
#   TELEGRAM_BOT_TOKEN=...   TELEGRAM_ALLOWED_ID=...   ANTHROPIC_API_KEY=...

sudo systemctl enable --now racha-bot.service
journalctl -u racha-bot.service -n 30 --no-pager   # deve mostrar "Application started"
```
Os valores das 3 variáveis o Mateus passa em separado (não vão no git). Depois de subir,
mandar uma foto de súmula no Telegram para validar.

## Pix automático (Efí) — cobrança por atleta + "pago" automático
Fluxo: todo dia 1º o servidor gera 1 cobrança Pix por atleta ativo (aba `PixCobrancas`);
o atleta paga pelo QR no site; a Efí chama o **webhook** e o pagamento cai sozinho na aba
`Pagamentos`, e o painel se atualiza. Passos (uma vez):

```bash
# 1) dependências novas (já estão no requirements.txt: efipay, flask)
sudo -u racha /opt/racha/venv/bin/pip install -r /opt/racha/app/requirements.txt

# 2) certificado da Efí: converter o .p12 (sem senha) para .pem
#    (o Mateus envia o arquivo producao-XXXXX.p12; copie para o servidor)
openssl pkcs12 -in producao-945611.p12 -out /opt/racha/app/efi-racha.pem -nodes -passin pass:
sudo chown racha:racha /opt/racha/app/efi-racha.pem && sudo chmod 600 /opt/racha/app/efi-racha.pem

# 3) .env: preencher as variáveis da "Fase 3" (EFI_*, PIX_WEBHOOK_TOKEN aleatório, etc.)
sudo nano /opt/racha/app/.env

# 4) serviço do webhook + timer mensal das cobranças
sudo cp /opt/racha/app/deploy/pix-webhook.service /etc/systemd/system/
sudo cp /opt/racha/app/deploy/racha-cobranca.service /etc/systemd/system/
sudo cp /opt/racha/app/deploy/racha-cobranca.timer   /etc/systemd/system/
chmod +x /opt/racha/app/deploy/gerar-cobrancas.sh
sudo systemctl daemon-reload
sudo systemctl enable --now pix-webhook.service racha-cobranca.timer
curl -s localhost:8090/pix-racha/health    # deve responder {"ok": true}

# 5) Caddy: acrescentar a rota do webhook (já incluída no deploy/Caddyfile.racha atualizado)
sudo nano /etc/caddy/Caddyfile      # garanta o bloco @pix -> reverse_proxy 127.0.0.1:8090
sudo systemctl reload caddy

# 6) registrar a URL do webhook na Efí (troque <TOKEN> pelo PIX_WEBHOOK_TOKEN do .env)
sudo -u racha /opt/racha/venv/bin/python /opt/racha/app/pix_webhook.py \
     registrar https://racharea.com.br/pix-racha/webhook/<TOKEN>

# gerar as cobranças do mês agora (teste): 
sudo systemctl start racha-cobranca.service
journalctl -u racha-cobranca.service -n 30 --no-pager
```

Para validar: pagar 1 centavo numa cobrança de teste e ver o pagamento aparecer na aba
Pagamentos + no painel. O `PANEL_REFRESH_CMD` no `.env` faz o site atualizar na hora.

## Arquivos
- `gerar-panel.sh` — gera o index.html do Sheets para `/opt/racha/site`
- `deploy.sh` — `git pull` + `pip install` (auto-deploy)
- `racha-panel.{service,timer}` — regenera o painel (15 min)
- `racha-deploy.{service,timer}` — puxa atualizações do GitHub (5 min)
- `Caddyfile.racha` — bloco do Caddy (subdomínio → /opt/racha/site)
- `.env.example` — modelo de configuração (copiar para `/opt/racha/app/.env`)
