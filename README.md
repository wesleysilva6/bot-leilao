# Robô de monitoramento de leilões — Jetta Highline TSI / Civic LXR / Civic LXS

Monitora listagens de leilão, reconhece os seus três modelos-alvo no meio do
título bagunçado dos lotes, aplica as suas regras de restrição e te avisa só do
que interessa. Guarda tudo em SQLite pra não te avisar duas vezes.

## Regras configuradas

**Alvos**

| id | modelo | ano de fabricação |
|---|---|---|
| `jetta-highline-tsi` | VW Jetta Highline 2.0 16V TSI Turbo (também acha "JETTA 2.0 TSI" sem "Highline") | 2011–2015 |
| `civic-lxr-20` | Honda Civic LXR 2.0 16V i-VTEC | 2013–2016 |
| `civic-lxs-18` | Honda Civic LXS 1.8 16V i-VTEC | 2012–2016 |

**Restrições**

| situação | resultado |
|---|---|
| retomado de financiamento, busca e apreensão, alienação fiduciária | ✅ aceita |
| pequena monta | ✅ aceita |
| média monta, grande monta, sucata, irrecuperável, baixa definitiva | ⛔ bloqueia |
| motor ou chassi remarcado / regravado / adulterado / ilegível | ⛔ bloqueia |
| enchente, alagamento, incêndio, roubo/furto | ⚠️ VERIFICAR com ALERTA (nunca aprova) |
| lote que não declara chassi/motor originais | ⚠️ VERIFICAR (citar "chassi" não basta) |
| lote que não diz nada sobre isso | ⚠️ VERIFICAR (nunca aprova no escuro) |

Os três status são:

- **APROVADO** — bateu modelo, ano e as declarações do lote são compatíveis com
  o que você aceita.
- **VERIFICAR** — bateu modelo e ano, mas o anúncio é omisso. Você precisa abrir
  o laudo/edital. A maioria dos lotes reais cai aqui.
- **BLOQUEADO** — fica gravado no banco mas não te incomoda.

O robô trata negação: *"sem chassi remarcado"* não vira bloqueio. A negação só
vale dentro da mesma oração: em *"sem laudo, chassi remarcado"* o lote é
bloqueado.

## Fontes configuradas

| fonte | tipo | o que o robô lê |
|---|---|---|
| **Freitas Leiloeiro** (SP) | leilão | lotes de Jetta/Civic; abre a página do lote e lê pátio, comissão e despesas |
| **PH Batidos** (Curitiba/PR) | venda direta, preço fixo | estoque Honda/VW; o card já traz a monta |
| **Copart** | — | **não coletável**: anti-robô (Incapsula). No bot vira botão de busca manual |
| **MGL** | — | **não coletável**: anti-robô (Cloudflare). No bot vira botão de busca manual |
| **Milan** | — | **não coletável**: anti-robô (Cloudflare Turnstile). No bot vira botão |

## FIPE e custo total

Para cada lote que bate com um alvo, o robô consulta a FIPE do ano/modelo (API
pública parallelum, com cache de 15 dias no SQLite) e calcula:

```
custo total = lance + comissão do leiloeiro + despesas (pátio, doc., logística)
```

O aviso mostra o custo como % da FIPE. Com `filtros.max_pct_fipe: 75` você só
é avisado de lote cujo custo total fica abaixo de 75% da FIPE.

A FIPE é de carro **inteiro e sem histórico**. Batido ou de leilão vale menos
na revenda (de 20% a 30% a menos, conforme o caso), então use a % como triagem,
não como avaliação.

## Filtros de aviso

```yaml
filtros:
  ufs: [PR, SC, SP]          # só esses estados
  patios_incluir: []         # trechos do nome do pátio/cidade
  patios_excluir: ["foz"]
  max_pct_fipe: 75
```

Lote filtrado continua no banco (sai no `--relatorio`), só não gera aviso. Lote
sem UF, pátio ou lance identificado passa pelo filtro: na dúvida, o robô avisa.

## Avisos no Telegram

1. No Telegram, abra **@BotFather** → `/newbot` → dê um nome e um usuário
   terminado em `bot`. Ele responde com o **token**.
2. Grave o token (PowerShell, uma vez só; depois feche e abra o terminal):
   `setx TELEGRAM_TOKEN "seu-token"`
3. Abra o seu bot no Telegram, toque em **Iniciar** e mande um "oi".
4. `python main.py --telegram-chat` → mostra o seu `chat_id`.
5. `setx TELEGRAM_CHAT_ID "o-numero"` (feche e abra o terminal de novo).
6. `python main.py --telegram-teste` → deve chegar uma mensagem.

Nunca coloque o token no `config.yaml`: o repositório é público.

### Bot com menu (`python main.py --bot`)

Mande qualquer mensagem (ou `/menu`) pro bot:

```
🤖 Robô de Leilão
[🚗 Carros] [🏍️ Motos]
[🔕 Desligar alertas]
   └─ 🚗 Carros → [VW Jetta Highline 2.0 TSI] [Civic LXR 2.0] [Civic LXS 1.8] [🔎 Todos]
         └─ busca em todas as leiloeiras → resumo + cada lote com foto
            (aprovados primeiro, depois menor % da FIPE)
            [🔄 Buscar de novo] [⬅️ Menu]  [🔗 Copart] [🔗 MGL] [🔗 Milan]
```

- Só responde ao seu `TELEGRAM_CHAT_ID`; qualquer outra pessoa recebe
  "Este robô é privado".
- Alertas automáticos: liga/desliga no menu. Com eles ligados, a cada
  `bot.alertas_minutos` o robô procura e avisa **só lote novo** (nada de
  repetição quando o lance muda — veja `avisar_mudancas`).
- Uma coleta serve pra todos os modelos e é reaproveitada por
  `bot.cache_minutos`; "Buscar de novo" força uma coleta nova.
- Copart, MGL e Milan bloqueiam robô, então viram botões que abrem a busca já
  preenchida no site.
- Pra motos (ou outra categoria), adicione um alvo com `categoria: moto`,
  `rotulo` e `busca` no `config.yaml`; o botão aparece sozinho.
- Rode **um** bot por vez: dois processos com o mesmo token brigam pelas
  mensagens.

Cada aviso chega como a **1ª foto do veículo** com a mensagem na legenda:
status, modelo e ano, cidade do pátio, lance, custo total, FIPE, pontos a favor,
o que conferir e o link do lote. Para pegar a foto de uma fonte nova, configure
`seletores.imagem` (HTML) ou `campos.imagem` (JSON).

## Instalação

```bash
pip install -r requirements.txt
```

## Uso

```bash
# uma passada nas leiloeiras ativas
python main.py --config config.yaml

# testa um título solto
python main.py --config config.yaml --testar "CIVIC LXR 2.0 16V 2015/2016 MEDIA MONTA"

# roda a bateria de testes das regras
python testes_matcher.py

# modo contínuo, a cada 30 minutos
python main.py --config config.yaml --loop 1800

# exporta tudo que está no banco pra planilha
python main.py --config config.yaml --relatorio lotes.csv
```

## Achando o endpoint de cada leiloeira (~2 min)

Nenhuma leiloeira grande publica API documentada, mas quase todas têm um
endpoint JSON interno que o próprio site chama. É ele que você quer — é muito
mais estável do que raspar HTML.

1. Abra a busca de veículos da leiloeira no Chrome.
2. `F12` → aba **Network** → filtro **Fetch/XHR**.
3. Digite "civic" na busca do site e dê enter.
4. Vai aparecer uma requisição com a resposta cheia de lotes. Clique nela.
5. Copie da aba **Headers**: a URL e o método (GET/POST).
6. Na aba **Preview**, veja onde fica a lista (ex.: `data.items`) e os nomes dos
   campos (ex.: `lanceAtual`, `descricaoLote`).
7. Preencha um bloco em `fontes:` no `config.yaml` com `url`, `params`,
   `caminho_lista` e `campos`. Nenhuma linha de código.

Se a leiloeira não tiver endpoint JSON, use `tipo: html` com seletor CSS. Se não
tiver nem isso, baixe o catálogo/edital e aponte uma fonte `tipo: arquivo` pro
CSV ou JSON.

Se a página do lote tiver informação que o card não mostra (pátio, custos,
laudo), configure o bloco `detalhe:` da fonte — veja a fonte `freitas` no
`config.yaml` como modelo.

Outras leiloeiras que valem a pena ligar, pelo volume de retomado de banco:
Sodré Santoro, Superbid, Mega Leilões, Parque dos Leilões, Vip Leilões, Milan,
além dos leilões do Detran-PR.

**Entre sempre pelo domínio oficial digitado, nunca por anúncio patrocinado** —
sites clonados de leiloeira são um golpe comum e bem feito.

## Ajustes que você provavelmente vai querer

`config.yaml`:

- **`ano_tolerancia`** (padrão `1`): muita leiloeira publica o ano/modelo no
  campo de ano. Com `1`, um lote de ano adjacente entra, mas **nunca como
  APROVADO** — ele cai em VERIFICAR com um aviso explícito pra conferir o CRLV.
  Coloque `0` se quiser corte seco.
- **`avisar_status`**: tire `VERIFICAR` se o volume de aviso incomodar.
- **`preco_max`** (por alvo, opcional): lance acima disso fica gravado no banco
  mas não gera aviso.
- **`termos_proibidos`**: já exclui Comfortline, Trendline, GLI e Jetta 1.4 no
  Jetta; e EXS/EXR/Si/Type R nos Civics. Adicione o que aparecer de ruído.
- **avisos**: descomente o bloco `telegram` (token do @BotFather + seu chat_id)
  ou aponte `webhook` pro seu n8n.

## Limites — o que o robô NÃO faz

O robô lê o que a leiloeira escreveu. Ele não lê o laudo em PDF, não consulta o
Detran e não sabe o que o anúncio omitiu. Remarcação de motor/chassi em
particular muitas vezes só aparece no laudo do pátio, não no título do lote —
por isso o status VERIFICAR existe e por isso ele nunca aprova um lote omisso.

Antes de dar lance, em qualquer lote: leia o edital inteiro (custos de pátio,
remoção e comissão do leiloeiro, que costumam somar 5 a 15% em cima do lance),
puxe a consulta de restrições pelo chassi, e confirme no laudo a situação de
motor e chassi.

## Arquivos

```
main.py                      CLI
requirements.txt             dependências (PyYAML, requests, beautifulsoup4)
config.yaml                  alvos, fontes e avisos — é aqui que você mexe
leilao_monitor/matcher.py    reconhecimento de modelo/ano + regras de restrição
leilao_monitor/fontes.py     adaptadores json / html / arquivo
leilao_monitor/estado.py     memória SQLite + Telegram/webhook
leilao_monitor/fipe.py       consulta FIPE com cache + custo total
leilao_monitor/bot.py        bot do Telegram com menu, busca sob demanda e alertas
testes_matcher.py            64 testes (regras, filtros, FIPE, mensagem e bot)
exemplos/lotes_exemplo.json  lotes fictícios pra validar sem rede
```
