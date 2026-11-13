"""
Motor de busca por palavras-chave nos objetos das contratações.

Sintaxe do campo "Incluir" (e também do "Excluir"):

    pneu, autopeças                 vírgula = OU (basta um dos itens)
    peça + veículo                  "+" = as palavras precisam aparecer PERTO uma da outra
                                    (até JANELA palavras de distância, em qualquer ordem)
    "oficina mecânica"              aspas = frase exata
    peça + "linha leve"             pode combinar as duas coisas

Regras gerais:
- Acentos e maiúsculas são ignorados ("Peças" = "pecas").
- Plural e gênero são tolerados: "veículo" encontra "veículos" e "veicular";
  "automotivo" encontra "automotiva(s)"; "peça" encontra "peças".
- Cada palavra casa pelo início: "natalin" encontra "natalina" e "natalino".

Usado pelo app (Streamlit) e pelo robô de alertas.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

JANELA = 12  # distância máxima (em palavras) entre os termos de um item com "+"
RE_PALAVRA = re.compile(r"[a-z0-9]+")


def normalizar(texto) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def _radical(palavra: str) -> str:
    """Radical leve para tolerar plural e gênero (sem cortar palavras curtas)."""
    p = palavra
    if len(p) > 5 and p.endswith(("oes", "aes")):
        return p[:-3]                      # caminhões -> caminh ; licitações -> licitac
    if len(p) > 4 and p.endswith("ao"):
        return p[:-2]                      # caminhão -> caminh ; manutenção -> manutenc
    if len(p) > 5 and p.endswith(("ais", "eis")):
        return p[:-2]                      # prediais -> predia ; automóveis -> automove
    if len(p) >= 6 and p.endswith(("al", "el")):
        return p[:-1]                      # predial -> predia ; automóvel -> automove
    if len(p) > 4 and p.endswith("ns"):
        p = p[:-2] + "m"                   # itens -> item
    elif len(p) > 3 and p.endswith("s"):
        p = p[:-1]                         # peças -> peca
    if len(p) >= 6 and p[-1] in "aeo":
        p = p[:-1]                         # veículo -> veicul ; automotivo -> automotiv
    return p


def tokens(texto: str) -> list[str]:
    return RE_PALAVRA.findall(normalizar(texto))


@dataclass
class Parte:
    palavras: list[str]   # radicais; mais de uma = frase exata
    rotulo: str

    def posicoes(self, toks: list[str]) -> list[int]:
        n = len(self.palavras)
        achados = []
        for i in range(len(toks) - n + 1):
            if all(toks[i + k].startswith(self.palavras[k]) for k in range(n)):
                achados.append(i)
        return achados


@dataclass
class Item:
    partes: list[Parte]
    rotulo: str

    def casa(self, toks: list[str]) -> bool:
        pos = [p.posicoes(toks) for p in self.partes]
        if any(not x for x in pos):
            return False
        if len(pos) == 1:
            return True
        # existe uma ocorrência de cada parte dentro da mesma janela?
        for p0 in pos[0]:
            if all(any(abs(q - p0) <= JANELA for q in outras) for outras in pos[1:]):
                return True
        return False


def _parte(texto: str) -> Parte | None:
    texto = texto.strip()
    frase = texto.startswith('"') and texto.endswith('"') and len(texto) > 2
    palavras = tokens(texto.strip('"'))
    if not palavras:
        return None
    if frase or len(palavras) > 1:
        # frase: última palavra tolera plural, as demais exatas pelo início
        radicais = palavras[:-1] + [_radical(palavras[-1])]
    else:
        radicais = [_radical(palavras[0])]
    return Parte(radicais, texto)


def compilar(texto_consulta: str) -> list[Item]:
    """Transforma o texto do campo em itens de busca."""
    itens = []
    # vírgula/ponto e vírgula separam itens, mas não dentro de aspas
    pedacos = re.findall(r'(?:"[^"]*"|[^,;])+', str(texto_consulta or ""))
    for pedaco in pedacos:
        partes = [_parte(p) for p in pedaco.split("+")]
        partes = [p for p in partes if p]
        if partes:
            itens.append(Item(partes, " + ".join(p.rotulo for p in partes)))
    return itens


def avaliar(texto: str, incluir: list[Item], excluir: list[Item], todos: bool = False):
    """
    Retorna (passou, termos_encontrados, pontuacao).
    - passou: True se atende ao "incluir" e não cai no "excluir".
    - termos_encontrados: rótulos dos itens que casaram (para mostrar ao usuário).
    - pontuacao: quantidade de itens encontrados (maior = mais aderente).
    """
    toks = tokens(texto)
    if excluir and any(it.casa(toks) for it in excluir):
        return False, [], 0
    if not incluir:
        return True, [], 0
    achados = [it.rotulo for it in incluir if it.casa(toks)]
    passou = (len(achados) == len(incluir)) if todos else bool(achados)
    return passou, achados, len(achados)


# ---------------------------------------------------------------------------
# Temas prontos (ponto de partida; o usuário pode editar depois de escolher)
# ---------------------------------------------------------------------------
TEMAS = {
    "Automotivo (peças, pneus e manutenção de frota)": {
        "incluir": ('autopeça, peça + veículo, peça + automotiv, peça + frota, peça + caminhão, '
                    'peça + ônibus, peça + motocicleta, peça + "máquinas pesadas", pneu, "câmara de ar", '
                    'lubrificante, óleo + motor, filtro + veículo, bateria + automotiv, bateria + veículo, '
                    'manutenção + frota, manutenção + veículo, reparo + veículo, "oficina mecânica", '
                    'mecânica + veículo, retífica + motor, funilaria, recapagem, "serviço de guincho"'),
        "excluir": ('equipamento + hospitalar, equipamento + médico, odontológico, laboratorial, '
                    'predial, edificação, "ar condicionado", "ar-condicionado", elevador, '
                    'impressora, eletrodoméstico'),
    },
    "Veículos (aquisição e locação)": {
        "incluir": ('aquisição + veículo, aquisição + automóvel, aquisição + caminhão, aquisição + ônibus, '
                    'aquisição + motocicleta, aquisição + ambulância, aquisição + "micro-ônibus", '
                    'aquisição + van, aquisição + "zero quilômetro", "0 km", locação + veículo, '
                    'locação + automóvel, locação + caminhão, "veículo novo", "veículos novos"'),
        "excluir": ('peça + veículo, "manutenção de veículos", "manutenção da frota", combustível, '
                    'pneu, "transporte escolar", "com motorista"'),
    },
    "Decoração natalina": {
        "incluir": ('natalino, "decoração de natal", "árvore de natal", "papai noel", '
                    'ornamentação + natal, iluminação + natal, "iluminação decorativa", "cascata de led", '
                    'pisca-pisca, enfeite + natal'),
        "excluir": 'pré-natal, prenatal, natalidade, "cesta de natal", "cesta natalina"',
    },
    "Limpeza e higiene": {
        "incluir": ('"material de limpeza", "materiais de limpeza", "produtos de limpeza", higienização, '
                    'saneante, detergente, desinfetante, "papel higiênico", "papel toalha", "saco de lixo", '
                    '"serviço de limpeza", "limpeza e conservação"'),
        "excluir": '"limpeza urbana", "limpeza de fossa", "limpeza de bueiro", hospitalar + cirúrgico',
    },
    "Gêneros alimentícios e merenda": {
        "incluir": ('"gêneros alimentícios", "gênero alimentício", merenda, "alimentação escolar", hortifruti, '
                    'hortifrutigranjeiro, "carne bovina", panificação, "agricultura familiar", "cesta básica"'),
        "excluir": '"ração animal", "alimentação parenteral", "nutrição enteral"',
    },
    "Informática e tecnologia": {
        "incluir": ('computador, notebook, microcomputador, impressora, toner, cartucho + impressora, '
                    '"equipamentos de informática", "material de informática", software, licença + software, '
                    'servidor + dados, nobreak, "rede lógica", switch + rede'),
        "excluir": '"servidor público", "servidores públicos", "servidores municipais"',
    },
    "Obras e serviços de engenharia": {
        "incluir": ('"obra de", "obras de", "serviço de engenharia", "serviços de engenharia", reforma + prédio, '
                    'reforma + escola, reforma + unidade, ampliação + escola, pavimentação, recapeamento, '
                    'drenagem, "construção de", "manutenção predial", "reparo predial"'),
        "excluir": '"reforma de estofado", "reforma de pneu", recapagem',
    },
}
