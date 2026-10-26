"""Bot do Telegram com menu: você escolhe o que buscar e ele procura na hora.

    python main.py --bot

Menu:  🚗 Carros / 🏍️ Motos  →  um botão por alvo (+ "todos")  →  busca em
todas as leiloeiras ativas e devolve só o que bate, com foto, ordenado do
melhor pro pior. Os alertas automáticos (só lote NOVO) podem ser ligados e
desligados pelo próprio menu.

Só responde ao TELEGRAM_CHAT_ID configurado: o bot é público no Telegram,
mas ninguém mais consegue disparar buscas.
"""

from __future__ import annotations

import html
import logging
import os
import time
from typing import Callable, Optional
from urllib.parse import quote

from .ambiente import processo_vivo
from .estado import Avisador, _telegram, formatar

log = logging.getLogger(__name__)

CATEGORIAS = {"carro": "🚗 Carros", "moto": "🏍️ Motos"}
ORDEM_STATUS = {"APROVADO": 0, "VERIFICAR": 1}
MAX_LOTES_POR_BUSCA = 10

ESQUEMA = """
CREATE TABLE IF NOT EXISTS bot_estado (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
"""


def _teclado(linhas: list[list[tuple[str, str]]]) -> dict:
    """[[(texto, callback ou https://url)]] -> inline_keyboard do Telegram."""
    return {
        "inline_keyboard": [
            [
                {"text": t, "url": d} if d.startswith("http") else {"text": t, "callback_data": d}
                for t, d in linha
            ]
            for linha in linhas
        ]
    }


def _rotulo(cfg_alvo: dict) -> str:
    return cfg_alvo.get("rotulo") or cfg_alvo.get("descricao") or cfg_alvo["id"]


def ordenar(resultados: list) -> list:
    """Aprovados primeiro; dentro do status, menor % da FIPE, depois menor lance."""
    def chave(r):
        pct = r.valores.get("pct_fipe")
        preco = r.lote.preco
        return (
            ORDEM_STATUS.get(r.av.status, 9),
            pct if pct is not None else float("inf"),
            preco if preco is not None else float("inf"),
        )
    return sorted(resultados, key=chave)


class Bot:
    def __init__(self, cfg: dict, avisador: Avisador, memoria,
                 analisar: Callable[[], tuple[list, dict]],
                 passada: Callable[[], dict]):
        """analisar/passada: as funções do main já com config, alvos e FIPE
        amarrados (functools.partial)."""
        self.cfg = cfg
        self.cfg_bot = cfg.get("bot") or {}
        self.avisador = avisador
        self.token = avisador.telegram.get("token")
        self.chat_id = str(avisador.telegram.get("chat_id") or "")
        if not (self.token and self.chat_id):
            raise RuntimeError("defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID (veja o README)")
        self.con = memoria.con
        self.con.executescript(ESQUEMA)
        self._analisar = analisar
        self._passada = passada
        self.alvos = {a["id"]: a for a in cfg["alvos"]}
        self.cache_seg = self.cfg_bot.get("cache_minutos", 10) * 60
        self.intervalo_alertas = self.cfg_bot.get("alertas_minutos", 30) * 60
        self._cache: Optional[tuple[float, list]] = None
        self._ultima_passada = 0.0

    # ---------------------------------------------------------------- estado

    def _ler(self, chave: str, padrao: str = "") -> str:
        linha = self.con.execute("SELECT valor FROM bot_estado WHERE chave=?", (chave,)).fetchone()
        return linha[0] if linha else padrao

    def _gravar(self, chave: str, valor) -> None:
        self.con.execute("INSERT OR REPLACE INTO bot_estado VALUES (?,?)", (chave, str(valor)))
        self.con.commit()

    @property
    def alertas_ligados(self) -> bool:
        padrao = "1" if self.cfg_bot.get("alertas", True) else "0"
        return self._ler("alertas", padrao) == "1"

    # ------------------------------------------------------------- telegram

    def _api(self, metodo: str, **payload) -> dict:
        resp = _telegram(self.token, metodo, **payload)
        if not resp.get("ok") and "not modified" not in resp.get("description", ""):
            log.warning("telegram %s: %s", metodo, resp.get("description"))
        return resp

    def _enviar(self, texto: str, teclado: Optional[dict] = None) -> dict:
        extra = {"reply_markup": teclado} if teclado else {}
        return self._api("sendMessage", chat_id=self.chat_id, text=texto,
                         parse_mode="HTML", disable_web_page_preview=True, **extra)

    def _editar(self, msg_id: int, texto: str, teclado: Optional[dict] = None) -> None:
        extra = {"reply_markup": teclado} if teclado else {}
        self._api("editMessageText", chat_id=self.chat_id, message_id=msg_id,
                  text=texto, parse_mode="HTML", disable_web_page_preview=True, **extra)

    # ----------------------------------------------------------------- telas

    def tela_menu(self) -> tuple[str, dict]:
        cats = {a.get("categoria", "carro") for a in self.alvos.values()}
        if self.alertas_ligados:
            alerta = (f"🔔 Alertas automáticos: <b>ligados</b> — te aviso de lote "
                      f"<b>novo</b> a cada {self.intervalo_alertas // 60} min")
            botao_alerta = ("🔕 Desligar alertas", "alertas")
        else:
            alerta = "🔕 Alertas automáticos: <b>desligados</b> — só busco quando você pedir"
            botao_alerta = ("🔔 Ligar alertas", "alertas")
        texto = (
            "🤖 <b>Robô de Leilão</b>\n\n"
            "O que você quer buscar? Eu procuro em todos os leilões e te mostro "
            "só o que bate com os seus filtros.\n\n" + alerta
        )
        linha_cat = [(rot, f"cat:{c}") for c, rot in CATEGORIAS.items()]
        extras = sorted(cats - set(CATEGORIAS))  # categorias novas do config
        linha_cat += [(c.capitalize(), f"cat:{c}") for c in extras]
        return texto, _teclado([linha_cat, [botao_alerta]])

    def tela_categoria(self, cat: str) -> tuple[str, dict]:
        alvos = [a for a in self.alvos.values() if a.get("categoria", "carro") == cat]
        nome = CATEGORIAS.get(cat, cat.capitalize())
        if not alvos:
            texto = (f"{nome}\n\nNenhum modelo cadastrado nessa categoria ainda.\n"
                     f"Adicione um alvo com <code>categoria: {cat}</code> no config.yaml.")
            return texto, _teclado([[("⬅️ Voltar", "menu")]])
        linhas = [[(_rotulo(a), f"alvo:{a['id']}")] for a in alvos]
        if len(alvos) > 1:
            linhas.append([(f"🔎 Todos ({len(alvos)})", f"todos:{cat}")])
        linhas.append([("⬅️ Voltar", "menu")])
        return f"{nome}\n\nQual modelo?", _teclado(linhas)

    # ----------------------------------------------------------------- busca

    def _resultados(self, forcar: bool) -> tuple[list, float]:
        """Todos os lotes que bateram com algum alvo. Uma coleta serve pra
        todos os alvos, então reaproveita por `cache_minutos`."""
        agora = time.time()
        if forcar or not self._cache or agora - self._cache[0] > self.cache_seg:
            resultados, _ = self._analisar()
            self._cache = (agora, resultados)
        return self._cache[1], self._cache[0]

    def buscar(self, msg_id: int, ids: list[str], titulo: str, origem: str, forcar: bool) -> None:
        fontes = [f["nome"] for f in self.cfg.get("fontes", []) if f.get("ativa", True)]
        self._editar(msg_id, f"🔎 Buscando <b>{html.escape(titulo)}</b> em "
                             f"{len(fontes)} leilões…\n<i>pode levar até 1 minuto</i>")
        try:
            todos, quando = self._resultados(forcar)
        except Exception as e:
            log.exception("busca falhou")
            self._editar(msg_id, f"❌ A busca falhou: {html.escape(str(e))}",
                         _teclado([[("🔄 Tentar de novo", f"refaz:{origem}"), ("⬅️ Menu", "menu")]]))
            return

        do_alvo = [r for r in todos if r.av.alvo_id in ids]
        mostrar = ordenar([r for r in do_alvo if r.av.status in ORDEM_STATUS and not r.silencio])
        bloqueados = sum(r.av.status == "BLOQUEADO" for r in do_alvo)
        filtrados = sum(r.av.status in ORDEM_STATUS and bool(r.silencio) for r in do_alvo)

        idade = int((time.time() - quando) // 60)
        linhas = [f"<b>{html.escape(titulo)}</b>"]
        if mostrar:
            aprov = sum(r.av.status == "APROVADO" for r in mostrar)
            verif = len(mostrar) - aprov
            linhas.append(f"{len(mostrar)} encontrado(s): ✅ {aprov} aprovado(s) · ⚠️ {verif} a verificar")
        else:
            linhas.append("Nenhum lote disponível agora.")
        ocultos = []
        if bloqueados:
            ocultos.append(f"⛔ {bloqueados} bloqueado(s)")
        if filtrados:
            ocultos.append(f"🚫 {filtrados} fora dos seus filtros")
        if ocultos:
            linhas.append("Ocultos: " + " · ".join(ocultos))
        linhas.append(f"<i>Leilões: {', '.join(fontes)} · "
                      + ("atualizado agora" if idade == 0 else f"dados de {idade} min atrás") + "</i>")
        if len(mostrar) > MAX_LOTES_POR_BUSCA:
            linhas.append(f"Mostrando os {MAX_LOTES_POR_BUSCA} melhores.")
        self._editar(msg_id, "\n".join(linhas))

        for r in mostrar[:MAX_LOTES_POR_BUSCA]:
            alvo = self.alvos.get(r.av.alvo_id, {})
            erro = self.avisador.enviar_telegram(
                formatar(r.lote, r.av, "busca", r.valores, alvo.get("descricao", ""))
            )
            if erro:
                log.warning("lote não enviado: %s", erro)

        self._enviar("O que mais?", self._teclado_final(ids, origem))

    def _teclado_final(self, ids: list[str], origem: str) -> dict:
        linhas = [[("🔄 Buscar de novo", f"refaz:{origem}"), ("⬅️ Menu", "menu")]]
        # leiloeiras que bloqueiam robô: atalho pra busca manual já preenchida
        if len(ids) == 1:
            termo = self.alvos[ids[0]].get("busca", "")
            manuais = [
                (f"🔗 {m['nome']}", m["url"].replace("{termo}", quote(termo)))
                for m in self.cfg.get("links_manuais", [])
                if termo or "{termo}" not in m["url"]
            ]
            for i in range(0, len(manuais), 3):
                linhas.append(manuais[i:i + 3])
        return _teclado(linhas)

    # -------------------------------------------------------------- eventos

    def _clique(self, cb: dict) -> None:
        dado = cb.get("data", "")
        msg_id = cb["message"]["message_id"]
        self._api("answerCallbackQuery", callback_query_id=cb["id"])

        if dado == "menu":
            self._editar(msg_id, *self.tela_menu())
        elif dado == "alertas":
            self._gravar("alertas", "0" if self.alertas_ligados else "1")
            self._editar(msg_id, *self.tela_menu())
        elif dado.startswith("cat:"):
            self._editar(msg_id, *self.tela_categoria(dado[4:]))
        elif dado.startswith(("alvo:", "todos:", "refaz:")):
            forcar = dado.startswith("refaz:")
            origem = dado.split(":", 1)[1] if forcar else dado
            tipo, _, valor = origem.partition(":")
            if tipo == "alvo" and valor in self.alvos:
                ids, titulo = [valor], _rotulo(self.alvos[valor])
            elif tipo == "todos":
                ids = [i for i, a in self.alvos.items() if a.get("categoria", "carro") == valor]
                titulo = f"Todos — {CATEGORIAS.get(valor, valor)}"
            else:
                self._editar(msg_id, *self.tela_menu())
                return
            # a busca demora: manda numa mensagem nova e deixa o histórico limpo
            nova = self._enviar("🔎 Preparando a busca…")
            nova_id = nova.get("result", {}).get("message_id", msg_id)
            self.buscar(nova_id, ids, titulo, origem, forcar)

    def _tratar(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        cb = upd.get("callback_query") or {}
        chat = str((msg.get("chat") or (cb.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != self.chat_id:
            log.warning("ignorando chat não autorizado %s", chat)
            if chat and msg:
                self._api("sendMessage", chat_id=chat, text="Este robô é privado.")
            return
        if cb:
            self._clique(cb)
        elif msg:
            self._enviar(*self.tela_menu())  # qualquer mensagem abre o menu

    # ------------------------------------------------------------------ loop

    def rodar(self) -> None:
        self._api("setMyCommands", commands=[{"command": "menu", "description": "Abrir o menu"}])
        self._enviar(*self.tela_menu())
        offset = int(self._ler("offset", "0"))
        log.info("bot no ar — abra o Telegram (ctrl+c pra parar)")
        painel = int(os.environ.get("PAINEL_PID") or 0)

        while True:
            if painel and not processo_vivo(painel):
                log.info("painel fechado — desligando o bot")
                return
            resp = _telegram(self.token, "getUpdates", espera=35, offset=offset, timeout=25,
                             allowed_updates=["message", "callback_query"])
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                self._gravar("offset", offset)  # antes de tratar: um erro não vira loop
                try:
                    self._tratar(upd)
                except Exception:
                    log.exception("erro tratando mensagem")
            if not resp.get("ok"):
                time.sleep(5)

            if self.alertas_ligados and time.time() - self._ultima_passada > self.intervalo_alertas:
                self._ultima_passada = time.time()
                try:
                    self._passada()
                    self._cache = None  # a passada coletou dados novos
                except Exception:
                    log.exception("alerta automático falhou")
