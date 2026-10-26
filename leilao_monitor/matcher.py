"""
Núcleo do robô: identifica o modelo/versão/ano no texto do lote e classifica
as restrições (monta, remarcação, origem).

Essa é a parte que NÃO quebra quando a leiloeira muda o site.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------
# Normalização
# --------------------------------------------------------------------------


def normalizar(texto: Optional[str]) -> str:
    """Minúsculas, sem acento, pontuação reduzida. Mantém '.' e '/' porque
    carregam significado ('2.0', '13/14')."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9./\s-]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# --------------------------------------------------------------------------
# Extração de ano
# --------------------------------------------------------------------------

# Os lookarounds evitam ler datas como ano: '04/11/2026' não vira fab 2004
# e '2026-11-04' não vira fab 2026.
_RE_ANO_PAR_LONGO = re.compile(
    r"(?<![\d/-])((?:19|20)\d{2})\s*[/-]\s*((?:19|20)?\d{2})\b(?!\s*[/-]\s*\d)"
)
_RE_ANO_PAR_CURTO = re.compile(r"(?<![\d/.,])(\d{2})\s*/\s*(\d{2})\b(?!\s*/\s*\d)")
_RE_ANO_SOLTO = re.compile(r"(?<![\d/-])((?:19|20)\d{2})\b(?!\s*[/-]\s*\d)")


def contem_termo(texto_norm: str, termo: str) -> bool:
    """Busca o termo respeitando limite de palavra.

    Sem isso, 'si' casaria dentro de 'sinistro' e '1.8' dentro de '21.800'.
    """
    t = normalizar(termo)
    if not t:
        return False
    return (
        re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", texto_norm)
        is not None
    )


def _expandir_2digitos(valor: str) -> int:
    n = int(valor)
    return 2000 + n if n <= 40 else 1900 + n


def extrair_anos(texto: str) -> tuple[Optional[int], Optional[int]]:
    """Devolve (ano_fabricacao, ano_modelo).

    Entende '2013/2014', '2013/14', '13/14' e ano solto '2013'.
    O primeiro número é sempre o de FABRICAÇÃO, que é o critério do usuário.
    """
    t = normalizar(texto)

    m = _RE_ANO_PAR_LONGO.search(t)
    if m:
        fab = int(m.group(1))
        bruto = m.group(2)
        mod = int(bruto) if len(bruto) == 4 else _expandir_2digitos(bruto)
        return fab, mod

    m = _RE_ANO_PAR_CURTO.search(t)
    if m:
        return _expandir_2digitos(m.group(1)), _expandir_2digitos(m.group(2))

    m = _RE_ANO_SOLTO.search(t)
    if m:
        ano = int(m.group(1))
        return ano, ano

    return None, None


# --------------------------------------------------------------------------
# Alvos (o que você quer comprar)
# --------------------------------------------------------------------------


@dataclass
class Alvo:
    """Um modelo-alvo.

    termos_obrigatorios: lista de GRUPOS. Todo grupo precisa bater (AND),
    e dentro do grupo basta uma alternativa (OR).
        [["civic"], ["lxr"], ["2.0", "20 16v"]]
    """

    id: str
    descricao: str
    termos_obrigatorios: list[list[str]]
    ano_fab_min: int
    ano_fab_max: int
    termos_proibidos: list[str] = field(default_factory=list)
    preco_max: Optional[float] = None  # acima disso grava, mas não avisa

    def casa_texto(self, texto_norm: str) -> tuple[bool, list[str]]:
        faltando: list[str] = []
        for grupo in self.termos_obrigatorios:
            if not any(contem_termo(texto_norm, alt) for alt in grupo):
                faltando.append(" ou ".join(grupo))
        if faltando:
            return False, [f"não encontrou: {', '.join(faltando)}"]

        for proibido in self.termos_proibidos:
            if contem_termo(texto_norm, proibido):
                return False, [f"versão errada: contém '{proibido}'"]

        return True, []


# --------------------------------------------------------------------------
# Restrições (o que bloqueia a compra)
# --------------------------------------------------------------------------

# Bloqueio duro — você disse que NÃO aceita
PADROES_BLOQUEIO: dict[str, list[str]] = {
    "monta_alta": [
        r"\bmedia monta\b",
        r"\bmonta media\b",
        r"\bgrande monta\b",
        r"\bmonta grande\b",
        r"\bsinistro de media\b",
        r"\bsinistro de grande\b",
        r"\birrecuperavel\b",
        r"\bbaixa definitiva\b",
        r"\bsucata\b",
        r"\bpara desmanche\b",
        r"\bvenda de pecas\b",
        r"\bsem direito a documenta",
        r"\bnao circula\b",
        r"\bdocumento baixado\b",
    ],
    "remarcacao": [
        # genéricos: pegam "chassi COM remarcacao", "chassi e motor remarcados"
        r"\bremarcad",
        r"\bremarcacao\b",
        r"\bregravad",
        r"\bregravacao\b",
        r"\badulterad",
        r"\badulteracao\b",
        r"\bchassi remarcad",
        r"\bmotor remarcad",
        r"\bremarcacao de chassi\b",
        r"\bremarcacao de motor\b",
        r"\bchassi regravad",
        r"\bmotor regravad",
        r"\bnumeracao remarcad",
        r"\bnumeracao regravad",
        r"\bchassi adulterad",
        r"\bmotor adulterad",
        r"\bnumeracao adulterad",
        r"\bchassi suprimid",
        r"\bnumeracao suprimid",
        r"\bchassi ilegivel\b",
        r"\bmotor ilegivel\b",
        r"\bchassi inexistente\b",
    ],
}

# Sinais positivos — você disse que ACEITA
PADROES_ACEITOS: dict[str, list[str]] = {
    "pequena_monta": [
        r"\bpequena monta\b",
        r"\bmonta pequena\b",
        r"\bsinistro de pequena\b",
    ],
    "retomado_financiamento": [
        r"\bretomad",
        r"\brecuperad[oa] de financiamento\b",
        r"\bbusca e apreensao\b",
        r"\balienacao fiduciaria\b",
        r"\bfinanceira\b",
        r"\bbanco credor\b",
        r"\brecuperacao de credito\b",
    ],
    "conservado": [
        r"\bapto a circular\b",
        r"\baptos a circular\b",
        r"\bconservad",
        r"\bsem sinistro\b",
        r"\bdocumentacao regular\b",
    ],
}

# Não bloqueia (você não pediu), mas nunca deixa aprovar: força VERIFICAR.
PADROES_ALERTA: dict[str, list[str]] = {
    "agua_fogo": [
        r"\benchente\b",
        r"\balagad",
        r"\balagamento\b",
        r"\bsubmers",
        r"\bincendi",
        r"\bqueimad",
    ],
    "roubo_furto": [
        r"\broubo\b",
        r"\bfurto\b",
        r"\broubad",
        r"\bfurtad",
    ],
}

# Declaração POSITIVA de chassi/motor. Só mencionar "chassi" não basta pra
# aprovar ("chassi: consultar laudo" não diz nada).
PADROES_CHASSI_OK: list[str] = [
    r"\b(?:chassi|motor|numeracao)(?: [a-z]+){0,4} (?:original|originais|integr[oa]s?|conferid[oa]s?|legive(?:l|is))\b",
    r"\b(?:chassi|motor|numeracao)(?: [a-z]+){0,4} sem (?:remarcacao|adulteracao|regravacao)\b",
    r"\bsem (?:chassi |motor )?(?:remarcad|remarcacao|adulterad|adulteracao)",
]

# Negações que anulam um match ("sem chassi remarcado", "não é de média monta").
# Só valem dentro da mesma frase/oração: "sem laudo, chassi remarcado" BLOQUEIA.
_RE_NEGACAO = re.compile(
    r"\b(?:sem|nao|nenhum|nenhuma|ausencia de|livre de|isento de|isenta de)\b"
)
_RE_FIM_ORACAO = re.compile(r"\.(?!\d)")


def _negado(texto_norm: str, inicio: int) -> bool:
    janela = texto_norm[max(0, inicio - 40) : inicio]
    janela = _RE_FIM_ORACAO.split(janela)[-1]
    return _RE_NEGACAO.search(janela) is not None


def _buscar(padroes: list[str], texto_norm: str) -> list[str]:
    """Todas as ocorrências não negadas. Olha cada ocorrência, não só a
    primeira: em 'chassi sem remarcacao, motor remarcado' a 2ª é que importa."""
    achados = []
    for p in padroes:
        for m in re.finditer(p, texto_norm):
            if not _negado(texto_norm, m.start()):
                achados.append(m.group(0).strip())
                break
    return achados


def _texto_restricoes(texto: str) -> str:
    """Normaliza transformando vírgula, ';', '|' e quebra de linha em fim de
    oração, pra negação não vazar de um trecho pro outro."""
    return normalizar(re.sub(r"[,;|!?\n]", ". ", texto or ""))


@dataclass
class Veredito:
    status: str  # "BLOQUEADO" | "APROVADO" | "VERIFICAR"
    bloqueios: list[str] = field(default_factory=list)
    sinais: list[str] = field(default_factory=list)
    observacoes: list[str] = field(default_factory=list)


def classificar_restricoes(texto: str) -> Veredito:
    """Aplica as regras do usuário:
      - aceita: retomado de financiamento, pequena monta
      - bloqueia: média/grande monta, sucata, motor ou chassi remarcado
      - sem informação => VERIFICAR (não aprova no escuro)
    """
    t = _texto_restricoes(texto)

    bloqueios: list[str] = []
    for categoria, padroes in PADROES_BLOQUEIO.items():
        for achado in _buscar(padroes, t):
            bloqueios.append(f"{categoria}: '{achado}'")

    sinais: list[str] = []
    for categoria, padroes in PADROES_ACEITOS.items():
        for achado in _buscar(padroes, t):
            sinais.append(f"{categoria}: '{achado}'")

    if bloqueios:
        return Veredito(status="BLOQUEADO", bloqueios=bloqueios, sinais=sinais)

    obs: list[str] = []
    for categoria, padroes in PADROES_ALERTA.items():
        for achado in _buscar(padroes, t):
            obs.append(f"ALERTA {categoria}: '{achado}' — ler o laudo antes de qualquer lance")
    if not any(re.search(p, t) for p in PADROES_CHASSI_OK):
        obs.append("lote não declara chassi/motor originais — confirmar no laudo e no edital")
    if not sinais:
        obs.append("lote não declara origem/classificação — confirmar no edital")

    if sinais and not obs:
        return Veredito(status="APROVADO", sinais=sinais)

    return Veredito(status="VERIFICAR", sinais=sinais, observacoes=obs)


# --------------------------------------------------------------------------
# Avaliação completa de um lote
# --------------------------------------------------------------------------


@dataclass
class Avaliacao:
    alvo_id: Optional[str]
    casou: bool
    status: str
    ano_fab: Optional[int]
    ano_mod: Optional[int]
    motivos: list[str] = field(default_factory=list)
    sinais: list[str] = field(default_factory=list)
    observacoes: list[str] = field(default_factory=list)


def avaliar(texto: str, alvos: list[Alvo], ano_tolerancia: int = 0) -> Avaliacao:
    """Testa o texto do lote contra todos os alvos e devolve a melhor avaliação.

    ano_tolerancia: margem em anos. 0 = estrito (recomendado). 1 = inclui o
    ano de fabricação imediatamente antes/depois, útil porque muita leiloeira
    publica o ano/modelo no lugar do ano de fabricação.
    """
    t = normalizar(texto)
    ano_fab, ano_mod = extrair_anos(texto)

    quase: list[str] = []

    for alvo in alvos:
        ok, motivos = alvo.casa_texto(t)
        if not ok:
            quase.extend(f"[{alvo.id}] {m}" for m in motivos)
            continue

        if ano_fab is None:
            return Avaliacao(
                alvo_id=alvo.id,
                casou=True,
                status="VERIFICAR",
                ano_fab=None,
                ano_mod=None,
                motivos=["ano de fabricação não identificado no anúncio"],
            )

        dentro_estrito = alvo.ano_fab_min <= ano_fab <= alvo.ano_fab_max
        dentro_tolerado = (
            alvo.ano_fab_min - ano_tolerancia
            <= ano_fab
            <= alvo.ano_fab_max + ano_tolerancia
        )

        if not dentro_tolerado:
            quase.append(
                f"[{alvo.id}] ano fab {ano_fab} fora da faixa "
                f"{alvo.ano_fab_min}-{alvo.ano_fab_max}"
            )
            continue

        v = classificar_restricoes(texto)
        status = v.status
        obs = list(v.observacoes)

        # Entrou só por causa da tolerância: nunca aprova sozinho.
        # Serve pro caso da leiloeira publicar o ano/modelo no campo de ano.
        if not dentro_estrito and status == "APROVADO":
            status = "VERIFICAR"
        if not dentro_estrito:
            obs.insert(
                0,
                f"ano fab {ano_fab} está FORA da faixa {alvo.ano_fab_min}-"
                f"{alvo.ano_fab_max} (entrou pela tolerância) — conferir CRLV",
            )

        return Avaliacao(
            alvo_id=alvo.id,
            casou=True,
            status=status,
            ano_fab=ano_fab,
            ano_mod=ano_mod,
            motivos=v.bloqueios,
            sinais=v.sinais,
            observacoes=obs,
        )

    return Avaliacao(
        alvo_id=None,
        casou=False,
        status="DESCARTADO",
        ano_fab=ano_fab,
        ano_mod=ano_mod,
        motivos=quase[:4],
    )
