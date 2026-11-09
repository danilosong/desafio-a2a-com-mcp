# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do MBA Engenharia de Software com IA, curso de MCP e A2A.

São dois processos separados, em Python:

- `servidor-mcp/servidor.py`: servidor MCP em Streamable HTTP, na porta `7301`, com o SDK oficial `mcp` v2 (revisão `2026-07-28`). Toda regra de sala mora aqui.
- `agente/agente.py` e `agente/host_mcp.py`: agente A2A v1.0 em JSON-RPC, na porta `7300`, que é host MCP por dentro e fala com o servidor por HTTP.

## Como rodar

Requisitos: Python 3.10 ou superior. Todos os comandos partem da raiz de um clone limpo.

Instale cada processo no seu próprio ambiente:

```bash
python3 -m venv servidor-mcp/.venv
servidor-mcp/.venv/bin/pip install ./servidor-mcp

python3 -m venv agente/.venv
agente/.venv/bin/pip install ./agente
```

No primeiro terminal, gere a chave de integridade, exporte-a e suba o servidor MCP. O processo não inicia sem `REQUEST_STATE_SECRET`, e a chave tem no mínimo 32 bytes de aleatoriedade. Nunca a coloque no repositório.

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
servidor-mcp/.venv/bin/python servidor-mcp/servidor.py
```

No segundo terminal, suba o agente:

```bash
agente/.venv/bin/python agente/agente.py
```

No terceiro terminal, rode o validador:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

As reservas ficam em memória e cada execução do validador as altera. Antes de rodar o validador de novo, ou de seguir os passos manuais com `curl`, reinicie os dois processos. Para que um `requestState` continue válido depois de reiniciar o servidor MCP, reinicie no mesmo terminal, com a mesma chave exportada.

Portas e endereços têm estes padrões e podem ser trocados por variável de ambiente:

| Variável | Padrão | Processo |
| --- | --- | --- |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `7301` | servidor MCP |
| `AGENTE_HOST`, `AGENTE_PORT` | `127.0.0.1`, `7300` | agente |
| `AGENTE_URL` | `http://127.0.0.1:7300` | agente, URL publicada no card |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | agente, endpoint do servidor MCP |

O stderr do servidor MCP registra uma linha por request, com `method`, `id`, `name` e `traceparent`. O stderr do agente registra as transições de estado de cada Task.

## Onde a ponte acontece

O `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` na função `_aplicar_resultado`, em `agente/agente.py`: quando o resultado do `tools/call` é um `InputRequiredResult`, ela guarda em `Task.pausa` a chave da elicitation, as opções, os argumentos originais e o `requestState`, e pausa a Task com a linha `alternativas: ...`. O `requestState` volta para o servidor na função `_continuar_task`, no mesmo arquivo: ao receber `escolha=<valor>`, ela traduz a escolha em `accept` ou `decline` e repete o `tools/call` por `HostMcp.chamar_tool`, em `agente/host_mcp.py`, com um id de JSON-RPC novo, a mesma chave em `inputResponses` e o `requestState` ecoado sem modificação.

## Decisões técnicas

**Proteção do `requestState`.** A tool `reservar_sala` devolve um `InputRequiredResult` com o pedido e as alternativas em texto puro, e o `RequestStateBoundary` do SDK sela esse valor na saída e o verifica na volta, antes de a tool enxergá-lo. O selo é AES-256-GCM, com chave derivada por HKDF de `REQUEST_STATE_SECRET`, configurado em `RequestStateSecurity`. Qualquer adulteração é recusada com `-32602`.

**Validade.** O `requestState` vale 10 minutos (`VALIDADE_REQUEST_STATE_SEGUNDOS`, em `servidor-mcp/servidor.py`). Depois disso é recusado com `-32602`.

**Nada guardado entre as rodadas.** O `requestState` carrega `sala`, `inicio`, `fim`, `responsavel` e as alternativas oferecidas. O servidor reconstrói o pedido a partir dele e não guarda nada em memória entre o `input_required` e o retry, por isso o retry funciona depois de um restart. O SDK também liga o selo ao nome da tool e a um resumo dos argumentos, então um retry com argumentos adulterados é recusado com `-32602`.

**Estado das Tasks.** Fica em memória no processo do agente, no dicionário `TASKS` de `agente/agente.py`. O `requestState` de uma Task pausada fica em `Task.pausa`, por Task, e `Task.para_wire` lista um a um os campos publicados, então ele nunca sai em resposta A2A.

**Comportamentos que o enunciado não fecha.**

- Se a sala escolhida for ocupada durante a pausa, ou a resposta não trouxer uma alternativa oferecida, o servidor não reserva e responde `input_required` de novo com as alternativas atuais. O agente volta a pausar a Task com a nova lista.
- Data que não é ISO 8601 devolve `isError` com `Data invalida: <valor>`. Data sem offset é interpretada em `-03:00`.
- Um `SendMessage` novo fora do formato fixo é recusado com `-32602`, sem abrir Task.

**Limitação do SDK no cliente MCP.** O agente não usa o `ClientSession` do SDK para emitir requests, e usa os tipos do SDK (`mcp_types`, `MCPError`) para ler as respostas. O enunciado exige declarar `{"elicitation": {"form": {}}}`, mas o `ClientSession` da versão 2.3.0 declara sempre `form` e `url` juntos, e sobrescreve em cada request o que vier no `_meta`. Trechos de `mcp/client/session.py`:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

```python
meta[CLIENT_CAPABILITIES_META_KEY] = capabilities
```

Como o protocolo não tem sessão, o cliente em `agente/host_mcp.py` é um POST por request, com `_meta` e headers explícitos. O servidor MCP usa o SDK normalmente.

## Saída do validador

```
trace-id desta execucao: ae0a740b18177fb9386ebdf64990896f
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
