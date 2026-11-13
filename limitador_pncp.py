"""
Controle de ritmo das consultas ao PNCP (evita o erro HTTP 429 - "Limite de
requisições excedido").

- Intervalo mínimo entre requisições, compartilhado por todas as threads.
- Ao receber 429, TODAS as consultas pausam juntas (respeitando o cabeçalho
  Retry-After, quando vier) e a mesma página é tentada de novo.
- A cada 429 o intervalo entre requisições aumenta; com respostas normais,
  ele volta aos poucos ao valor inicial.

Usado pelo app (Streamlit) e pelo robô de alertas.
"""

from __future__ import annotations

import re
import threading
import time

INTERVALO_INICIAL = 0.7   # segundos entre requisições
INTERVALO_MAXIMO = 4.0
TENTATIVAS_429 = 6        # esperas de 10, 20, 40, 80, 90, 90 s (no pior caso)


class Limitador:
    def __init__(self, intervalo: float = INTERVALO_INICIAL):
        self._trava = threading.Lock()
        self.intervalo = intervalo
        self._proximo = 0.0
        self._pausa_ate = 0.0
        self.total_429 = 0

    def aguardar(self):
        with self._trava:
            agora = time.monotonic()
            inicio = max(agora, self._proximo, self._pausa_ate)
            self._proximo = inicio + self.intervalo
        espera = inicio - time.monotonic()
        if espera > 0:
            time.sleep(espera)

    def recebeu_429(self, segundos: float):
        with self._trava:
            self.total_429 += 1
            self._pausa_ate = max(self._pausa_ate, time.monotonic() + segundos)
            self.intervalo = min(self.intervalo * 1.5, INTERVALO_MAXIMO)

    def recebeu_ok(self):
        with self._trava:
            self.intervalo = max(INTERVALO_INICIAL, self.intervalo * 0.97)


LIMITADOR = Limitador()


def _espera_sugerida(res, tentativa: int) -> float:
    valor = (res.headers or {}).get("Retry-After", "")
    if str(valor).strip().isdigit():
        return min(float(valor), 120.0)
    return float(min(10 * 2 ** tentativa, 90))


def get_pncp(sessao, url: str, params: dict, timeout: int = 60):
    """GET com ritmo controlado e nova tentativa automática em caso de HTTP 429."""
    res = None
    for tentativa in range(TENTATIVAS_429):
        LIMITADOR.aguardar()
        res = sessao.get(url, params=params, timeout=timeout)
        if res.status_code != 429:
            LIMITADOR.recebeu_ok()
            return res
        LIMITADOR.recebeu_429(_espera_sugerida(res, tentativa))
    return res


def resumo_erro(res) -> str:
    """Mensagem curta e legível (sem o HTML da página de erro)."""
    if res.status_code == 429:
        return ("PNCP limitou as consultas (HTTP 429) mesmo após aguardar. "
                "Tente de novo em alguns minutos ou reduza estados/modalidades.")
    texto = re.sub(r"<[^>]+>", " ", res.text or "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return f"HTTP {res.status_code}: {texto[:150]}"
