"""Testes das regras. Roda sem rede: python testes_matcher.py"""

import yaml
from leilao_monitor.bot import Bot, ordenar
from leilao_monitor.estado import Avisador, Memoria, formatar
from leilao_monitor.fipe import Fipe, custo_total
from leilao_monitor.fontes import Lote, _preco, extrair_uf
from main import Resultado, motivo_silencio
from leilao_monitor.matcher import Alvo, Avaliacao, avaliar, classificar_restricoes, extrair_anos

ALVOS = [
    Alvo(
        id=a["id"],
        descricao=a.get("descricao", ""),
        termos_obrigatorios=a["termos_obrigatorios"],
        termos_proibidos=a.get("termos_proibidos", []),
        ano_fab_min=a["ano_fab_min"],
        ano_fab_max=a["ano_fab_max"],
    )
    for a in yaml.safe_load(open("config.yaml", encoding="utf-8"))["alvos"]
]

CASOS = [
    # (texto, alvo_esperado, status_esperado)
    ("VW/JETTA HIGHLINE 2.0 TSI 16V 2013/2014. Retomado de financiamento, "
     "documentacao regular, chassi original.", "jetta-highline-tsi", "APROVADO"),
    ("HONDA CIVIC LXR 2.0 16V 2015/2015. Sinistro de pequena monta, "
     "numeracao de chassi original e motor original.", "civic-lxr-20", "APROVADO"),
    ("HONDA CIVIC LXS 1.8 16V 2012/2013. Busca e apreensao. Motor e chassi "
     "sem remarcacao.", "civic-lxs-18", "APROVADO"),

    # bloqueios
    ("HONDA CIVIC LXR 2.0 2014/2015. Media monta.", "civic-lxr-20", "BLOQUEADO"),
    ("JETTA HIGHLINE 2.0 TSI 2013/2014. Chassi remarcado.",
     "jetta-highline-tsi", "BLOQUEADO"),
    ("CIVIC LXS 1.8 2014/2015. Motor regravado conforme laudo.",
     "civic-lxs-18", "BLOQUEADO"),
    ("JETTA HIGHLINE 2.0 TSI 2012/2013. Sucata, venda de pecas.",
     "jetta-highline-tsi", "BLOQUEADO"),

    # sem informacao -> nao aprova no escuro
    ("JETTA HIGHLINE 2.0 TSI 2014/2015. Retirada no patio.",
     "jetta-highline-tsi", "VERIFICAR"),

    # negacao nao pode virar bloqueio
    ("CIVIC LXR 2.0 2015/2016. Pequena monta, sem chassi remarcado, "
     "numeracao de motor conferida.", "civic-lxr-20", "APROVADO"),

    # versao errada
    ("VW JETTA COMFORTLINE 2.0 FLEX 2013/2014. Retomado.", None, "DESCARTADO"),
    ("VW JETTA HIGHLINE 1.4 TSI 2015/2016. Retomado.", None, "DESCARTADO"),
    ("HONDA CIVIC EXR 2.0 2015/2016. Pequena monta.", None, "DESCARTADO"),
    ("HONDA CIVIC LXS 1.8 2012. Retomado.", "civic-lxs-18", "VERIFICAR"),

    # ano fora da faixa, alem da tolerancia
    ("HONDA CIVIC LXR 2.0 2019/2020. Retomado.", None, "DESCARTADO"),
    ("VW JETTA HIGHLINE 2.0 TSI 2008/2009. Retomado.", None, "DESCARTADO"),

    # 'si' nao pode casar dentro de 'sinistro'
    ("HONDA CIVIC LXR 2.0 16V 2016/2016. Sem sinistro, retomado de "
     "financiamento, chassi original.", "civic-lxr-20", "APROVADO"),

    # negacao nao pode vazar de uma oracao pra outra
    ("JETTA HIGHLINE 2.0 TSI 2013/2014. Sem laudo, chassi remarcado.",
     "jetta-highline-tsi", "BLOQUEADO"),
    ("CIVIC LXR 2.0 2015/2016. Veiculo sem chave, media monta.",
     "civic-lxr-20", "BLOQUEADO"),
    # toda ocorrencia conta, nao so a primeira
    ("CIVIC LXR 2.0 2015/2016. Retomado. Chassi sem remarcacao, motor remarcado.",
     "civic-lxr-20", "BLOQUEADO"),
    ("CIVIC LXR 2.0 2015/2016. Retomado. Chassi com remarcacao.",
     "civic-lxr-20", "BLOQUEADO"),
    # 'nao' dentro de 'padrao' nao e negacao
    ("CIVIC LXR 2015/2016. Retomado. Padrao media monta.", "civic-lxr-20", "BLOQUEADO"),
    # citar chassi sem dizer que e original nao aprova
    ("CIVIC LXR 2.0 2015/2016. Retomado de financiamento. Chassi: consultar laudo.",
     "civic-lxr-20", "VERIFICAR"),
    # enchente/roubo nunca aprova
    ("CIVIC LXS 1.8 2014/2015. Retomado. Chassi original. Atingido por enchente.",
     "civic-lxs-18", "VERIFICAR"),
    ("JETTA HIGHLINE 2.0 TSI 2013/2014. Recuperado de roubo, retomado, chassi original.",
     "jetta-highline-tsi", "VERIFICAR"),
    # Detran costuma omitir a motorizacao
    ("I/HONDA CIVIC LXR 2014/2015 FLEX AUT. Retomado, chassi original.",
     "civic-lxr-20", "APROVADO"),
    ("HONDA/CIVIC LXS 2013/2013. Busca e apreensao, motor e chassi originais.",
     "civic-lxs-18", "APROVADO"),
    # Jetta TSI sem a palavra Highline
    ("VW/JETTA 2.0 TSI, 13/14, GASOLINA. FINANCEIRA. Chassi original.",
     "jetta-highline-tsi", "APROVADO"),
    ("VW/JETTA TSI 2012/2012. Retomado.", "jetta-highline-tsi", "VERIFICAR"),
    ("VW/JETTA 250 TSI 2015/2016. Retomado.", None, "DESCARTADO"),
    ("VW/JETTA 2.0 FLEX 2013/2014. Retomado.", None, "DESCARTADO"),
    # formato real do Freitas
    ("HONDA/CIVIC LXR, 14/14, PLACA: O__-___3, GASOL/ALC, BRANCA | FINANCEIRA",
     "civic-lxr-20", "VERIFICAR"),
]


def main() -> int:
    falhas = 0
    for texto, alvo_esp, status_esp in CASOS:
        av = avaliar(texto, ALVOS, ano_tolerancia=1)
        ok = av.alvo_id == alvo_esp and av.status == status_esp
        if not ok:
            falhas += 1
            print(f"FALHOU: {texto[:60]}...")
            print(f"   esperado: {alvo_esp} / {status_esp}")
            print(f"   obtido:   {av.alvo_id} / {av.status}  {av.motivos}")

    # extracao de ano
    for texto, esperado in [
        ("JETTA 2013/2014", 2013),
        ("CIVIC 13/14", 2013),
        ("CIVIC LXS 1.8 16V 2015", 2015),
        ("JETTA 2.0 TSI 11/12", 2011),
        ("CIVIC 2.0 16V ano 2016/2017", 2016),
        ("CIVIC LXR encerra 04/11/2026", None),   # data nao e ano do carro
        ("CIVIC LXR leilao 2026-11-04", None),
    ]:
        fab, _ = extrair_anos(texto)
        if fab != esperado:
            falhas += 1
            print(f"FALHOU ano: {texto!r} -> {fab}, esperado {esperado}")

    # 1.8 nao pode casar dentro de um preco
    v = classificar_restricoes("lance R$ 21.800,00 pequena monta chassi original")
    if v.status != "APROVADO":
        falhas += 1
        print(f"FALHOU restricao: {v}")

    # preco BR sem centavos: ponto e milhar
    for bruto, esperado in [("R$ 38.500", 38500.0), ("R$ 38.500,00", 38500.0),
                            ("1.250.000", 1250000.0), (38500, 38500.0)]:
        if _preco(bruto) != esperado:
            falhas += 1
            print(f"FALHOU preco: {bruto!r} -> {_preco(bruto)}, esperado {esperado}")

    # UF do pátio
    for local, esperado in [("Maringa - PR", "PR"), ("SANTA BARBARA D OESTE/SP", "SP"),
                            ("PH Batidos - PÁTIO AV DAS TORRES", "")]:
        if extrair_uf(local) != esperado:
            falhas += 1
            print(f"FALHOU uf: {local!r} -> {extrair_uf(local)!r}, esperado {esperado!r}")

    # filtros de aviso
    civic = next(a for a in ALVOS if a.id == "civic-lxr-20")
    filtros = {"ufs": ["PR"], "patios_excluir": ["foz"], "max_pct_fipe": 70}
    for lote, valores, silencia in [
        (Lote("t", "1", "x", "", local="Curitiba - PR", uf="PR"), {"pct_fipe": 50}, False),
        (Lote("t", "2", "x", "", local="Barueri/SP", uf="SP"), {}, True),
        (Lote("t", "3", "x", "", local="Foz do Iguacu - PR", uf="PR"), {}, True),
        (Lote("t", "4", "x", "", uf="PR"), {"pct_fipe": 82}, True),
        (Lote("t", "5", "x", ""), {}, False),   # sem UF: na duvida avisa
    ]:
        if (motivo_silencio(lote, civic, valores, filtros) is not None) != silencia:
            falhas += 1
            print(f"FALHOU filtro: lote {lote.id_lote}")

    # modelo FIPE: LXS manual x automatico
    cfg_lxs = {"marca": 25, "modelos": [{"codigo": 4011, "se": ["mec", "manual"]}, {"codigo": 4010}]}
    for texto, esperado in [("CIVIC LXS 1.8 FLEX MEC", 4011), ("CIVIC LXS AUT", 4010)]:
        if Fipe.escolher_modelo(cfg_lxs, texto) != esperado:
            falhas += 1
            print(f"FALHOU fipe modelo: {texto!r}")
    if round(custo_total(30000, 5, 2850)) != 34350:
        falhas += 1
        print("FALHOU custo_total")

    # mensagem: HTML escapado, versao texto sem tags, cabe na legenda da foto
    lote = Lote("freitas", "1", "CIVIC LXR <AUT> & cia", "https://x.com/l?a=1&b=2",
                local="AV. JUSCELINO K. DE OLIVEIRA, 1350 MARGINAL - SANTA BARBARA D OESTE/SP",
                preco=30000, comissao_pct=5, despesas=2850, imagem="https://x.com/f.jpg")
    av = avaliar("HONDA CIVIC LXR 2014/2015. Retomado.", ALVOS, ano_tolerancia=1)
    msg = formatar(lote, av, "novo", {"custo_total": 34350, "fipe": 70249, "pct_fipe": 48.9},
                   civic.descricao)
    for ok, nome in [
        ("&lt;AUT&gt; &amp; cia" in msg.html, "escape html"),
        ("<b>" not in msg.texto and "CIVIC LXR <AUT> & cia" in msg.texto, "texto sem tags"),
        ("https://x.com/l?a=1&b=2" in msg.texto, "link no texto"),
        ("SANTA BARBARA D OESTE/SP" in msg.texto and "JUSCELINO" not in msg.texto, "local curto"),
        ("49% da FIPE" in msg.texto and "R$ 34.350" in msg.texto, "valores"),
        (len(msg.texto) <= 1024 and msg.imagem, "cabe na legenda"),
    ]:
        if not ok:
            falhas += 1
            print(f"FALHOU mensagem: {nome}\n{msg.texto}")

    # bot: telas, ordenacao e chat nao autorizado (Telegram simulado)
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    memoria = Memoria(":memory:")
    bot = Bot(cfg, Avisador({"console": False, "telegram": {"token": "x", "chat_id": "123"}}),
              memoria, analisar=lambda: ([], {}), passada=lambda: {})
    chamadas = []
    bot._api = lambda metodo, **p: chamadas.append((metodo, p)) or {"ok": True, "result": {}}

    _, teclado = bot.tela_categoria("carro")
    botoes = [b["callback_data"] for linha in teclado["inline_keyboard"] for b in linha]
    texto_moto, _ = bot.tela_categoria("moto")
    bot._tratar({"message": {"chat": {"id": 999}, "text": "oi"}})   # intruso
    bot._tratar({"message": {"chat": {"id": 123}, "text": "oi"}})   # dono
    r = lambda st, pct, preco: Resultado(Lote("f", st + str(pct), "t", "", preco=preco),
                                         Avaliacao("a", True, st, 2014, 2014), {"pct_fipe": pct}, None, None)
    ordem = [x.lote.id_lote for x in ordenar([r("VERIFICAR", 40, 1), r("APROVADO", 70, 1),
                                               r("APROVADO", None, 1), r("APROVADO", 50, 1)])]
    for ok, nome in [
        (botoes == ["alvo:jetta-highline-tsi", "alvo:civic-lxr-20", "alvo:civic-lxs-18",
                    "todos:carro", "menu"], "menu de carros"),
        ("Nenhum modelo" in texto_moto, "moto vazia"),
        ([(p["chat_id"], "reply_markup" in p) for _, p in chamadas] == [("999", False), ("123", True)],
         "intruso nao recebe menu"),
        (ordem == ["APROVADO50", "APROVADO70", "APROVADONone", "VERIFICAR40"], "ordenacao"),
    ]:
        if not ok:
            falhas += 1
            print(f"FALHOU bot: {nome}")

    total = len(CASOS) + 7 + 1 + 4 + 3 + 5 + 2 + 1 + 6 + 4
    print(f"\n{total - falhas}/{total} testes passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
