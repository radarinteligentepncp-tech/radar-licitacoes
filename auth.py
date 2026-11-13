"""
Controle de acesso do Radar de Licitações.

Fluxo:
1. O interessado preenche "Solicitar acesso" -> cadastro fica PENDENTE.
2. O administrador entra no painel "Gestão de usuários" e APROVA ou RECUSA.
3. Só usuários APROVADOS conseguem entrar. O administrador pode, a qualquer
   momento, bloquear, reativar, redefinir senha, promover a administrador
   ou excluir um usuário.

Onde os cadastros ficam guardados:
- Publicado (Streamlit Community Cloud): numa planilha do Google Sheets,
  configurada nos Secrets ([gcp_service_account] + planilha_usuarios_id).
- Rodando no computador, sem essa configuração: no arquivo .dados/usuarios.csv.

O administrador principal fica nos Secrets ([admin]) e nunca depende da
planilha, para que você não perca o acesso em nenhuma hipótese.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

CAMPOS = ["usuario", "nome", "email", "orgao", "senha_hash", "status", "perfil",
          "criado_em", "decidido_em", "decidido_por", "ultimo_acesso"]

PENDENTE, APROVADO, RECUSADO, BLOQUEADO = "pendente", "aprovado", "recusado", "bloqueado"
ROTULO_STATUS = {PENDENTE: "⏳ Pendente", APROVADO: "✅ Aprovado",
                 RECUSADO: "❌ Recusado", BLOQUEADO: "🚫 Bloqueado"}

MAX_TENTATIVAS = 5          # por sessão do navegador
REVALIDAR_SEGUNDOS = 600    # reconfere o status do usuário logado a cada 10 min
FUSO = ZoneInfo("America/Sao_Paulo")
RE_USUARIO = re.compile(r"^[a-z0-9._-]{3,30}$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Senhas
# ---------------------------------------------------------------------------
def gerar_hash(senha: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, 200_000).hex()
    return f"pbkdf2${salt.hex()}${h}"


def verificar_senha(senha: str, armazenado: str) -> bool:
    try:
        _, salt, hash_hex = str(armazenado).split("$")
        calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), 200_000).hex()
        return hmac.compare_digest(calc, hash_hex)
    except Exception:
        return False


def agora() -> str:
    return datetime.now(FUSO).strftime("%d/%m/%Y %H:%M")


# ---------------------------------------------------------------------------
# Armazenamento
# ---------------------------------------------------------------------------
class RepositorioLocal:
    """Arquivo CSV local. Serve para testes no computador (não use na nuvem)."""

    persistente = False
    _trava = threading.Lock()

    def __init__(self, caminho: str = ".dados/usuarios.csv"):
        self.caminho = caminho
        os.makedirs(os.path.dirname(caminho), exist_ok=True)

    def _ler(self) -> pd.DataFrame:
        if os.path.exists(self.caminho):
            df = pd.read_csv(self.caminho, dtype=str).fillna("")
            return df.reindex(columns=CAMPOS, fill_value="")
        return pd.DataFrame(columns=CAMPOS)

    def _gravar(self, df: pd.DataFrame):
        df.to_csv(self.caminho, index=False)

    def listar(self) -> list[dict]:
        return self._ler().to_dict("records")

    def obter(self, usuario: str) -> dict | None:
        df = self._ler()
        achado = df[df["usuario"] == usuario]
        return achado.iloc[0].to_dict() if not achado.empty else None

    def inserir(self, registro: dict):
        with self._trava:
            df = self._ler()
            df = pd.concat([df, pd.DataFrame([{c: registro.get(c, "") for c in CAMPOS}])])
            self._gravar(df)

    def atualizar(self, usuario: str, campos: dict):
        with self._trava:
            df = self._ler()
            for k, v in campos.items():
                df.loc[df["usuario"] == usuario, k] = v
            self._gravar(df)

    def excluir(self, usuario: str):
        with self._trava:
            df = self._ler()
            self._gravar(df[df["usuario"] != usuario])


class RepositorioPlanilha:
    """Planilha do Google Sheets (aba 'usuarios'), acessada por conta de serviço."""

    persistente = True
    _trava = threading.Lock()

    def __init__(self, credenciais: dict, planilha_id: str):
        import gspread

        gc = gspread.service_account_from_dict(credenciais)
        planilha = gc.open_by_key(planilha_id)
        try:
            self.ws = planilha.worksheet("usuarios")
        except gspread.exceptions.WorksheetNotFound:
            self.ws = planilha.add_worksheet("usuarios", rows=200, cols=len(CAMPOS))
        if self.ws.row_values(1) != CAMPOS:
            self.ws.update(range_name="A1", values=[CAMPOS], value_input_option="RAW")

    def listar(self) -> list[dict]:
        linhas = self.ws.get_all_values()[1:]
        return [dict(zip(CAMPOS, (l + [""] * len(CAMPOS))[:len(CAMPOS)])) for l in linhas if l and l[0]]

    def _numero_linha(self, usuario: str) -> int | None:
        coluna = self.ws.col_values(1)
        return coluna.index(usuario) + 1 if usuario in coluna else None

    def obter(self, usuario: str) -> dict | None:
        n = self._numero_linha(usuario)
        if not n:
            return None
        linha = self.ws.row_values(n)
        return dict(zip(CAMPOS, (linha + [""] * len(CAMPOS))[:len(CAMPOS)]))

    def inserir(self, registro: dict):
        with self._trava:
            self.ws.append_row([registro.get(c, "") for c in CAMPOS], value_input_option="RAW")

    def atualizar(self, usuario: str, campos: dict):
        with self._trava:
            n = self._numero_linha(usuario)
            if not n:
                return
            atual = self.obter(usuario)
            atual.update(campos)
            self.ws.update(range_name=f"A{n}", values=[[atual[c] for c in CAMPOS]],
                           value_input_option="RAW")

    def excluir(self, usuario: str):
        with self._trava:
            n = self._numero_linha(usuario)
            if n:
                self.ws.delete_rows(n)


def _segredo(chave, padrao=None):
    try:
        return st.secrets[chave]
    except Exception:
        return padrao


@st.cache_resource(show_spinner="Conectando ao cadastro de usuários...")
def repositorio():
    credenciais = _segredo("gcp_service_account")
    planilha_id = _segredo("planilha_usuarios_id")
    if credenciais and planilha_id:
        return RepositorioPlanilha(dict(credenciais), planilha_id)
    return RepositorioLocal()


def admin_principal() -> dict | None:
    adm = _segredo("admin")
    if adm and adm.get("usuario") and adm.get("senha"):
        return {"usuario": str(adm["usuario"]).lower(), "senha": adm["senha"],
                "nome": adm.get("nome", "Administrador")}
    return None


def contar_pendentes() -> int:
    try:
        return sum(1 for r in repositorio().listar() if r["status"] == PENDENTE)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Tela de login / solicitação de acesso
# ---------------------------------------------------------------------------
def _tentar_login(usuario: str, senha: str):
    adm = admin_principal()
    if adm and usuario == adm["usuario"]:
        if verificar_senha(senha, adm["senha"]):
            return {"usuario": usuario, "nome": adm["nome"], "perfil": "admin",
                    "principal": True, "validado_em": time.time()}, None
        return None, "Usuário ou senha inválidos."

    reg = repositorio().obter(usuario)
    if not reg or not verificar_senha(senha, reg["senha_hash"]):
        return None, "Usuário ou senha inválidos."
    # Status só é revelado depois que a senha confere
    if reg["status"] == PENDENTE:
        return None, "⏳ Seu cadastro ainda aguarda autorização do administrador."
    if reg["status"] == RECUSADO:
        return None, "Sua solicitação de acesso não foi aprovada. Procure o administrador."
    if reg["status"] == BLOQUEADO:
        return None, "Seu acesso está bloqueado. Procure o administrador."
    repositorio().atualizar(usuario, {"ultimo_acesso": agora()})
    return {"usuario": usuario, "nome": reg["nome"], "perfil": reg["perfil"] or "usuario",
            "principal": False, "validado_em": time.time()}, None


def _form_login():
    tentativas = st.session_state.get("tentativas_login", 0)
    if tentativas >= MAX_TENTATIVAS:
        st.error("Muitas tentativas incorretas. Feche a aba e tente novamente mais tarde.")
        return
    with st.form("form_login"):
        usuario = st.text_input("Usuário").strip().lower()
        senha = st.text_input("Senha", type="password")
        entrar = st.form_submit_button("Entrar", type="primary")
    if entrar:
        sessao, erro = _tentar_login(usuario, senha)
        if sessao:
            st.session_state["auth"] = sessao
            st.session_state["tentativas_login"] = 0
            st.rerun()
        st.session_state["tentativas_login"] = tentativas + 1
        time.sleep(1)  # dificulta tentativas automatizadas
        st.error(erro)


def _form_cadastro():
    if st.session_state.get("cadastro_enviado"):
        st.success("✅ Solicitação enviada! Você poderá entrar assim que o administrador autorizar.")
        return
    with st.form("form_cadastro", clear_on_submit=False):
        nome = st.text_input("Nome completo *").strip()
        email = st.text_input("E-mail *").strip().lower()
        orgao = st.text_input("Órgão / empresa").strip()
        usuario = st.text_input("Usuário desejado *",
                                help="3 a 30 caracteres: letras minúsculas, números, ponto, hífen ou _").strip().lower()
        senha = st.text_input("Senha * (mínimo 8 caracteres)", type="password")
        senha2 = st.text_input("Repita a senha *", type="password")
        aceite = st.checkbox("Autorizo o uso do meu nome, e-mail e órgão/empresa exclusivamente para "
                             "controle de acesso a este sistema (LGPD - Lei nº 13.709/2018).")
        enviar = st.form_submit_button("Solicitar acesso", type="primary")
    if not enviar:
        return

    erros = []
    if len(nome) < 3:
        erros.append("Informe o nome completo.")
    if not RE_EMAIL.match(email):
        erros.append("E-mail inválido.")
    if not RE_USUARIO.match(usuario):
        erros.append("Usuário inválido (use 3 a 30 letras minúsculas, números, ponto, hífen ou _).")
    if len(senha) < 8:
        erros.append("A senha deve ter pelo menos 8 caracteres.")
    if senha != senha2:
        erros.append("As senhas não conferem.")
    if not aceite:
        erros.append("É necessário autorizar o uso dos dados para controle de acesso.")
    if not erros:
        repo = repositorio()
        existentes = repo.listar()
        adm = admin_principal()
        if any(r["usuario"] == usuario for r in existentes) or (adm and adm["usuario"] == usuario):
            erros.append("Esse nome de usuário já está em uso. Escolha outro.")
        elif any(r["email"] == email for r in existentes):
            erros.append("Já existe uma solicitação com esse e-mail.")
    if erros:
        for e in erros:
            st.error(e)
        return

    repositorio().inserir({
        "usuario": usuario, "nome": nome, "email": email, "orgao": orgao,
        "senha_hash": gerar_hash(senha), "status": PENDENTE, "perfil": "usuario",
        "criado_em": agora(),
    })
    st.session_state["cadastro_enviado"] = True
    st.rerun()


def exigir_login(titulo: str = "Radar de Licitações - PNCP") -> dict:
    """Bloqueia o app até haver um usuário autorizado. Retorna os dados da sessão."""
    sessao = st.session_state.get("auth")
    if sessao:
        # Reconfere periodicamente: se o admin bloquear, o acesso cai em até 10 min
        if not sessao["principal"] and time.time() - sessao["validado_em"] > REVALIDAR_SEGUNDOS:
            reg = repositorio().obter(sessao["usuario"])
            if not reg or reg["status"] != APROVADO:
                st.session_state.clear()
                st.rerun()
            sessao.update({"perfil": reg["perfil"] or "usuario", "validado_em": time.time()})
        return sessao

    if not admin_principal():
        st.error("Administrador não configurado. Inclua a seção [admin] nos Secrets "
                 "(veja o arquivo LEIAME.md).")
        st.stop()

    _, centro, _ = st.columns([1, 1.4, 1])
    with centro:
        st.markdown(f"## 🔐 {titulo}")
        aba_entrar, aba_cadastro = st.tabs(["Entrar", "Solicitar acesso"])
        with aba_entrar:
            _form_login()
        with aba_cadastro:
            st.caption("Seu cadastro ficará pendente até ser autorizado pelo administrador.")
            _form_cadastro()
        if not repositorio().persistente:
            st.caption("ℹ️ Modo local: cadastros gravados em .dados/usuarios.csv.")
    st.stop()


# ---------------------------------------------------------------------------
# Painel do administrador
# ---------------------------------------------------------------------------
def painel_admin(sessao: dict):
    repo = repositorio()
    st.title("👥 Gestão de usuários")
    if not repo.persistente:
        st.warning("Modo local (arquivo CSV). Na nuvem, configure a planilha do Google "
                   "nos Secrets, senão os cadastros se perdem quando o app reiniciar.")

    registros = repo.listar()
    quem = sessao["usuario"]

    def decidir(usuario, status):
        repo.atualizar(usuario, {"status": status, "decidido_em": agora(), "decidido_por": quem})

    # --- Solicitações pendentes ---
    pendentes = [r for r in registros if r["status"] == PENDENTE]
    st.subheader(f"Solicitações aguardando autorização ({len(pendentes)})")
    if not pendentes:
        st.info("Nenhuma solicitação pendente.")
    for r in pendentes:
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"**{r['nome']}** · `{r['usuario']}`  \n"
                        f"{r['email']} · {r['orgao'] or 'órgão/empresa não informado'}  \n"
                        f"Solicitado em {r['criado_em']}")
            if c2.button("✅ Aprovar", key=f"apr_{r['usuario']}", type="primary"):
                decidir(r["usuario"], APROVADO)
                st.rerun()
            if c3.button("❌ Recusar", key=f"rec_{r['usuario']}"):
                decidir(r["usuario"], RECUSADO)
                st.rerun()

    # --- Todos os usuários ---
    st.subheader("Todos os usuários")
    if not registros:
        st.info("Nenhum usuário cadastrado ainda.")
        return
    tabela = pd.DataFrame(registros).drop(columns=["senha_hash"])
    tabela["status"] = tabela["status"].map(lambda s: ROTULO_STATUS.get(s, s))
    tabela.columns = ["Usuário", "Nome", "E-mail", "Órgão/Empresa", "Status", "Perfil",
                      "Cadastro", "Decisão em", "Decidido por", "Último acesso"]
    st.dataframe(tabela, hide_index=True)

    # --- Ações sobre um usuário ---
    st.subheader("Gerenciar um usuário")
    opcoes = {f"{r['nome']} ({r['usuario']}) — {ROTULO_STATUS.get(r['status'], r['status'])}": r
              for r in registros}
    escolhido = opcoes[st.selectbox("Usuário:", list(opcoes.keys()))]
    u = escolhido["usuario"]

    c1, c2, c3 = st.columns(3)
    if escolhido["status"] == APROVADO:
        if c1.button("🚫 Bloquear acesso"):
            decidir(u, BLOQUEADO)
            st.rerun()
    elif c1.button("✅ Aprovar / reativar"):
        decidir(u, APROVADO)
        st.rerun()

    if escolhido["perfil"] == "admin":
        if c2.button("Remover perfil de administrador"):
            repo.atualizar(u, {"perfil": "usuario"})
            st.rerun()
    elif c2.button("⭐ Tornar administrador"):
        repo.atualizar(u, {"perfil": "admin"})
        st.rerun()

    with c3.popover("🔑 Redefinir senha"):
        nova = st.text_input("Nova senha (mín. 8)", type="password", key=f"nova_{u}")
        if st.button("Salvar nova senha", key=f"salvar_{u}"):
            if len(nova) < 8:
                st.error("Mínimo de 8 caracteres.")
            else:
                repo.atualizar(u, {"senha_hash": gerar_hash(nova)})
                st.success("Senha alterada. Informe a nova senha ao usuário.")

    with st.expander("🗑️ Excluir usuário"):
        st.caption("Remove o cadastro definitivamente. Para apenas impedir o acesso, prefira bloquear.")
        if st.checkbox(f"Confirmo a exclusão de {u}", key=f"conf_{u}") and st.button("Excluir", key=f"exc_{u}"):
            repo.excluir(u)
            st.rerun()
