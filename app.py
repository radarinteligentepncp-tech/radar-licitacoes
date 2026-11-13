"""
Radar Inteligente de Licitações - PNCP (v2)
==========================================

Varredura de contratações publicadas no Portal Nacional de Contratações
Públicas (PNCP - art. 174 da Lei nº 14.133/2021), usando a API pública de
consulta: https://pncp.gov.br/api/consulta/swagger-ui/index.html

Principais recursos:
- Dois modos de busca:
    * "Propostas em aberto"  -> GET /v1/contratacoes/proposta
      (editais cujo prazo de recebimento de propostas ainda não encerrou)
    * "Publicadas no período" -> GET /v1/contratacoes/publicacao
- Todas as modalidades do PNCP (pregão eletrônico/presencial, concorrência,
  dispensa, inexigibilidade, credenciamento, chamada pública, leilão etc.),
  carregadas da própria API, com tabela de contingência embutida.
- Todas as 27 UFs, filtro opcional por município (código IBGE) e CNPJ do órgão.
- Palavras-chave sem diferenciar acentos/maiúsculas, com termos de exclusão.
- Distância em linha reta a partir de QUALQUER município do Brasil
  (coordenadas de todos os 5.570 municípios, pelo código IBGE).
- Requisições em paralelo, com retry/backoff, tratamento de HTTP 429 e
  relatório de falhas (nada é descartado em silêncio).
- Filtros pós-busca instantâneos (sem consultar a API de novo), métricas,
  links clicáveis e exportação para Excel/CSV em memória.

Execução:
    pip install streamlit pandas requests openpyxl
    streamlit run app.py
"""

from __future__ import annotations

import io
import math
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st
from limitador_pncp import LIMITADOR, get_pncp, resumo_erro
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configurações gerais
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Radar de Licitações - PNCP", page_icon="📡", layout="wide")

# Imagem de fundo e logotipo da consultoria (pasta "imagens"; ver identidade_visual.py)
import identidade_visual  # noqa: E402

identidade_visual.aplicar()

# Controle de acesso: login, solicitação de cadastro e aprovação pelo administrador
from auth import contar_pendentes, exigir_login, painel_admin  # noqa: E402

SESSAO = exigir_login()
USUARIO = SESSAO["usuario"]

with st.sidebar:
    st.caption(f"👤 **{SESSAO['nome']}** ({USUARIO})")
    if st.button("Sair"):
        st.session_state.clear()
        st.rerun()
    pagina = "radar"
    if SESSAO["perfil"] == "admin":
        n_pend = contar_pendentes()
        if n_pend:
            st.warning(f"{n_pend} solicitação(ões) de acesso aguardando sua autorização.")
        rotulos_paginas = {"radar": "📡 Radar", "usuarios": "👥 Usuários"}
        pagina = st.radio("Área:", list(rotulos_paginas), key="pagina_atual",
                          format_func=rotulos_paginas.get)
    st.divider()

if pagina == "usuarios":
    painel_admin(SESSAO)
    st.stop()

API_CONSULTA = "https://pncp.gov.br/api/consulta/v1"
API_PNCP = "https://pncp.gov.br/api/pncp/v1"
URL_MUNICIPIOS = (
    "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"
)
TAMANHO_PAGINA = 50          # máximo aceito pela API de consulta
MAX_WORKERS = 2              # requisições simultâneas (o PNCP limita: ver limitador_pncp.py)
HEADERS = {"User-Agent": "RadarLicitacoesPNCP/2.0 (+streamlit)", "Accept": "application/json"}

# Tabela de domínio "Modalidade de Contratação" do PNCP (contingência caso
# o serviço /v1/modalidades esteja fora do ar). Fonte: Manual de Integração PNCP.
MODALIDADES_PADRAO = {
    1: "Leilão - Eletrônico",
    2: "Diálogo Competitivo",
    3: "Concurso",
    4: "Concorrência - Eletrônica",
    5: "Concorrência - Presencial",
    6: "Pregão - Eletrônico",
    7: "Pregão - Presencial",
    8: "Dispensa de Licitação",
    9: "Inexigibilidade",
    10: "Manifestação de Interesse",
    11: "Pré-qualificação",
    12: "Credenciamento",
    13: "Leilão - Presencial",
    14: "Inaplicabilidade da Licitação",
    15: "Chamada Pública",
}
MODALIDADES_DEFAULT = [6, 7, 8, 4]  # Pregões, Dispensa e Concorrência Eletrônica

UFS = {
    "AC": 12, "AL": 27, "AP": 16, "AM": 13, "BA": 29, "CE": 23, "DF": 53, "ES": 32,
    "GO": 52, "MA": 21, "MT": 51, "MS": 50, "MG": 31, "PA": 15, "PB": 25, "PR": 41,
    "PE": 26, "PI": 22, "RJ": 33, "RN": 24, "RS": 43, "RO": 11, "RR": 14, "SC": 42,
    "SP": 35, "SE": 28, "TO": 17,
}
COD_UF_PARA_SIGLA = {v: k for k, v in UFS.items()}

ESFERAS = {"F": "Federal", "E": "Estadual", "M": "Municipal", "D": "Distrital", "N": "Não informada"}

# Identificação da plataforma de disputa pelo domínio do link de origem
PLATAFORMAS = [
    ("bll.org.br", "BLL Compras"), ("bllcompras", "BLL Compras"),
    ("bnc.org.br", "BNC - Bolsa Nacional de Compras"),
    ("portaldecompraspublicas", "Portal de Compras Públicas"),
    ("comprasnet", "Compras.gov.br"), ("compras.gov.br", "Compras.gov.br"),
    ("licitacoes-e", "Licitações-e (BB)"),
    ("licitanet", "Licitanet"),
    ("bbmnet", "BBMNET"),
    ("comprasbr", "ComprasBR"),
    ("licitar.digital", "Licitar Digital"),
    ("ammlicita", "AMM Licita"),
    ("compras.pr.gov.br", "Compras Paraná"), ("comprasparana", "Compras Paraná"),
    ("bec.sp.gov.br", "BEC/SP"),
    ("compras.mg.gov.br", "Compras MG"),
    ("e-lic", "e-Lic"),
    ("pncp.gov.br", "PNCP"),
]

# Contingência de coordenadas caso a base de municípios não possa ser baixada
COORDENADAS_CONTINGENCIA = {
    "4128633": ("Doutor Ulysses", "PR", -24.5665, -49.4219),
    "4112009": ("Jaguariaíva", "PR", -24.2439, -49.7066),
    "4106902": ("Curitiba", "PR", -25.4195, -49.2646),
    "4119905": ("Ponta Grossa", "PR", -25.0916, -50.1668),
}


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos: 'Veículos' -> 'veiculos'."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def compilar_termos(texto_termos: str) -> list[re.Pattern]:
    """Cada termo casa no início de palavra ('pneu' casa 'pneus', 'pneumático')."""
    termos = [normalizar(t.strip()) for t in texto_termos.replace(";", ",").split(",")]
    return [re.compile(r"\b" + re.escape(t)) for t in termos if t]


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def parse_data(valor) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "")[:19])
    except ValueError:
        return None


def identificar_plataforma(link: str) -> str:
    link_l = (link or "").lower()
    for chave, nome in PLATAFORMAS:
        if chave in link_l:
            return nome
    return "Portal do órgão / outra" if link_l else "Não informada"


def criar_sessao() -> requests.Session:
    """Sessão HTTP com retry exponencial, inclusive para 429 (limite de requisições)."""
    sessao = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],  # o 429 é tratado em limitador_pncp.py
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_maxsize=MAX_WORKERS * 2)
    sessao.mount("https://", adapter)
    sessao.headers.update(HEADERS)
    return sessao


# ---------------------------------------------------------------------------
# Dados de apoio (cacheados)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=24 * 3600, show_spinner=False)
def carregar_modalidades() -> dict[int, str]:
    """Busca a tabela de modalidades ativa no PNCP; usa a contingência se falhar."""
    try:
        res = requests.get(f"{API_PNCP}/modalidades", params={"statusAtivo": "true"},
                           headers=HEADERS, timeout=15)
        if res.status_code == 200:
            dados = res.json()
            mods = {int(m["id"]): m["nome"] for m in dados if m.get("id") and m.get("nome")}
            if mods:
                return dict(sorted({**MODALIDADES_PADRAO, **mods}.items()))
    except Exception:
        pass
    return MODALIDADES_PADRAO


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def carregar_municipios() -> pd.DataFrame:
    """Coordenadas de todos os municípios brasileiros, indexadas pelo código IBGE."""
    try:
        df = pd.read_csv(URL_MUNICIPIOS, dtype={"codigo_ibge": str})
        df["uf"] = df["codigo_uf"].map(COD_UF_PARA_SIGLA)
        df = df[["codigo_ibge", "nome", "uf", "latitude", "longitude"]]
    except Exception:
        df = pd.DataFrame(
            [(k, *v) for k, v in COORDENADAS_CONTINGENCIA.items()],
            columns=["codigo_ibge", "nome", "uf", "latitude", "longitude"],
        )
    df["chave_nome"] = df["nome"].map(normalizar) + "|" + df["uf"]
    df["rotulo"] = df["nome"] + " - " + df["uf"]
    return df.sort_values("rotulo").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Coleta na API do PNCP
# ---------------------------------------------------------------------------
TTL_CACHE_CONSULTA = 30 * 60  # 30 min


@st.cache_resource
def _cache_consultas() -> dict:
    """Cache compartilhado (thread-safe o suficiente para leitura/escrita de dict)."""
    return {}


def consultar_api(endpoint: str, params_fixos: tuple, max_paginas: int) -> dict:
    """Versão com cache de 30 min: repetir a mesma busca não sobrecarrega a API."""
    chave = (endpoint, params_fixos, max_paginas)
    cache = _cache_consultas()
    item = cache.get(chave)
    if item and time.time() - item[0] < TTL_CACHE_CONSULTA:
        return item[1]
    resultado = _consultar_api(endpoint, params_fixos, max_paginas)
    if not resultado["erro"]:  # não guarda falhas, para tentar de novo na próxima
        cache[chave] = (time.time(), resultado)
    return resultado


def _consultar_api(endpoint: str, params_fixos: tuple, max_paginas: int) -> dict:
    """Pagina um endpoint de consulta para uma combinação UF x modalidade."""
    sessao = criar_sessao()
    params = dict(params_fixos)
    registros, erro, total_paginas = [], None, 0
    pagina = 1
    while pagina <= max_paginas:
        params.update({"pagina": pagina, "tamanhoPagina": TAMANHO_PAGINA})
        try:
            res = get_pncp(sessao, f"{API_CONSULTA}/{endpoint}", params, timeout=60)
        except requests.RequestException as exc:
            erro = f"Falha de conexão na página {pagina}: {type(exc).__name__}"
            break
        if res.status_code == 204:          # sem conteúdo
            break
        if res.status_code != 200:
            erro = f"Página {pagina}: {resumo_erro(res)}"
            break
        try:
            dados = res.json()
        except ValueError:
            erro = f"Resposta inválida (não-JSON) na página {pagina}"
            break
        registros.extend(dados.get("data") or [])
        total_paginas = dados.get("totalPaginas") or 0
        if not dados.get("paginasRestantes"):
            break
        pagina += 1
    truncado = total_paginas > max_paginas
    return {"registros": registros, "erro": erro, "truncado": truncado,
            "total_paginas": total_paginas}


def montar_tarefas(modo, ufs, modalidades, data_ini, data_fim, cod_ibge, cnpj):
    endpoint = "contratacoes/proposta" if modo == "aberto" else "contratacoes/publicacao"
    tarefas = []
    for uf in ufs:
        for cod_mod in modalidades:
            p = {"codigoModalidadeContratacao": cod_mod, "dataFinal": data_fim}
            if modo != "aberto":
                p["dataInicial"] = data_ini
            if uf:
                p["uf"] = uf
            if cod_ibge:
                p["codigoMunicipioIbge"] = cod_ibge
            if cnpj:
                p["cnpj"] = cnpj
            tarefas.append((uf or "Todas", cod_mod, endpoint, tuple(sorted(p.items()))))
    return tarefas


def processar_registro(item, cod_mod, nomes_mod, origem, municipios_idx, municipios_nome_idx):
    orgao = item.get("orgaoEntidade") or {}
    unidade = item.get("unidadeOrgao") or {}
    cnpj = orgao.get("cnpj", "")
    ano, seq = item.get("anoCompra", ""), item.get("sequencialCompra", "")

    municipio = unidade.get("municipioNome") or "Não informado"
    uf = unidade.get("ufSigla") or ""
    cod_ibge = str(unidade.get("codigoIbge") or "")

    # Distância: procura pelo código IBGE; se não houver, pelo nome + UF
    distancia = None
    if origem is not None:
        destino = municipios_idx.get(cod_ibge) or municipios_nome_idx.get(f"{normalizar(municipio)}|{uf}")
        if destino:
            distancia = haversine_km(origem[0], origem[1], destino[0], destino[1])

    abertura = parse_data(item.get("dataAberturaProposta"))
    encerramento = parse_data(item.get("dataEncerramentoProposta"))
    link_origem = item.get("linkSistemaOrigem") or ""
    amparo = item.get("amparoLegal") or {}

    return {
        "Controle PNCP": item.get("numeroControlePNCP") or f"{cnpj}-{ano}-{seq}",
        "UF": uf,
        "Município": municipio,
        "Distância (km)": distancia,
        "Esfera": ESFERAS.get(orgao.get("esferaId") or "N", "Não informada"),
        "Órgão": orgao.get("razaoSocial") or "Não informado",
        "Unidade": unidade.get("nomeUnidade") or "",
        "Modalidade": item.get("modalidadeNome") or nomes_mod.get(cod_mod, str(cod_mod)),
        "Nº Compra": f"{item.get('numeroCompra') or ''}/{ano}".strip("/"),
        "Processo": item.get("processo") or "",
        "Objeto": (item.get("objetoCompra") or "").strip(),
        "Info. Complementar": (item.get("informacaoComplementar") or "").strip(),
        "Situação": item.get("situacaoCompraNome") or "",
        "Modo de Disputa": item.get("modoDisputaNome") or "",
        "SRP": "Sim" if item.get("srp") else "Não",
        "Amparo Legal": amparo.get("nome") or "",
        "Valor Estimado (R$)": item.get("valorTotalEstimado"),
        "Abertura Propostas": abertura,
        "Encerramento Propostas": encerramento,
        "Publicação PNCP": parse_data(item.get("dataPublicacaoPncp")),
        "Plataforma": identificar_plataforma(link_origem),
        "Link PNCP": f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}" if cnpj and ano and seq else "",
        "Link Plataforma": link_origem,
    }


def executar_varredura(tarefas, max_paginas, nomes_mod, origem, municipios):
    municipios_idx = dict(zip(municipios["codigo_ibge"],
                              zip(municipios["latitude"], municipios["longitude"])))
    municipios_nome_idx = dict(zip(municipios["chave_nome"],
                                   zip(municipios["latitude"], municipios["longitude"])))
    linhas, falhas, truncados = [], [], []
    barra = st.progress(0.0, text="Iniciando varredura...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futuros = {pool.submit(consultar_api, ep, params, max_paginas): (uf, mod)
                   for uf, mod, ep, params in tarefas}
        for i, fut in enumerate(as_completed(futuros), start=1):
            uf, mod = futuros[fut]
            rotulo = f"{uf} · {nomes_mod.get(mod, mod)}"
            try:
                resultado = fut.result()
            except Exception as exc:  # erro inesperado não derruba a varredura
                resultado = {"registros": [], "erro": repr(exc), "truncado": False}
            if resultado["erro"]:
                falhas.append({"Consulta": rotulo, "Erro": resultado["erro"]})
            if resultado["truncado"]:
                truncados.append(f"{rotulo} ({resultado['total_paginas']} páginas)")
            for item in resultado["registros"]:
                linhas.append(processar_registro(item, mod, nomes_mod, origem,
                                                 municipios_idx, municipios_nome_idx))
            barra.progress(i / len(tarefas), text=f"{i}/{len(tarefas)} consultas · {rotulo}")
    barra.empty()

    df = pd.DataFrame(linhas)
    if not df.empty:
        df = df.drop_duplicates(subset="Controle PNCP").reset_index(drop=True)
    return df, falhas, truncados


# ---------------------------------------------------------------------------
# Exportação
# ---------------------------------------------------------------------------
RE_CARACTERES_INVALIDOS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _limpar_para_excel(valor):
    """Remove caracteres de controle invisíveis (vindos de PDF/Word) que o Excel não aceita."""
    if isinstance(valor, str):
        return RE_CARACTERES_INVALIDOS.sub(" ", valor)[:32000]  # limite de uma célula do Excel
    return valor


def gerar_excel(df: pd.DataFrame) -> bytes:
    df = df.copy()
    for col in df.columns:
        if not (pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_datetime64_any_dtype(df[col])):
            df[col] = df[col].map(_limpar_para_excel)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Editais")
        ws = writer.sheets["Editais"]
        larguras = {"Objeto": 70, "Info. Complementar": 50, "Órgão": 40, "Unidade": 30,
                    "Link PNCP": 45, "Link Plataforma": 45}
        for idx, col in enumerate(df.columns, start=1):
            letra = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[letra].width = larguras.get(col, max(12, min(len(col) + 4, 25)))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        # Links clicáveis
        for nome_col in ("Link PNCP", "Link Plataforma"):
            if nome_col in df.columns:
                c = df.columns.get_loc(nome_col) + 1
                for r in range(2, len(df) + 2):
                    cel = ws.cell(row=r, column=c)
                    if cel.value:
                        cel.hyperlink = cel.value
                        cel.style = "Hyperlink"
    return buffer.getvalue()


def mostrar_tabela(df: pd.DataFrame):
    config = {
        "Link PNCP": st.column_config.LinkColumn("Link PNCP", display_text="Abrir edital"),
        "Link Plataforma": st.column_config.LinkColumn("Plataforma (link)", display_text="Acessar"),
        "Valor Estimado (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
        "Distância (km)": st.column_config.NumberColumn(format="%.0f km"),
        "Dias p/ Encerrar": st.column_config.NumberColumn(format="%d"),
        "Encerramento Propostas": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        "Abertura Propostas": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        "Publicação PNCP": st.column_config.DatetimeColumn(format="DD/MM/YYYY"),
        "Objeto": st.column_config.TextColumn(width="large"),
    }
    try:
        st.dataframe(df, column_config=config, hide_index=True, width="stretch", height=560)
    except TypeError:  # versões antigas do Streamlit
        st.dataframe(df, column_config=config, hide_index=True, use_container_width=True, height=560)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
st.title("📡 Radar Inteligente de Licitações - PNCP")
st.caption("Varredura na API oficial de consulta do Portal Nacional de Contratações Públicas "
           "(art. 174 da Lei nº 14.133/2021).")

modalidades = carregar_modalidades()
municipios = carregar_municipios()

with st.sidebar:
    st.header("⚙️ Busca na API")

    modo_label = st.radio(
        "O que buscar?",
        ["Propostas em aberto", "Publicadas no período"],
        help="**Propostas em aberto**: editais cujo prazo de envio de propostas ainda não encerrou "
             "(ideal para quem vai participar).\n\n**Publicadas no período**: tudo o que foi "
             "publicado no PNCP no intervalo, inclusive contratações diretas já concluídas.",
    )
    modo = "aberto" if modo_label == "Propostas em aberto" else "periodo"

    hoje = datetime.now()
    if modo == "aberto":
        horizonte = st.slider("Encerramento das propostas em até (dias):", 1, 120, 30)
        data_ini, data_fim = None, (hoje + timedelta(days=horizonte)).strftime("%Y%m%d")
    else:
        periodo = st.date_input("Período de publicação:",
                                value=(hoje.date() - timedelta(days=15), hoje.date()),
                                max_value=hoje.date(), format="DD/MM/YYYY")
        if isinstance(periodo, (list, tuple)) and len(periodo) == 2:
            data_ini, data_fim = periodo[0].strftime("%Y%m%d"), periodo[1].strftime("%Y%m%d")
        else:
            data_ini = data_fim = None

    todas_ufs = st.checkbox("Todo o Brasil (sem filtro de UF)", value=False,
                            help="Uma única consulta nacional por modalidade.")
    ufs_sel = [] if todas_ufs else st.multiselect("Estado(s):", list(UFS.keys()), default=["PR"])

    rotulos_mod = {f"{cod} - {nome}": cod for cod, nome in modalidades.items()}
    mods_sel = st.multiselect(
        "Modalidades / formas de contratação:",
        options=list(rotulos_mod.keys()),
        default=[r for r, c in rotulos_mod.items() if c in MODALIDADES_DEFAULT],
        help="Chamamentos públicos aparecem, no PNCP, como Credenciamento, Chamada Pública ou "
             "Manifestação de Interesse, conforme o órgão cadastrou.",
    )
    cods_mod = [rotulos_mod[r] for r in mods_sel]

    with st.expander("Filtros avançados da API"):
        mun_api = st.selectbox("Somente um município (opcional):",
                               ["(todos)"] + municipios["rotulo"].tolist())
        cod_ibge_api = (None if mun_api == "(todos)"
                        else municipios.loc[municipios["rotulo"] == mun_api, "codigo_ibge"].iloc[0])
        cnpj_api = re.sub(r"\D", "", st.text_input("CNPJ do órgão (opcional):"))
        max_paginas = st.slider("Máx. de páginas por consulta (50 registros cada):", 1, 40, 10)

    st.divider()
    st.header("🔎 Palavras-chave")
    termos_txt = st.text_area(
        "Incluir (separe por vírgula):", "",
        placeholder="Em branco = traz tudo. Ex.: veículo, pneu, decoração natalina",
        help="Acentos e maiúsculas são ignorados; 'pneu' encontra 'pneus' e 'pneumáticos'. "
             "Depois de digitar, clique fora da caixa (ou Ctrl+Enter) para aplicar.",
    )
    regra = st.radio("Regra:", ["Qualquer termo", "Todos os termos"], horizontal=True)
    excluir_txt = st.text_input("Excluir se contiver:", "", placeholder="Ex.: locação com motorista")
    buscar_complemento = st.checkbox("Procurar também em 'Informação complementar'", value=True)

    st.divider()
    st.header("📍 Distância")
    rotulos_mun = municipios["rotulo"].tolist()
    padrao_origem = "Doutor Ulysses - PR"
    origem_rotulo = st.selectbox("Cidade de referência:", rotulos_mun,
                                 index=rotulos_mun.index(padrao_origem) if padrao_origem in rotulos_mun else 0)

    executar = st.button("🚀 Executar varredura", type="primary")

# ---------------------------------------------------------------------------
# Execução da varredura (só quando o botão é clicado)
# ---------------------------------------------------------------------------
if executar:
    erros = []
    if not todas_ufs and not ufs_sel:
        erros.append("Selecione ao menos um estado ou marque 'Todo o Brasil'.")
    if not cods_mod:
        erros.append("Selecione ao menos uma modalidade.")
    if modo == "periodo" and not data_ini:
        erros.append("Informe a data inicial e a final do período.")
    for e in erros:
        st.error(f"⚠️ {e}")

    if not erros:
        linha_origem = municipios.loc[municipios["rotulo"] == origem_rotulo].iloc[0]
        origem = (linha_origem["latitude"], linha_origem["longitude"])
        tarefas = montar_tarefas(modo, ufs_sel or [None], cods_mod, data_ini, data_fim,
                                 cod_ibge_api, cnpj_api)
        pausas_antes = LIMITADOR.total_429
        with st.spinner(f"Consultando o PNCP ({len(tarefas)} combinações UF × modalidade)..."):
            df_bruto, falhas, truncados = executar_varredura(tarefas, max_paginas, modalidades,
                                                             origem, municipios)
        st.session_state["resultado"] = {
            "df": df_bruto, "falhas": falhas, "truncados": truncados,
            "origem": origem_rotulo, "quando": datetime.now(), "modo": modo_label,
            "pausas": LIMITADOR.total_429 - pausas_antes,
        }

# ---------------------------------------------------------------------------
# Resultados + filtros pós-busca (não refazem a consulta)
# ---------------------------------------------------------------------------
resultado = st.session_state.get("resultado")
if not resultado:
    st.info("👈 Configure a busca na barra lateral e clique em **Executar varredura**.")
    st.stop()

df = resultado["df"]
st.caption(f"Última varredura: {resultado['quando']:%d/%m/%Y %H:%M} · modo **{resultado['modo']}** · "
           f"distâncias a partir de **{resultado['origem']}** (linha reta).")

if resultado.get("pausas"):
    st.info(f"⏳ O PNCP pediu para diminuir o ritmo {resultado['pausas']} vez(es); a busca aguardou "
            "e continuou automaticamente.")
if resultado["falhas"]:
    with st.expander(f"⚠️ {len(resultado['falhas'])} consulta(s) com falha — resultados podem estar incompletos"):
        st.dataframe(pd.DataFrame(resultado["falhas"]), hide_index=True)
if resultado["truncados"]:
    st.warning("Algumas consultas tinham mais páginas do que o limite configurado: "
               + "; ".join(resultado["truncados"])
               + ". Aumente o limite de páginas ou refine UF/município.")

if df.empty:
    st.warning("🔍 A API não retornou contratações para os parâmetros informados.")
    st.stop()

# --- Palavras-chave (aplicadas localmente, permitem ajuste sem nova consulta) ---
incluir = compilar_termos(termos_txt)
excluir = compilar_termos(excluir_txt)
texto_busca = df["Objeto"].fillna("")
if buscar_complemento:
    texto_busca = texto_busca + " " + df["Info. Complementar"].fillna("")
texto_busca = texto_busca.map(normalizar)

mascara = pd.Series(True, index=df.index)
if incluir:
    agreg = all if regra == "Todos os termos" else any
    mascara &= texto_busca.map(lambda t: agreg(p.search(t) for p in incluir))
if excluir:
    mascara &= ~texto_busca.map(lambda t: any(p.search(t) for p in excluir))
ocultos_palavras = int((~mascara).sum())
df = df[mascara].copy()

agora = datetime.now()
df["Dias p/ Encerrar"] = pd.to_numeric(df["Encerramento Propostas"].map(
    lambda d: (d - agora).days if isinstance(d, datetime) and not pd.isna(d) else None),
    errors="coerce").astype("Int64")
df["Status Prazo"] = df["Encerramento Propostas"].map(
    lambda d: "Sem data" if not isinstance(d, datetime) or pd.isna(d)
    else ("Aberto" if d >= agora else "Encerrado"))

st.subheader("Refinar resultados")
c1, c2, c3, c4 = st.columns(4)
with c1:
    status_sel = st.multiselect("Prazo de propostas:", ["Aberto", "Encerrado", "Sem data"],
                                default=["Aberto", "Sem data"] if resultado["modo"] == "Propostas em aberto"
                                else ["Aberto", "Encerrado", "Sem data"])
with c2:
    esferas_sel = st.multiselect("Esfera:", sorted(df["Esfera"].unique()))
with c3:
    plataformas_sel = st.multiselect("Plataforma:", sorted(df["Plataforma"].unique()))
with c4:
    mods_res_sel = st.multiselect("Modalidade:", sorted(df["Modalidade"].unique()))

c5, c6, c7 = st.columns([2, 2, 1])
with c5:
    df["Distância (km)"] = pd.to_numeric(df["Distância (km)"], errors="coerce")
    dist_valida = df["Distância (km)"].dropna()
    raio_max = int(dist_valida.max()) + 1 if not dist_valida.empty else 100
    raio = st.slider("Raio máximo (km):", 0, max(raio_max, 50), max(raio_max, 50), step=10)
    incluir_sem_dist = st.checkbox("Manter itens sem distância calculada", value=True)
with c6:
    vmin, vmax = st.columns(2)
    valor_min = vmin.number_input("Valor estimado mín. (R$):", min_value=0.0, value=0.0, step=1000.0)
    valor_max = vmax.number_input("Valor estimado máx. (R$) — 0 = sem limite:", min_value=0.0,
                                  value=0.0, step=1000.0)
    texto_livre = st.text_input("Buscar em órgão/município/objeto:")
with c7:
    somente_srp = st.checkbox("Somente SRP")
    ordenar = st.selectbox("Ordenar por:", ["Encerramento", "Distância", "Valor", "Publicação"])

f = df.copy()
if status_sel:
    f = f[f["Status Prazo"].isin(status_sel)]
if esferas_sel:
    f = f[f["Esfera"].isin(esferas_sel)]
if plataformas_sel:
    f = f[f["Plataforma"].isin(plataformas_sel)]
if mods_res_sel:
    f = f[f["Modalidade"].isin(mods_res_sel)]
dist_ok = f["Distância (km)"] <= raio
if incluir_sem_dist:
    dist_ok |= f["Distância (km)"].isna()
f = f[dist_ok]
valores = pd.to_numeric(f["Valor Estimado (R$)"], errors="coerce").fillna(0)
f = f[valores >= valor_min]
if valor_max > 0:
    f = f[pd.to_numeric(f["Valor Estimado (R$)"], errors="coerce").fillna(0) <= valor_max]
if somente_srp:
    f = f[f["SRP"] == "Sim"]
if texto_livre:
    alvo = normalizar(texto_livre)
    f = f[(f["Órgão"] + " " + f["Município"] + " " + f["Objeto"]).map(normalizar).str.contains(alvo, regex=False)]

coluna_ordem = {"Encerramento": ("Encerramento Propostas", True), "Distância": ("Distância (km)", True),
                "Valor": ("Valor Estimado (R$)", False), "Publicação": ("Publicação PNCP", False)}[ordenar]
f = f.sort_values(coluna_ordem[0], ascending=coluna_ordem[1], na_position="last")

# --- Métricas ---
m1, m2, m3, m4 = st.columns(4)
m1.metric("Editais encontrados", f"{len(f):,}".replace(",", "."),
          help=f"{len(resultado['df'])} retornados pela API antes dos filtros")
total_valor = pd.to_numeric(f["Valor Estimado (R$)"], errors="coerce").sum()
m2.metric("Valor estimado somado",
          f"R$ {total_valor:,.0f}".replace(",", "X").replace(".", ",").replace("X", "."))
m3.metric("Com propostas abertas", int((f["Status Prazo"] == "Aberto").sum()))
m4.metric("Encerram em até 3 dias", int(f["Dias p/ Encerrar"].between(0, 3).sum()))

ocultos_filtros = len(df) - len(f)
if ocultos_palavras or ocultos_filtros:
    partes = []
    if ocultos_palavras:
        partes.append(f"**{ocultos_palavras}** pelas palavras-chave (barra lateral)")
    if ocultos_filtros:
        partes.append(f"**{ocultos_filtros}** pelos filtros acima (prazo, esfera, raio, valor...)")
    st.info(f"🔎 A API trouxe {len(resultado['df'])} contratação(ões). Ficaram ocultas: "
            + " e ".join(partes) + ".")

if f.empty:
    st.warning("Nenhum edital atende aos filtros. Afrouxe as palavras-chave, o raio ou o prazo.")
    st.stop()

colunas_exibir = ["Status Prazo", "Dias p/ Encerrar", "Encerramento Propostas", "UF", "Município",
                  "Distância (km)", "Modalidade", "Objeto", "Valor Estimado (R$)", "Órgão",
                  "Esfera", "Plataforma", "SRP", "Situação", "Nº Compra", "Link PNCP",
                  "Link Plataforma", "Publicação PNCP", "Abertura Propostas", "Modo de Disputa",
                  "Amparo Legal", "Processo", "Unidade", "Info. Complementar", "Controle PNCP"]
f = f[colunas_exibir]
mostrar_tabela(f)

# --- Exportação ---
carimbo = datetime.now().strftime("%Y%m%d_%H%M")
d1, d2, _ = st.columns([1, 1, 3])
try:
    d1.download_button("📥 Baixar Excel", gerar_excel(f), file_name=f"Radar_PNCP_{carimbo}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
except Exception as exc:  # a tabela e o CSV continuam disponíveis mesmo se o Excel falhar
    d1.warning(f"Não foi possível gerar o Excel ({type(exc).__name__}). Use o CSV.")
d2.download_button("📄 Baixar CSV", f.to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig"),
                   file_name=f"Radar_PNCP_{carimbo}.csv", mime="text/csv")

st.caption("Os dados vêm diretamente do PNCP e refletem o que cada órgão publicou. Confira sempre o "
           "edital e seus anexos no link oficial antes de decidir participar.")
