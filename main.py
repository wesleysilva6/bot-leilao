#!/usr/bin/env python3
"""
Robô de monitoramento de leilões de veículos.

    python main.py --config config.yaml              # uma passada
    python main.py --config config.yaml --loop 1800  # a cada 30 min
    python main.py --config config.yaml --relatorio relatorio.csv
    python main.py --testar "JETTA 2.0 TSI HIGHLINE 2013/2014 PEQUENA MONTA"
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from leilao_monitor import fontes as mod_fontes
from leilao_monitor.estado import Avisador, Memoria, descobrir_chats, formatar
from leilao_monitor.fipe import Fipe, custo_total
from leilao_monitor.matcher import Alvo, avaliar, normalizar

# Console do Windows é cp1252 e quebra no emoji do aviso (✅ ⚠️ ⛔).
for _fluxo in (sys.stdout, sys.stderr):
    if hasattr(_fluxo, "reconfigure"):
        _fluxo.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("leilao")


def carregar_config(caminho: str) -> dict:
    return yaml.safe_load(Path(caminho).read_text(encoding="utf-8"))


def montar_alvos(cfg: dict) -> list[Alvo]:
    return [
        Alvo(
            id=a["id"],
            descricao=a.get("descricao", a["id"]),
            termos_obrigatorios=a["termos_obrigatorios"],
            termos_proibidos=a.get("termos_proibidos", []),
            ano_fab_min=a["ano_fab_min"],
            ano_fab_max=a["ano_fab_max"],
            preco_max=a.get("preco_max"),
        )
        for a in cfg["alvos"]
    ]


def comparar_fipe(lote, av, fipe: Optional[Fipe], cfg_fipe_alvo: Optional[dict]) -> dict:
    """FIPE do ano/modelo + custo total (lance + comissão + despesas)."""
    valores: dict = {"custo_total": custo_total(lote.preco, lote.comissao_pct, lote.despesas)}
    if fipe is None:
        return valores
    vf = fipe.consultar(cfg_fipe_alvo, lote.texto_completo, av.ano_mod or av.ano_fab)
    if vf and vf.valor:
        valores.update(fipe=vf.valor, fipe_ref=vf.referencia)
        if valores["custo_total"]:
            valores["pct_fipe"] = round(100 * valores["custo_total"] / vf.valor, 1)
    return valores


def motivo_silencio(lote, alvo: Alvo, valores: dict, filtros: dict) -> Optional[str]:
    """Por que um lote que bateu NÃO deve gerar aviso (None = avisa).
    Lote sem UF/pátio/preço identificado passa: na dúvida, avisa."""
    ufs = {u.upper() for u in filtros.get("ufs") or []}
    if ufs and lote.uf and lote.uf.upper() not in ufs:
        return f"UF {lote.uf} fora de {sorted(ufs)}"

    local = normalizar(lote.local)
    incluir = filtros.get("patios_incluir") or []
    if incluir and local and not any(normalizar(p) in local for p in incluir):
        return f"pátio '{lote.local}' fora de patios_incluir"
    for p in filtros.get("patios_excluir") or []:
        if local and normalizar(p) in local:
            return f"pátio '{lote.local}' em patios_excluir"

    if alvo.preco_max is not None and lote.preco is not None and lote.preco > alvo.preco_max:
        return f"lance {lote.preco:.0f} acima de preco_max {alvo.preco_max:.0f}"

    teto = filtros.get("max_pct_fipe")
    if teto is not None and valores.get("pct_fipe") is not None and valores["pct_fipe"] > teto:
        return f"custo = {valores['pct_fipe']:.0f}% da FIPE (teto {teto}%)"
    return None


def passada(cfg: dict, alvos: list[Alvo], memoria: Memoria, avisador: Avisador,
            fipe: Optional[Fipe] = None) -> dict:
    tolerancia = cfg.get("ano_tolerancia", 0)
    avisar_status = set(cfg.get("avisar_status", ["APROVADO", "VERIFICAR"]))
    filtros = cfg.get("filtros") or {}
    por_id = {a.id: a for a in alvos}
    fipe_alvo = {a["id"]: a.get("fipe") for a in cfg["alvos"]}
    contagem = {"coletados": 0, "casaram": 0, "avisos": 0, "silenciados": 0}

    for cfg_fonte in cfg.get("fontes", []):
        if not cfg_fonte.get("ativa", True):
            continue
        try:
            fonte = mod_fontes.construir(cfg_fonte)
        except Exception as e:
            log.error("fonte %s inválida: %s", cfg_fonte.get("nome"), e)
            continue

        log.info("coletando: %s", fonte.nome)
        try:
            for lote in fonte.coletar():
                contagem["coletados"] += 1
                # um lote com problema não pode derrubar o resto da fonte
                try:
                    av = avaliar(lote.texto_completo, alvos, ano_tolerancia=tolerancia)
                    if not av.casou:
                        continue
                    # bateu: abre a página do lote (descrição, pátio, custos)
                    # e reavalia com o texto completo
                    if av.status != "BLOQUEADO" and fonte.detalhar(lote):
                        av = avaliar(lote.texto_completo, alvos, ano_tolerancia=tolerancia)
                        if not av.casou:
                            continue
                    contagem["casaram"] += 1
                    valores = comparar_fipe(lote, av, fipe, fipe_alvo.get(av.alvo_id))
                    mudanca = memoria.registrar(lote, av, valores)
                    if not mudanca or av.status not in avisar_status:
                        continue
                    silencio = motivo_silencio(lote, por_id[av.alvo_id], valores, filtros)
                    if silencio:
                        log.info("sem aviso (%s): %s", silencio, lote.titulo)
                        contagem["silenciados"] += 1
                        continue
                    avisador.enviar(formatar(lote, av, mudanca, valores))
                    contagem["avisos"] += 1
                except Exception as e:
                    log.error("erro no lote %s de %s: %s", lote.id_lote, fonte.nome, e)
        except Exception as e:
            log.error("erro coletando %s: %s", fonte.nome, e)

    log.info(
        "fim da passada — %s lotes vistos, %s bateram com os alvos, %s avisos, "
        "%s silenciados pelos filtros",
        contagem["coletados"], contagem["casaram"], contagem["avisos"],
        contagem["silenciados"],
    )
    return contagem


def exportar(memoria: Memoria, destino: str) -> None:
    linhas = memoria.listar()
    campos = [
        "status", "alvo_id", "ano_fab", "titulo", "preco", "custo_total",
        "fipe", "pct_fipe", "fipe_ref", "uf", "local", "encerramento", "fonte",
        "url", "motivos", "atualizado_em",
    ]
    with open(destino, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for linha in linhas:
            w.writerow({c: linha[c] for c in campos})
    log.info("relatório gravado em %s (%s linhas)", destino, len(linhas))


def modo_teste(texto: str, cfg: dict) -> None:
    alvos = montar_alvos(cfg)
    av = avaliar(texto, alvos, ano_tolerancia=cfg.get("ano_tolerancia", 0))
    print(json.dumps(av.__dict__, ensure_ascii=False, indent=2))


def modo_telegram_chat(cfg: dict) -> int:
    token = Avisador(cfg.get("avisos", {})).telegram.get("token")
    if not token:
        print("defina TELEGRAM_TOKEN primeiro (token que o @BotFather te deu)")
        return 1
    try:
        chats = descobrir_chats(token)
    except RuntimeError as e:
        print(f"o Telegram recusou o token: {e}")
        return 1
    if not chats:
        print("nenhuma mensagem encontrada — abra o seu bot no Telegram, "
              "mande um 'oi' e rode de novo")
        return 1
    for c in chats:
        print(f"chat_id: {c['chat_id']}   ({c['nome']}, {c['tipo']})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Monitor de leilões de veículos")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--loop", type=int, metavar="SEGUNDOS",
                   help="roda continuamente neste intervalo")
    p.add_argument("--relatorio", metavar="ARQUIVO.csv",
                   help="exporta tudo que está na memória e sai")
    p.add_argument("--testar", metavar="TEXTO",
                   help="testa as regras contra um título de lote e sai")
    p.add_argument("--telegram-chat", action="store_true",
                   help="mostra o chat_id de quem mandou mensagem pro bot e sai")
    p.add_argument("--telegram-teste", action="store_true",
                   help="manda uma mensagem de teste pro Telegram e sai")
    args = p.parse_args()

    cfg = carregar_config(args.config)

    if args.testar:
        modo_teste(args.testar, cfg)
        return 0
    if args.telegram_chat:
        return modo_telegram_chat(cfg)
    if args.telegram_teste:
        erro = Avisador(cfg.get("avisos", {})).enviar_telegram(
            "✅ Robô de leilão conectado. Os avisos de lote vão chegar aqui."
        )
        print("mensagem enviada — confira o Telegram" if not erro else f"falhou: {erro}")
        return 1 if erro else 0

    memoria = Memoria(cfg.get("banco", "leiloes.db"))

    if args.relatorio:
        exportar(memoria, args.relatorio)
        memoria.fechar()
        return 0

    alvos = montar_alvos(cfg)
    avisador = Avisador(cfg.get("avisos", {}))
    cfg_fipe = cfg.get("fipe") or {}
    fipe = Fipe(memoria.con, cfg_fipe) if cfg_fipe.get("ativa", True) else None

    try:
        if args.loop:
            log.info("modo contínuo: a cada %ss (ctrl+c pra parar)", args.loop)
            while True:
                passada(cfg, alvos, memoria, avisador, fipe)
                time.sleep(args.loop)
        else:
            passada(cfg, alvos, memoria, avisador, fipe)
    except KeyboardInterrupt:
        log.info("encerrado pelo usuário")
    finally:
        memoria.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
