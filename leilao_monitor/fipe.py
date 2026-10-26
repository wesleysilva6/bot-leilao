"""Consulta da tabela FIPE (API pública parallelum) com cache em SQLite.

A API gratuita aceita ~500 consultas/dia, e a FIPE só muda uma vez por mês,
então cada (modelo, ano) é consultado no máximo a cada `cache_dias`.

No config, cada alvo diz qual modelo FIPE usar:

    fipe:
      marca: 25                      # Honda
      modelos:
        - {codigo: 4010, se: [aut, cvt, automatico]}   # LXS automático
        - {codigo: 4011, se: [mec, manual]}            # LXS manual
        - {codigo: 4010}                               # padrão se nada bater
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from .fontes import _preco
from .matcher import contem_termo, normalizar

log = logging.getLogger(__name__)

API = "https://parallelum.com.br/fipe/api/v1/carros/marcas"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS fipe_cache (
    chave        TEXT PRIMARY KEY,   -- marca/modelo/ano
    valor        REAL,               -- NULL = ano não existe na FIPE
    modelo       TEXT,
    referencia   TEXT,
    consultado_em TEXT NOT NULL
);
"""


@dataclass
class ValorFipe:
    valor: float
    modelo: str
    referencia: str  # "outubro de 2026"


class Fipe:
    def __init__(self, con: sqlite3.Connection, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.con = con
        self.con.executescript(ESQUEMA)
        self.cache_dias = self.cfg.get("cache_dias", 15)
        self.sessao = requests.Session()

    @staticmethod
    def escolher_modelo(cfg_alvo: dict, texto: str) -> Optional[int]:
        """Primeiro modelo cuja lista `se` bate no texto; sem `se` = padrão."""
        t = normalizar(texto)
        for m in cfg_alvo.get("modelos", []):
            termos = m.get("se")
            if not termos or any(contem_termo(t, x) for x in termos):
                return m["codigo"]
        return None

    def consultar(self, cfg_alvo: Optional[dict], texto: str, ano_modelo: Optional[int]) -> Optional[ValorFipe]:
        if not cfg_alvo or not ano_modelo:
            return None
        marca = cfg_alvo["marca"]
        modelo = self.escolher_modelo(cfg_alvo, texto)
        if modelo is None:
            return None

        chave = f"{marca}/{modelo}/{ano_modelo}"
        linha = self.con.execute(
            "SELECT valor, modelo, referencia, consultado_em FROM fipe_cache WHERE chave=?",
            (chave,),
        ).fetchone()
        if linha is not None:
            idade = datetime.now(timezone.utc) - datetime.fromisoformat(linha[3])
            if idade < timedelta(days=self.cache_dias):
                return ValorFipe(linha[0], linha[1], linha[2]) if linha[0] else None

        try:
            achado = self._buscar_api(marca, modelo, ano_modelo)
        except Exception as e:  # falha de rede não vai pro cache
            log.warning("FIPE indisponível para %s: %s", chave, e)
            return None
        self.con.execute(
            "INSERT OR REPLACE INTO fipe_cache VALUES (?,?,?,?,?)",
            (
                chave,
                achado.valor if achado else None,
                achado.modelo if achado else None,
                achado.referencia if achado else None,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.con.commit()
        return achado

    def _buscar_api(self, marca: int, modelo: int, ano: int) -> Optional[ValorFipe]:
        base = f"{API}/{marca}/modelos/{modelo}/anos"
        r = self.sessao.get(base, timeout=20)
        r.raise_for_status()
        # o sufixo muda com o combustível: 2014-1 gasolina, 2014-5 flex...
        codigo = next((a["codigo"] for a in r.json() if a["codigo"].startswith(f"{ano}-")), None)
        if codigo is None:
            return None
        r = self.sessao.get(f"{base}/{codigo}", timeout=20)
        r.raise_for_status()
        d = r.json()
        return ValorFipe(_preco(d["Valor"]), d["Modelo"], d["MesReferencia"])


def custo_total(preco: Optional[float], comissao_pct: Optional[float], despesas: Optional[float]) -> Optional[float]:
    """Lance + comissão do leiloeiro + despesas fixas (pátio, documentação)."""
    if preco is None:
        return None
    return preco * (1 + (comissao_pct or 0) / 100) + (despesas or 0)
