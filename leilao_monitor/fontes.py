"""
Adaptadores de fonte. Cada leiloeira é só configuração — nenhum código novo.

Três tipos:
  - json    : a leiloeira tem um endpoint de busca que devolve JSON (o caso mais
              comum; é o que o próprio site chama por trás)
  - html    : a listagem vem renderizada em HTML, raspada por seletor CSS
  - arquivo : lê um .json/.csv local (teste, ou catálogo exportado)

Veja o README para descobrir o endpoint JSON de qualquer leiloeira em 2 minutos.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Lote:
    fonte: str
    id_lote: str
    titulo: str
    url: str
    descricao: str = ""
    preco: Optional[float] = None
    encerramento: str = ""
    local: str = ""
    uf: str = ""
    imagem: str = ""                      # URL da 1ª foto
    comissao_pct: Optional[float] = None  # % do leiloeiro sobre o lance
    despesas: Optional[float] = None      # pátio, documentação, logística (R$)
    bruto: dict = field(default_factory=dict)

    @property
    def texto_completo(self) -> str:
        return " | ".join(p for p in (self.titulo, self.descricao, self.local) if p)

    @property
    def chave(self) -> str:
        return f"{self.fonte}::{self.id_lote}"


# --------------------------------------------------------------------------


def _caminho(obj: Any, caminho: str, padrao: Any = None) -> Any:
    """Navega 'dados.resultados[0].titulo' dentro de dicts/listas."""
    if not caminho:
        return obj
    atual = obj
    for parte in caminho.replace("]", "").replace("[", ".").split("."):
        if parte == "":
            continue
        try:
            if isinstance(atual, list):
                atual = atual[int(parte)]
            else:
                atual = atual[parte]
        except (KeyError, IndexError, ValueError, TypeError):
            return padrao
    return atual if atual is not None else padrao


def _preco(valor: Any) -> Optional[float]:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    txt = str(valor)
    txt = "".join(c for c in txt if c.isdigit() or c in ",.")
    if not txt:
        return None
    # formato BR: 12.345,67
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    # formato BR sem centavos: 38.500 / 1.250.000 (ponto é milhar, não decimal)
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", txt):
        txt = txt.replace(".", "")
    try:
        return float(txt)
    except ValueError:
        return None


_UFS = (
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC "
    "SP SE TO"
).split()
_RE_UF = re.compile(r"(?:[/-]|\s)\s*(" + "|".join(_UFS) + r")\s*$")


def extrair_uf(local: str) -> str:
    """'Maringa - PR' / 'SANTA BARBARA D OESTE/SP' -> 'PR' / 'SP'."""
    m = _RE_UF.search((local or "").strip().upper())
    return m.group(1) if m else ""


# --------------------------------------------------------------------------


class FonteBase:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.nome: str = cfg["nome"]
        self.sessao = requests.Session()
        self.sessao.headers.update({"User-Agent": UA, **cfg.get("headers", {})})
        self.timeout = cfg.get("timeout", 25)
        self.pausa = cfg.get("pausa_segundos", 1.5)

    def coletar(self) -> Iterator[Lote]:
        """Coleta e completa uf/custos com os padrões da fonte."""
        custos = self.cfg.get("custos", {})
        for lote in self._coletar():
            lote.uf = lote.uf or extrair_uf(lote.local) or self.cfg.get("uf", "")
            if lote.comissao_pct is None:
                lote.comissao_pct = custos.get("comissao_pct")
            if lote.despesas is None:
                lote.despesas = custos.get("despesas")
            yield lote

    def _coletar(self) -> Iterator[Lote]:  # pragma: no cover
        raise NotImplementedError

    def _absoluta(self, url: str) -> str:
        """'/lote/1' e '//cdn/x.jpg' viram URL completa."""
        url = (url or "").strip()
        if url.startswith("//"):
            return "https:" + url
        base = self.cfg.get("url_base", "")
        if url.startswith("/") and base:
            return base.rstrip("/") + url
        return url

    def detalhar(self, lote: Lote) -> bool:
        """Abre a página do lote e completa descrição, pátio e custos.

        Só é chamado pra lote que bateu com um alvo (poucas requisições).
        Config:
            detalhe:
              inicio: "Descrição completa"     # recorta o texto útil da página
              fim: "Abertura p/ lances"        # (evita o edital genérico)
              local_regex: "Retirada:\\s*([^\\n]+)"
              comissao_regex: "(\\d+(?:,\\d+)?)\\s*%\\s*referente"
              despesas_regex: "R\\$\\s*([\\d.,]+)\\s*por conta do comprador"
        """
        det = self.cfg.get("detalhe")
        if not det or not lote.url:
            return False
        from bs4 import BeautifulSoup

        try:
            r = self.sessao.get(lote.url, timeout=self.timeout)
            r.raise_for_status()
        except Exception as e:
            log.warning("[%s] falha no detalhe %s: %s", self.nome, lote.url, e)
            return False
        time.sleep(self.pausa)

        sopa = BeautifulSoup(r.text, "html.parser")
        for lixo in sopa(["script", "style", "noscript"]):
            lixo.decompose()
        texto = sopa.get_text("\n", strip=True)
        if det.get("inicio") and det["inicio"] in texto:
            texto = texto[texto.index(det["inicio"]):]
        if det.get("fim") and det["fim"] in texto:
            texto = texto[: texto.index(det["fim"])]

        if det.get("local_regex"):
            m = re.search(det["local_regex"], texto)
            if m:
                lote.local = m.group(1).strip()
                lote.uf = extrair_uf(lote.local) or lote.uf
        if det.get("comissao_regex"):
            m = re.search(det["comissao_regex"], texto)
            if m:
                lote.comissao_pct = float(m.group(1).replace(",", "."))
        if det.get("despesas_regex"):
            valores = [_preco(v) for v in re.findall(det["despesas_regex"], texto)]
            if valores:
                lote.despesas = sum(v for v in valores if v)

        lote.descricao = f"{lote.descricao}\n{texto}".strip()
        return True


class FonteJSON(FonteBase):
    """Consome um endpoint de busca que devolve JSON.

    Config esperada:
        tipo: json
        nome: copart
        url: https://.../api/busca
        metodo: GET
        params: {q: "{termo}", page: "{pagina}"}
        termos: ["jetta highline", "civic lxr", "civic lxs"]
        paginas: 3
        caminho_lista: "data.results"
        campos:
          id_lote: "lotId"
          titulo: "title"
          descricao: "description"
          url: "detailUrl"
          preco: "currentBid"
          encerramento: "saleDate"
          local: "yardName"
        url_base: "https://www.exemplo.com.br"   # prefixo p/ urls relativas
    """

    def _coletar(self) -> Iterator[Lote]:
        termos = self.cfg.get("termos") or [""]
        paginas = self.cfg.get("paginas", 1)
        campos = self.cfg["campos"]

        for termo in termos:
            for pagina in range(1, paginas + 1):
                dados = self._requisitar(termo, pagina)
                if dados is None:
                    break
                itens = _caminho(dados, self.cfg.get("caminho_lista", ""), [])
                if not isinstance(itens, list) or not itens:
                    break
                for item in itens:
                    lote = self._montar(item, campos)
                    if lote:
                        yield lote
                time.sleep(self.pausa)

    def _requisitar(self, termo: str, pagina: int) -> Any:
        def fmt(v):
            if isinstance(v, str):
                return v.replace("{termo}", termo).replace("{pagina}", str(pagina))
            return v

        url = fmt(self.cfg["url"])
        params = {k: fmt(v) for k, v in self.cfg.get("params", {}).items()}
        corpo = self.cfg.get("json_body")
        metodo = self.cfg.get("metodo", "GET").upper()

        try:
            if metodo == "POST":
                corpo_fmt = json.loads(fmt(json.dumps(corpo or {})))
                r = self.sessao.post(
                    url, params=params, json=corpo_fmt, timeout=self.timeout
                )
            else:
                r = self.sessao.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("[%s] falha em '%s' p%s: %s", self.nome, termo, pagina, e)
            return None

    def _montar(self, item: dict, campos: dict) -> Optional[Lote]:
        id_lote = _caminho(item, campos.get("id_lote", ""))
        titulo = _caminho(item, campos.get("titulo", ""), "")
        if not id_lote and not titulo:
            return None
        url = self._absoluta(str(_caminho(item, campos.get("url", ""), "") or ""))
        return Lote(
            fonte=self.nome,
            id_lote=str(id_lote or titulo)[:120],
            titulo=str(titulo),
            descricao=str(_caminho(item, campos.get("descricao", ""), "") or ""),
            url=url,
            imagem=self._absoluta(str(_caminho(item, campos.get("imagem", ""), "") or "")),
            preco=_preco(_caminho(item, campos.get("preco", ""))),
            encerramento=str(_caminho(item, campos.get("encerramento", ""), "") or ""),
            local=str(_caminho(item, campos.get("local", ""), "") or ""),
            bruto=item if self.cfg.get("guardar_bruto") else {},
        )


class FonteHTML(FonteBase):
    """Raspa uma listagem HTML por seletores CSS.

        tipo: html
        nome: leiloeira-x
        url: "https://.../busca?q={termo}&p={pagina}"
        termos: ["jetta highline"]
        paginas: 2
        seletor_item: "div.card-lote"
        seletores:
          titulo: "h3.titulo"
          descricao: "p.detalhe"
          preco: "span.lance"
          url: {seletor: "a.link", atributo: "href"}
          imagem: {seletor: "img", atributo: "src"}
          id_lote: {seletor: "", atributo: "data-lote"}
    """

    def _coletar(self) -> Iterator[Lote]:
        from bs4 import BeautifulSoup  # import tardio: só quem usa html precisa

        termos = self.cfg.get("termos") or [""]
        paginas = self.cfg.get("paginas", 1)
        sels = self.cfg["seletores"]

        for termo in termos:
            for pagina in range(1, paginas + 1):
                url = (
                    self.cfg["url"]
                    .replace("{termo}", requests.utils.quote(termo))
                    .replace("{pagina}", str(pagina))
                )
                try:
                    r = self.sessao.get(url, timeout=self.timeout)
                    r.raise_for_status()
                except Exception as e:
                    log.warning("[%s] falha em %s: %s", self.nome, url, e)
                    break

                sopa = BeautifulSoup(r.text, "html.parser")
                itens = sopa.select(self.cfg["seletor_item"])
                if not itens:
                    break

                for item in itens:
                    campos = {k: self._extrair(item, s) for k, s in sels.items()}
                    titulo = campos.get("titulo") or ""
                    if not titulo:
                        continue
                    url_lote = self._absoluta(campos.get("url") or "")
                    yield Lote(
                        fonte=self.nome,
                        id_lote=(campos.get("id_lote") or url_lote or titulo)[:120],
                        titulo=titulo,
                        descricao=campos.get("descricao") or "",
                        url=url_lote,
                        imagem=self._absoluta(campos.get("imagem") or ""),
                        preco=_preco(campos.get("preco")),
                        encerramento=campos.get("encerramento") or "",
                        local=campos.get("local") or "",
                    )
                time.sleep(self.pausa)

    @staticmethod
    def _extrair(item, seletor) -> str:
        # lista: junta vários pedaços (ex.: título + ano que fica em outro div)
        if isinstance(seletor, list):
            partes = (FonteHTML._extrair(item, s) for s in seletor)
            return " ".join(p for p in partes if p)
        if isinstance(seletor, dict):
            alvo = item.select_one(seletor["seletor"]) if seletor.get("seletor") else item
            if alvo is None:
                return ""
            atributo = seletor.get("atributo")
            if atributo:
                # lista = primeiro que existir (ex.: [data-src, src] p/ lazy load)
                for a in atributo if isinstance(atributo, list) else [atributo]:
                    if alvo.get(a):
                        return str(alvo.get(a))
                return ""
            return alvo.get_text(" ", strip=True)
        alvo = item.select_one(seletor)
        return alvo.get_text(" ", strip=True) if alvo else ""


class FonteArquivo(FonteBase):
    """Lê .json ou .csv local. Útil pra testar as regras sem rede e pra processar
    catálogos exportados de leiloeiras que não têm API."""

    def _coletar(self) -> Iterator[Lote]:
        caminho = Path(self.cfg["caminho"])
        campos = self.cfg.get("campos", {})

        if caminho.suffix == ".json":
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            itens = _caminho(dados, self.cfg.get("caminho_lista", ""), dados)
        else:
            with caminho.open(encoding="utf-8-sig", newline="") as fh:
                itens = list(csv.DictReader(fh))

        for i, item in enumerate(itens):
            yield Lote(
                fonte=self.nome,
                id_lote=str(_caminho(item, campos.get("id_lote", ""), i)),
                titulo=str(_caminho(item, campos.get("titulo", "titulo"), "") or ""),
                descricao=str(_caminho(item, campos.get("descricao", "descricao"), "") or ""),
                url=str(_caminho(item, campos.get("url", "url"), "") or ""),
                imagem=str(_caminho(item, campos.get("imagem", "imagem"), "") or ""),
                preco=_preco(_caminho(item, campos.get("preco", "preco"))),
                encerramento=str(_caminho(item, campos.get("encerramento", ""), "") or ""),
                local=str(_caminho(item, campos.get("local", ""), "") or ""),
            )


TIPOS = {"json": FonteJSON, "html": FonteHTML, "arquivo": FonteArquivo}


def construir(cfg: dict) -> FonteBase:
    tipo = cfg.get("tipo", "json")
    if tipo not in TIPOS:
        raise ValueError(f"tipo de fonte desconhecido: {tipo}")
    return TIPOS[tipo](cfg)
