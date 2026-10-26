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


TELEGRAM_API = "https://api.telegram.org/bot{token}/{metodo}"


def _telegram(token: str, metodo: str, **payload) -> dict:
    """Chama a API do Telegram. O erro nunca inclui a URL: ela contém o token."""
    try:
        r = requests.post(TELEGRAM_API.format(token=token, metodo=metodo),
                          json=payload, timeout=15)
        dados = r.json()
    except Exception as e:
        return {"ok": False, "description": f"sem resposta ({type(e).__name__})"}
    return dados


def descobrir_chats(token: str) -> list[dict]:
    """Quem mandou mensagem pro bot recentemente: [{chat_id, nome}]."""
    resp = _telegram(token, "getUpdates")
    if not resp.get("ok"):
        raise RuntimeError(resp.get("description", "erro desconhecido"))
    chats = {}
    for upd in resp.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat")
        if chat:
            nome = chat.get("title") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p
            )
            chats[chat["id"]] = {"chat_id": chat["id"], "nome": nome, "tipo": chat.get("type")}
    return list(chats.values())


class Avisador:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        # token/chat_id podem vir do ambiente pra não irem pro git
        self.telegram = dict(self.cfg.get("telegram") or {})
        self.telegram.setdefault("token", os.environ.get("TELEGRAM_TOKEN"))
        self.telegram.setdefault("chat_id", os.environ.get("TELEGRAM_CHAT_ID"))

    def enviar_telegram(self, texto: str) -> Optional[str]:
        """Devolve None se enviou, ou a descrição do erro."""
        tg = self.telegram
        if not (tg.get("token") and tg.get("chat_id")):
            return "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID não configurados"
        resp = _telegram(tg["token"], "sendMessage", chat_id=tg["chat_id"],
                         text=texto[:4000], disable_web_page_preview=True)
        return None if resp.get("ok") else resp.get("description", "erro desconhecido")

    def enviar(self, texto: str) -> None:
        if self.cfg.get("console", True):
            print("\n" + texto + "\n" + "-" * 60)

        if self.telegram.get("token") and self.telegram.get("chat_id"):
            erro = self.enviar_telegram(texto)
            if erro:
                log.warning("telegram falhou: %s", erro)

        gancho = self.cfg.get("webhook")
        if gancho:
            try:
                requests.post(gancho, json={"text": texto}, timeout=15)
            except Exception as e:
                log.warning("webhook falhou: %s", e)
