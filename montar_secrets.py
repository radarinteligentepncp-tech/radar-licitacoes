"""
Monta o conteúdo dos "Secrets" do Radar de Licitações.

Uso:   python montar_secrets.py
Gera o arquivo secrets_gerado.toml. Copie o conteúdo dele para
"Settings > Secrets" no Streamlit Community Cloud (ou salve como
.streamlit/secrets.toml para rodar no computador) e depois APAGUE o arquivo.
NUNCA envie esse conteúdo para o GitHub.
"""
import getpass
import hashlib
import json
import os

print("=== Administrador principal (você) ===")
usuario = input("Usuário do administrador (ex.: luiz): ").strip().lower()
nome = input("Nome para exibição: ").strip() or "Administrador"
senha = getpass.getpass("Senha do administrador (mín. 8): ")
if len(senha) < 8 or senha != getpass.getpass("Repita a senha: "):
    raise SystemExit("Senha curta ou as senhas não conferem.")
salt = os.urandom(16)
hash_senha = f"pbkdf2${salt.hex()}${hashlib.pbkdf2_hmac('sha256', senha.encode(), salt, 200_000).hex()}"

print("\n=== Planilha do Google (deixe em branco para modo local) ===")
entrada = input("ID da planilha (ou cole a URL inteira): ").strip()
if "/d/" in entrada:  # aceita a URL completa
    entrada = entrada.split("/d/", 1)[1].split("/")[0]
planilha_id = entrada.strip().strip("/").strip()

caminho_json = ""
if planilha_id:
    caminho_json = input("Caminho do arquivo .json (ou da pasta onde ele está): ").strip().strip('"').strip("'")
    if os.path.isdir(caminho_json):  # aceita a pasta e procura o .json dentro dela
        jsons = [f for f in os.listdir(caminho_json) if f.lower().endswith(".json")]
        if len(jsons) != 1:
            raise SystemExit(f"Na pasta há {len(jsons)} arquivo(s) .json; informe o caminho do arquivo certo.")
        caminho_json = os.path.join(caminho_json, jsons[0])
        print(f"   Usando: {caminho_json}")
    if not os.path.isfile(caminho_json):
        raise SystemExit(f"Arquivo não encontrado: {caminho_json}")

print("\n=== Telegram (opcional, para gerar links de convite no painel) ===")
bot = input("Nome do robô no Telegram, sem @ (ex.: RadarLicitaBot): ").strip().lstrip("@")

linhas = []
if planilha_id:
    linhas.append(f"planilha_usuarios_id = {json.dumps(planilha_id)}")
if bot:
    linhas.append(f"telegram_bot_nome = {json.dumps(bot)}")
if linhas:
    linhas.append("")
linhas += ["[admin]", f"usuario = {json.dumps(usuario)}", f"nome = {json.dumps(nome)}",
           f"senha = {json.dumps(hash_senha)}"]
if caminho_json:
    with open(caminho_json, encoding="utf-8") as f:
        credenciais = json.load(f)
    linhas += ["", "[gcp_service_account]"]
    linhas += [f"{k} = {json.dumps(v)}" for k, v in credenciais.items()]

with open("secrets_gerado.toml", "w", encoding="utf-8") as f:
    f.write("\n".join(linhas) + "\n")
print("\n✅ Arquivo secrets_gerado.toml criado. Copie o conteúdo para os Secrets e apague o arquivo.")
if caminho_json:
    print(f"   Lembre-se de compartilhar a planilha (Editor) com: {credenciais.get('client_email')}")
