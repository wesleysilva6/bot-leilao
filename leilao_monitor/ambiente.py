"""Credenciais locais em `.env` (fica fora do git — veja .gitignore).

Numa máquina nova não há variável de ambiente configurada: o painel grava o
token e o chat_id aqui e o robô lê daqui. O que está no .env vale mais que a
variável de ambiente, pra que trocar o token no painel tenha efeito.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent.parent / ".env"


def ler_env(caminho: Path = ARQUIVO) -> dict[str, str]:
    if not caminho.exists():
        return {}
    valores = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valores[chave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def carregar_env(caminho: Path = ARQUIVO) -> None:
    """Joga o .env no os.environ (sobrescrevendo)."""
    os.environ.update(ler_env(caminho))


def processo_vivo(pid: int) -> bool:
    if os.name == "nt":
        saida = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return f'"{pid}"' in saida
    try:
        os.kill(pid, 0)  # sinal 0 só testa se existe (POSIX)
        return True
    except OSError:
        return False


def salvar_env(novos: dict[str, str], caminho: Path = ARQUIVO) -> None:
    """Atualiza só as chaves passadas; mantém o resto do arquivo."""
    valores = ler_env(caminho)
    valores.update({k: v for k, v in novos.items() if v is not None})
    linhas = ["# credenciais locais do robô — NÃO commitar (está no .gitignore)"]
    linhas += [f"{k}={v}" for k, v in valores.items()]
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
