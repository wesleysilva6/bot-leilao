"""Memória do robô (SQLite) + canais de aviso.

A memória existe pra duas coisas:
  1. não te avisar duas vezes do mesmo lote;
  2. te avisar de novo se o lance subiu ou o status mudou.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
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


_STATUS = {
    "APROVADO": ("✅", "APROVADO"),
    "VERIFICAR": ("⚠️", "VERIFICAR"),
    "BLOQUEADO": ("⛔", "BLOQUEADO"),
}
_MOTIVO = {"novo": "Novo lote", "preco": "Lance mudou", "status": "Status mudou"}
_CATEGORIA = {
    "retomado_financiamento": "Retomado de financiamento",
    "pequena_monta": "Pequena monta",
    "conservado": "Bom estado / doc. regular",
    "monta_alta": "Média/grande monta ou sucata",
    "remarcacao": "Chassi/motor remarcado",
}


@dataclass
class Mensagem:
    html: str                 # Telegram (parse_mode=HTML)
    imagem: str = ""          # URL da 1ª foto do veículo
    url: str = ""

    @property
    def texto(self) -> str:
        """Versão sem tags, pro console e webhook."""
        sem_tags = re.sub(r'<a href="([^"]+)">[^<]*</a>', r"\1", self.html)
        return html.unescape(re.sub(r"<[^>]+>", "", sem_tags))


def _item(achado: str) -> str:
    """"retomado_financiamento: 'financeira'" -> "Retomado de financiamento (financeira)"."""
    cat, _, trecho = achado.partition(": ")
    nome = _CATEGORIA.get(cat)
    if not nome:
        return achado
    trecho = trecho.strip("'")
    return f"{nome} ({trecho})" if trecho else nome


def _local_curto(local: str) -> str:
    """Endereço do pátio vira só a cidade/UF: '..., SP304 - SANTA BARBARA D OESTE/SP'."""
    local = (local or "").strip()
    return local.rsplit(" - ", 1)[-1].strip() if " - " in local and len(local) > 40 else local


def formatar(lote, avaliacao, motivo_aviso: str, valores: Optional[dict] = None,
             alvo_descricao: str = "") -> Mensagem:
    valores = valores or {}
    e = html.escape
    emoji, status = _STATUS.get(avaliacao.status, ("•", avaliacao.status))

    linhas = [
        f"{emoji} <b>{status}</b>  ·  {_MOTIVO.get(motivo_aviso, motivo_aviso)}",
        f"<b>{e(lote.titulo)}</b>",
        "",
    ]

    modelo = re.sub(r"\s*\(.*\)\s*$", "", alvo_descricao) or avaliacao.alvo_id
    ano = avaliacao.ano_fab or "?"
    if avaliacao.ano_mod and avaliacao.ano_mod != avaliacao.ano_fab:
        ano = f"{ano}/{avaliacao.ano_mod}"
    linhas.append(f"🚗 {e(modelo)}  ·  {ano}")
    onde = " · ".join(x for x in (_local_curto(lote.local), lote.fonte) if x)
    linhas.append(f"📍 {e(onde)}")
    if lote.encerramento:
        linhas.append(f"📅 Encerra {e(lote.encerramento)}")

    # bloco de valores
    valores_linhas = []
    if lote.preco is not None:
        valores_linhas.append(f"💰 Lance: <b>{_reais(lote.preco)}</b>")
    else:
        valores_linhas.append("💰 Lance: <i>ainda sem lance</i>")
    custo = valores.get("custo_total")
    if custo and lote.preco is not None and abs(custo - lote.preco) > 1:
        valores_linhas.append(
            f"🧾 Custo total: <b>{_reais(custo)}</b>\n"
            f"      +{lote.comissao_pct or 0:g}% comissão + {_reais(lote.despesas or 0)} despesas"
        )
    if valores.get("fipe"):
        pct = valores.get("pct_fipe")
        extra = f"  →  <b>{pct:.0f}% da FIPE</b>" if pct else ""
        valores_linhas.append(f"📊 FIPE: {_reais(valores['fipe'])}{extra}")
    linhas += ["", *valores_linhas]

    # o que o lote diz
    for titulo, itens in (
        ("👍 A favor", avaliacao.sinais),
        ("🔎 Conferir", avaliacao.observacoes),
        ("🚫 Bloqueios", avaliacao.motivos),
    ):
        if itens:
            linhas += ["", f"<b>{titulo}</b>", *(f"• {e(_item(i))}" for i in itens)]

    if lote.url:
        linhas += ["", f'🔗 <a href="{e(lote.url, quote=True)}">Abrir o lote</a>']

    return Mensagem(html="\n".join(linhas), imagem=lote.imagem, url=lote.url)


TELEGRAM_API = "https://api.telegram.org/bot{token}/{metodo}"


LIMITE_LEGENDA = 1024  # Telegram: legenda de foto; texto comum vai até 4096


def _telegram(token: str, metodo: str, arquivos: Optional[dict] = None, **payload) -> dict:
    """Chama a API do Telegram. O erro nunca inclui a URL: ela contém o token."""
    url = TELEGRAM_API.format(token=token, metodo=metodo)
    try:
        if arquivos:  # upload multipart: campos vão como form-data
            r = requests.post(url, data=payload, files=arquivos, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=15)
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

    def _foto(self, imagem: str, legenda: str) -> dict:
        """Manda a foto pela URL; se o Telegram não conseguir baixar (CDN
        bloqueando, extensão estranha), baixa aqui e faz upload."""
        tg = self.telegram
        base = {"chat_id": tg["chat_id"], "parse_mode": "HTML"}
        if legenda:
            base["caption"] = legenda
        resp = _telegram(tg["token"], "sendPhoto", photo=imagem, **base)
        if resp.get("ok"):
            return resp
        try:
            r = requests.get(imagem, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
        except Exception as e:
            return {"ok": False, "description": f"foto indisponível ({type(e).__name__})"}
        return _telegram(tg["token"], "sendPhoto", arquivos={"photo": ("foto.jpg", r.content)}, **base)

    def enviar_telegram(self, msg: "Mensagem | str") -> Optional[str]:
        """Foto do veículo com a mensagem na legenda. Devolve None se enviou,
        ou a descrição do erro."""
        tg = self.telegram
        if not (tg.get("token") and tg.get("chat_id")):
            return "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID não configurados"
        if isinstance(msg, str):
            msg = Mensagem(html=html.escape(msg))

        texto_cabe_na_legenda = len(msg.texto) <= LIMITE_LEGENDA
        if msg.imagem:
            resp = self._foto(msg.imagem, msg.html if texto_cabe_na_legenda else "")
            if resp.get("ok") and texto_cabe_na_legenda:
                return None
            if not resp.get("ok"):
                log.warning("foto não enviada (%s), mandando só o texto", resp.get("description"))

        resp = _telegram(tg["token"], "sendMessage", chat_id=tg["chat_id"],
                         text=msg.html[:4000], parse_mode="HTML",
                         disable_web_page_preview=True)
        return None if resp.get("ok") else resp.get("description", "erro desconhecido")

    def enviar(self, msg: Mensagem) -> None:
        if self.cfg.get("console", True):
            print("\n" + msg.texto + "\n" + "-" * 60)

        if self.telegram.get("token") and self.telegram.get("chat_id"):
            erro = self.enviar_telegram(msg)
            if erro:
                log.warning("telegram falhou: %s", erro)

        gancho = self.cfg.get("webhook")
        if gancho:
            try:
                requests.post(gancho, json={"text": msg.texto, "imagem": msg.imagem,
                                            "url": msg.url}, timeout=15)
            except Exception as e:
                log.warning("webhook falhou: %s", e)
