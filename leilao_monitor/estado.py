"""Memória do robô (SQLite) + canais de aviso.

A memória existe pra duas coisas:
  1. não te avisar duas vezes do mesmo lote;
  2. te avisar de novo se o lance subiu ou o status mudou.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS lotes (
    chave         TEXT PRIMARY KEY,
    fonte         TEXT NOT NULL,
    id_lote       TEXT NOT NULL,
    titulo        TEXT NOT NULL,
    url           TEXT,
    alvo_id       TEXT,
    status        TEXT,
    ano_fab       INTEGER,
    preco         REAL,
    local         TEXT,
    encerramento  TEXT,
    motivos       TEXT,
    visto_em      TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON lotes(status);
CREATE INDEX IF NOT EXISTS idx_alvo ON lotes(alvo_id);
"""

# Colunas adicionadas depois: bancos antigos ganham via ALTER TABLE.
COLUNAS_NOVAS = {
    "uf": "TEXT",
    "fipe": "REAL",
    "fipe_ref": "TEXT",
    "custo_total": "REAL",
    "pct_fipe": "REAL",
}


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Memoria:
    def __init__(self, caminho: str = "leiloes.db"):
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(caminho)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQUEMA)
        existentes = {r["name"] for r in self.con.execute("PRAGMA table_info(lotes)")}
        for coluna, tipo in COLUNAS_NOVAS.items():
            if coluna not in existentes:
                self.con.execute(f"ALTER TABLE lotes ADD COLUMN {coluna} {tipo}")
        self.con.commit()

    def registrar(self, lote, avaliacao, valores: Optional[dict] = None) -> Optional[str]:
        """Grava/atualiza. Devolve 'novo', 'preco' ou 'status' se vale avisar;
        None se nada mudou.

        valores: fipe, fipe_ref, custo_total, pct_fipe (ver comparar_fipe)."""
        valores = valores or {}
        extras = (
            lote.uf, valores.get("fipe"), valores.get("fipe_ref"),
            valores.get("custo_total"), valores.get("pct_fipe"),
        )
        linha = self.con.execute(
            "SELECT status, preco FROM lotes WHERE chave = ?", (lote.chave,)
        ).fetchone()

        motivos = json.dumps(
            {
                "motivos": avaliacao.motivos,
                "sinais": avaliacao.sinais,
                "observacoes": avaliacao.observacoes,
            },
            ensure_ascii=False,
        )
        agora = _agora()

        if linha is None:
            self.con.execute(
                """INSERT INTO lotes (chave, fonte, id_lote, titulo, url, alvo_id,
                   status, ano_fab, preco, local, encerramento, motivos,
                   visto_em, atualizado_em, uf, fipe, fipe_ref, custo_total, pct_fipe)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lote.chave, lote.fonte, lote.id_lote, lote.titulo, lote.url,
                    avaliacao.alvo_id, avaliacao.status, avaliacao.ano_fab,
                    lote.preco, lote.local, lote.encerramento, motivos, agora, agora,
                    *extras,
                ),
            )
            self.con.commit()
            return "novo"

        mudou = None
        if linha["status"] != avaliacao.status:
            mudou = "status"
        elif lote.preco is not None and linha["preco"] is not None:
            if abs(lote.preco - linha["preco"]) > 0.01:
                mudou = "preco"

        self.con.execute(
            """UPDATE lotes SET status=?, preco=?, motivos=?, encerramento=?,
               atualizado_em=?, local=?, uf=?, fipe=?, fipe_ref=?, custo_total=?,
               pct_fipe=? WHERE chave=?""",
            (avaliacao.status, lote.preco, motivos, lote.encerramento, agora,
             lote.local, *extras, lote.chave),
        )
        self.con.commit()
        return mudou

    def listar(self, status: Optional[list[str]] = None) -> list[sqlite3.Row]:
        if status:
            marcas = ",".join("?" * len(status))
            sql = f"SELECT * FROM lotes WHERE status IN ({marcas}) ORDER BY atualizado_em DESC"
            return self.con.execute(sql, status).fetchall()
        return self.con.execute(
            "SELECT * FROM lotes ORDER BY atualizado_em DESC"
        ).fetchall()

    def fechar(self):
        self.con.close()


# --------------------------------------------------------------------------
# Avisos
# --------------------------------------------------------------------------


def _reais(v: float) -> str:
    return f"R$ {v:,.0f}".replace(",", ".")


def formatar(lote, avaliacao, motivo_aviso: str, valores: Optional[dict] = None) -> str:
    valores = valores or {}
    emoji = {"APROVADO": "✅", "VERIFICAR": "⚠️", "BLOQUEADO": "⛔"}.get(
        avaliacao.status, "•"
    )
    rotulo = {"novo": "NOVO LOTE", "preco": "LANCE ALTERADO", "status": "STATUS MUDOU"}
    linhas = [
        f"{emoji} {rotulo.get(motivo_aviso, motivo_aviso)} — {avaliacao.status}",
        f"{lote.titulo}",
        f"alvo: {avaliacao.alvo_id} | fab: {avaliacao.ano_fab or '?'}",
        f"fonte: {lote.fonte}" + (f" | {lote.local}" if lote.local else ""),
    ]
    if lote.preco is not None:
        linhas.append(f"lance: R$ {lote.preco:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    if valores.get("custo_total") and valores["custo_total"] != lote.preco:
        linhas.append(
            f"custo estimado: {_reais(valores['custo_total'])} "
            f"(+{lote.comissao_pct or 0:g}% comissão + {_reais(lote.despesas or 0)} despesas)"
        )
    if valores.get("fipe"):
        pct = f" → custo = {valores['pct_fipe']:.0f}% da FIPE" if valores.get("pct_fipe") else ""
        linhas.append(f"FIPE: {_reais(valores['fipe'])} ({valores.get('fipe_ref', '')}){pct}")
    if lote.encerramento:
        linhas.append(f"encerra: {lote.encerramento}")
    for s in avaliacao.sinais:
        linhas.append(f"  + {s}")
    for o in avaliacao.observacoes:
        linhas.append(f"  ? {o}")
    for m in avaliacao.motivos:
        linhas.append(f"  - {m}")
    if lote.url:
        linhas.append(lote.url)
    return "\n".join(linhas)


class Avisador:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def enviar(self, texto: str) -> None:
        if self.cfg.get("console", True):
            print("\n" + texto + "\n" + "-" * 60)

        # token/chat_id podem vir do ambiente pra não irem pro git
        tg = dict(self.cfg.get("telegram") or {})
        tg.setdefault("token", os.environ.get("TELEGRAM_TOKEN"))
        tg.setdefault("chat_id", os.environ.get("TELEGRAM_CHAT_ID"))
        if tg.get("token") and tg.get("chat_id"):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                    json={
                        "chat_id": tg["chat_id"],
                        "text": texto,
                        "disable_web_page_preview": False,
                    },
                    timeout=15,
                )
            except Exception as e:
                log.warning("telegram falhou: %s", e)

        gancho = self.cfg.get("webhook")
        if gancho:
            try:
                requests.post(gancho, json={"text": texto}, timeout=15)
            except Exception as e:
                log.warning("webhook falhou: %s", e)
