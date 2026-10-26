#!/usr/bin/env sh
# Linux/Mac: ./iniciar.sh  — instala as dependências e abre o painel.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
    echo "Python não encontrado. Instale o Python 3 e rode de novo."
    exit 1
fi
"$PY" -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1
echo "Abrindo o painel no navegador. Deixe este terminal aberto (ctrl+c fecha e desliga o bot)."
exec "$PY" main.py --painel
