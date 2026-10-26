"""Painel local: uma página no navegador pra configurar e ligar/desligar o bot.

    python main.py --painel        (ou dois cliques no iniciar.bat)

Só escuta em 127.0.0.1 — ninguém na rede consegue ligar o seu bot. O bot roda
como processo separado (python main.py --bot); o PID fica em bot.pid pra o
painel reconhecer um bot que já estava rodando quando ele abriu.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .ambiente import carregar_env, ler_env, processo_vivo, salvar_env
from .estado import Avisador, _telegram, descobrir_chats

log = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent.parent
HTML = Path(__file__).resolve().parent / "painel.html"
ARQ_PID = RAIZ / "bot.pid"
ARQ_LOG = RAIZ / "bot.log"
NO_WINDOWS = os.name == "nt"


def _mascarar(token: str) -> str:
    return f"{token[:6]}…{token[-4:]}" if len(token) > 12 else ""


class ControleBot:
    """Liga/desliga o processo do bot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.desde: Optional[float] = None

    def pid(self) -> Optional[int]:
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        if ARQ_PID.exists():  # bot de uma sessão anterior do painel
            try:
                pid = int(ARQ_PID.read_text().strip())
                if processo_vivo(pid):
                    return pid
            except ValueError:
                pass
            ARQ_PID.unlink(missing_ok=True)
        return None

    def ligar(self) -> None:
        with self.lock:
            if self.pid():
                return
            carregar_env()
            # PAINEL_PID: o bot se desliga sozinho se o painel for fechado no X
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                   "PAINEL_PID": str(os.getpid())}
            extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if NO_WINDOWS else {}
            saida = open(ARQ_LOG, "a", encoding="utf-8")
            saida.write(f"\n===== bot ligado pelo painel em {time.strftime('%d/%m %H:%M:%S')} =====\n")
            saida.flush()
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(RAIZ / "main.py"), "--bot"],
                cwd=RAIZ, env=env, stdout=saida, stderr=subprocess.STDOUT, **extra,
            )
            self.desde = time.time()
            ARQ_PID.write_text(str(self.proc.pid))

    def desligar(self) -> None:
        with self.lock:
            pid = self.pid()
            if not pid:
                return
            if self.proc and self.proc.pid == pid:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            elif NO_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.kill(pid, signal.SIGTERM)
            ARQ_PID.unlink(missing_ok=True)
            self.proc, self.desde = None, None


def _ultimas_linhas(n: int = 60) -> list[str]:
    if not ARQ_LOG.exists():
        return []
    with open(ARQ_LOG, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 20000))
        return fh.read().decode("utf-8", "replace").splitlines()[-n:]


class Painel(BaseHTTPRequestHandler):
    controle: ControleBot  # preenchido em rodar()

    def log_message(self, *args):  # silencia o log de cada requisição
        pass

    def _json(self, dados: dict, codigo: int = 200) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self) -> dict:
        tam = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(tam) or b"{}") if tam else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            corpo = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif self.path == "/api/status":
            self._json(self.status())
        else:
            self._json({"erro": "não encontrado"}, 404)

    def do_POST(self):
        # outra aba/site não pode mandar comando: só aceita a própria origem
        origem = self.headers.get("Origin", "")
        if origem and not origem.startswith(("http://127.0.0.1", "http://localhost")):
            self._json({"erro": "origem não permitida"}, 403)
            return
        try:
            dados = self._corpo()
            acao = {
                "/api/ligar": self.ligar,
                "/api/desligar": self.desligar,
                "/api/config": self.config,
                "/api/descobrir-chat": self.descobrir_chat,
                "/api/testar": self.testar,
            }.get(self.path)
            if not acao:
                self._json({"erro": "não encontrado"}, 404)
                return
            self._json(acao(dados))
        except Exception as e:
            log.exception("erro no painel")
            self._json({"erro": str(e)}, 500)

    # --------------------------------------------------------------- ações

    def status(self) -> dict:
        env = ler_env()
        token = env.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
        chat = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
        pid = self.controle.pid()
        return {
            "rodando": bool(pid),
            "pid": pid,
            "desde": self.controle.desde,
            "token": _mascarar(token),
            "chat_id": chat,
            "bot_nome": env.get("BOT_NOME", ""),
            "ligar_ao_abrir": env.get("LIGAR_AO_ABRIR") == "1",
            "log": _ultimas_linhas(),
        }

    def ligar(self, _):
        st = self.status()
        if not (st["token"] and st["chat_id"]):
            return {"erro": "Configure o token e o chat_id primeiro."}
        self.controle.ligar()
        return {"ok": True}

    def desligar(self, _):
        self.controle.desligar()
        return {"ok": True}

    def config(self, dados: dict):
        novos = {}
        token = (dados.get("token") or "").strip()
        if token:
            resp = _telegram(token, "getMe")
            if not resp.get("ok"):
                return {"erro": f"O Telegram recusou o token ({resp.get('description')})."}
            novos["TELEGRAM_TOKEN"] = token
            novos["BOT_NOME"] = "@" + resp["result"].get("username", "")
        if dados.get("chat_id"):
            novos["TELEGRAM_CHAT_ID"] = str(dados["chat_id"]).strip()
        if "ligar_ao_abrir" in dados:
            novos["LIGAR_AO_ABRIR"] = "1" if dados["ligar_ao_abrir"] else "0"
        salvar_env(novos)
        carregar_env()
        return {"ok": True, "bot_nome": novos.get("BOT_NOME")}

    def descobrir_chat(self, _):
        carregar_env()
        token = os.environ.get("TELEGRAM_TOKEN")
        if not token:
            return {"erro": "Salve o token primeiro."}
        chats = descobrir_chats(token)
        if not chats:
            return {"erro": "Nenhuma mensagem encontrada. Abra o bot no Telegram, "
                            "toque em Iniciar, mande um 'oi' e tente de novo."}
        return {"chats": chats}

    def testar(self, _):
        carregar_env()
        erro = Avisador({"console": False}).enviar_telegram(
            "✅ Painel conectado. Os avisos do robô de leilão vão chegar aqui."
        )
        return {"erro": erro} if erro else {"ok": True}


def rodar(porta: int = 8765, abrir: bool = True) -> None:
    carregar_env()
    controle = ControleBot()
    Painel.controle = controle
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", porta), Painel)
    except OSError:
        # já tem um painel aberto nessa porta: só abre o navegador nele
        log.info("painel já está aberto em http://127.0.0.1:%s", porta)
        if abrir:
            webbrowser.open(f"http://127.0.0.1:{porta}")
        return

    if ler_env().get("LIGAR_AO_ABRIR") == "1" and not controle.pid():
        try:
            controle.ligar()
        except Exception:
            log.exception("não consegui ligar o bot automaticamente")

    url = f"http://127.0.0.1:{porta}"
    log.info("painel em %s — deixe esta janela aberta (ctrl+c pra fechar)", url)
    if abrir:
        threading.Timer(0.8, webbrowser.open, args=[url]).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
        # fechar o painel desliga o bot (no X da janela quem desliga é o
        # próprio bot, que vigia o PAINEL_PID)
        controle.desligar()
        log.info("painel fechado, bot desligado")
