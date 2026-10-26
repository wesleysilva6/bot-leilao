@echo off
chcp 65001 >nul
title Robo de Leilao - painel
cd /d "%~dp0"

rem Acha o Python (python ou o launcher py)
set PY=
where python >nul 2>nul && set PY=python
if not defined PY (where py >nul 2>nul && set PY=py)
if not defined PY (
    echo.
    echo  Python nao encontrado.
    echo  Instale em https://www.python.org/downloads/  ^(marque "Add python.exe to PATH"^)
    echo  e rode este arquivo de novo.
    echo.
    pause
    exit /b 1
)

echo Conferindo dependencias...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Falha ao instalar as dependencias. Veja a mensagem acima.
    pause
    exit /b 1
)

echo.
echo  Abrindo o painel no navegador...
echo  Deixe esta janela aberta. Fechar esta janela desliga o bot.
echo.
%PY% main.py --painel
pause
