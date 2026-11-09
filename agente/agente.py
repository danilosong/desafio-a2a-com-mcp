"""Agente da Central de Salas: servidor A2A por fora, host MCP por dentro.

Traduz protocolo, nao decide dominio. Conflito, politica e alternativas vem
sempre do servidor MCP. Nao ha LLM: o pedido tem formato fixo e a decisao e regra.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from host_mcp import HostMcp
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, ElicitRequest, ElicitResult, InputRequiredResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://127.0.0.1:{PORTA}")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

FERRAMENTA_RESERVA = "reservar_sala"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
ESTADOS_TERMINAIS = {COMPLETED, CANCELED, FAILED}

ERRO_PARSE = -32700
ERRO_REQUEST_INVALIDO = -32600
ERRO_METODO_INEXISTENTE = -32601
ERRO_PARAMETROS_INVALIDOS = -32602
ERRO_TASK_INEXISTENTE = -32001
ERRO_OPERACAO_NAO_SUPORTADA = -32004

PEDIDO = re.compile(r"^reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(.+)$")
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-([0-9a-f]{2})")

CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


class ErroRpc(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


@dataclass
class Pausa:
    """O que a retomada precisa. O `request_state` e opaco: guardar e ecoar, nada mais."""

    argumentos: dict[str, Any]
    politica: str
    chave: str
    opcoes: list[str]
    request_state: str | None


@dataclass
class Task:
    id: str
    context_id: str
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artefatos: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str | None = None
    trace_flags: str = "01"
    pausa: Pausa | None = None

    def para_wire(self) -> dict[str, Any]:
        """Lista os campos publicos um a um: `pausa` nunca sai deste processo."""
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artefatos,
        }


# O estado das Tasks fica em memoria, inclusive o requestState das pausadas.
TASKS: dict[str, Task] = {}


def _novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _transitar(task: Task, estado: str, texto: str | None = None) -> None:
    task.estado = estado
    task.mensagem = None
    if texto is not None:
        task.mensagem = {
            "messageId": _novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": task.id,
            "contextId": task.context_id,
        }
        task.historico.append(task.mensagem)
    print(f"a2a task={task.id} estado={estado}", file=sys.stderr, flush=True)


def _adotar_trace(task: Task, cabecalho: str | None) -> None:
    """Fixa na Task o trace-id do primeiro traceparent valido que o cliente enviar."""
    if task.trace_id is not None or cabecalho is None:
        return
    encontrado = TRACEPARENT.match(cabecalho.strip().lower())
    if encontrado and set(encontrado.group(1)) != {"0"}:
        task.trace_id, task.trace_flags = encontrado.groups()


def _meta(task: Task) -> dict[str, Any]:
    """`_meta` de um request MCP: mesmo trace-id da Task, span-id novo a cada chamada."""
    if task.trace_id is None:
        return {}
    return {"traceparent": f"00-{task.trace_id}-{secrets.token_hex(8)}-{task.trace_flags}"}


def _texto_de(mensagem: dict[str, Any]) -> str:
    partes = [parte.get("text", "") for parte in mensagem["parts"] if isinstance(parte, dict)]
    return " ".join(partes).strip()


def _opcoes_da_elicitation(pedido: Any) -> list[str]:
    if not isinstance(pedido, ElicitRequest):
        raise ValueError("o servidor MCP pediu algo diferente de elicitation")
    campo = pedido.params.requested_schema["properties"]["sala"]
    return list(campo["enum"]) if "enum" in campo else [campo["const"]]


def _aplicar_resultado(
    task: Task,
    argumentos: dict[str, Any],
    politica: str,
    resultado: CallToolResult | InputRequiredResult,
) -> None:
    """A ponte: traduz o resultado de um tools/call no proximo estado da Task."""
    if isinstance(resultado, InputRequiredResult):
        chave, pedido = next(iter((resultado.input_requests or {}).items()))
        opcoes = _opcoes_da_elicitation(pedido)
        task.pausa = Pausa(
            argumentos=argumentos,
            politica=politica,
            chave=chave,
            opcoes=opcoes,
            request_state=resultado.request_state,
        )
        _transitar(task, INPUT_REQUIRED, f"alternativas: {', '.join(opcoes)}")
        return

    if resultado.is_error:
        texto = " ".join(bloco.text for bloco in resultado.content if bloco.type == "text")
        _transitar(task, FAILED, texto)
        return

    dados = resultado.structured_content or {}
    if dados.get("reservado") is False:
        _transitar(task, CANCELED, f"Reserva nao realizada: {dados.get('motivo')}.")
        return

    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": politica,
    }
    task.artefatos.append(
        {"artifactId": _novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva)}]}
    )
    _transitar(task, COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


def _falhar(task: Task, erro: Exception) -> None:
    print(f"a2a task={task.id} falha: {erro!r}", file=sys.stderr, flush=True)
    _transitar(task, FAILED, erro.message if isinstance(erro, MCPError) else "Falha ao falar com o servidor MCP")


async def _abrir_task(host: HostMcp, mensagem: dict[str, Any], traceparent: str | None) -> Task:
    pedido = PEDIDO.match(_texto_de(mensagem))
    if pedido is None:
        raise ErroRpc(
            ERRO_PARAMETROS_INVALIDOS,
            "Pedido fora do formato: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
        )
    sala, inicio, fim, responsavel = pedido.groups()
    argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}

    task = Task(id=_novo_id("task"), context_id=_novo_id("ctx"))
    task.historico.append(mensagem)
    _adotar_trace(task, traceparent)
    TASKS[task.id] = task
    print(f"a2a task={task.id} estado={SUBMITTED}", file=sys.stderr, flush=True)
    # Cede o loop para que um GetTask concorrente consiga observar o SUBMITTED.
    await asyncio.sleep(0)
    _transitar(task, WORKING)

    try:
        if FERRAMENTA_RESERVA not in await host.descobrir_ferramentas(_meta(task)):
            raise ValueError(f"o servidor MCP nao oferece a tool {FERRAMENTA_RESERVA}")
        politica = await host.ler_versao_da_politica(_meta(task))
        resultado = await host.chamar_tool(FERRAMENTA_RESERVA, argumentos, _meta(task))
        _aplicar_resultado(task, argumentos, politica, resultado)
    except Exception as erro:
        _falhar(task, erro)
    return task


async def _continuar_task(host: HostMcp, task: Task, mensagem: dict[str, Any], traceparent: str | None) -> Task:
    if task.estado in ESTADOS_TERMINAIS:
        raise ErroRpc(ERRO_OPERACAO_NAO_SUPORTADA, f"A Task {task.id} ja terminou em {task.estado}")
    if task.estado != INPUT_REQUIRED or task.pausa is None:
        raise ErroRpc(ERRO_OPERACAO_NAO_SUPORTADA, f"A Task {task.id} nao esta aguardando resposta")

    pausa = task.pausa
    task.historico.append(mensagem)
    _adotar_trace(task, traceparent)

    escolha = ESCOLHA.match(_texto_de(mensagem))
    valor = escolha.group(1).strip() if escolha else None
    if valor == "recusar":
        resposta = ElicitResult(action="decline")
    elif valor in pausa.opcoes:
        resposta = ElicitResult(action="accept", content={"sala": valor})
    else:
        _transitar(task, INPUT_REQUIRED, f"alternativas: {', '.join(pausa.opcoes)}")
        return task

    # Sai de INPUT_REQUIRED antes do primeiro await: uma continuacao concorrente
    # encontra a Task em WORKING e e recusada, sem gastar o requestState duas vezes.
    task.pausa = None
    _transitar(task, WORKING)
    try:
        # A volta da ponte: mesmo tools/call, id novo, mesma chave em inputResponses
        # e o requestState ecoado sem modificacao.
        resultado = await host.chamar_tool(
            FERRAMENTA_RESERVA,
            pausa.argumentos,
            _meta(task),
            input_responses={pausa.chave: resposta},
            request_state=pausa.request_state,
        )
        _aplicar_resultado(task, pausa.argumentos, pausa.politica, resultado)
    except Exception as erro:
        _falhar(task, erro)
    return task


def _buscar_task(task_id: Any) -> Task:
    task = TASKS.get(task_id) if isinstance(task_id, str) else None
    if task is None:
        raise ErroRpc(ERRO_TASK_INEXISTENTE, f"Task inexistente: {task_id}")
    return task


async def _send_message(host: HostMcp, parametros: dict[str, Any], traceparent: str | None) -> Task:
    mensagem = parametros.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroRpc(ERRO_PARAMETROS_INVALIDOS, "SendMessage exige message com parts")
    task_id = mensagem.get("taskId")
    if task_id is None:
        return await _abrir_task(host, mensagem, traceparent)
    return await _continuar_task(host, _buscar_task(task_id), mensagem, traceparent)


def _resposta_erro(id_request: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_request, "error": {"code": codigo, "message": mensagem}})


async def endpoint_a2a(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return _resposta_erro(None, ERRO_PARSE, "Corpo nao e JSON")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _resposta_erro(None, ERRO_REQUEST_INVALIDO, "Request JSON-RPC invalido")

    id_request = corpo.get("id")
    parametros = corpo.get("params")
    if not isinstance(parametros, dict):
        return _resposta_erro(id_request, ERRO_PARAMETROS_INVALIDOS, "params precisa ser um objeto")

    try:
        if corpo["method"] == "SendMessage":
            task = await _send_message(request.app.state.host, parametros, request.headers.get("traceparent"))
        elif corpo["method"] == "GetTask":
            task = _buscar_task(parametros.get("id"))
        else:
            raise ErroRpc(ERRO_METODO_INEXISTENTE, f"Metodo inexistente: {corpo['method']}")
    except ErroRpc as erro:
        return _resposta_erro(id_request, erro.codigo, erro.mensagem)
    return JSONResponse({"jsonrpc": "2.0", "id": id_request, "result": {"task": task.para_wire()}})


async def agent_card(_: Request) -> JSONResponse:
    return JSONResponse(CARD)


@asynccontextmanager
async def ciclo_de_vida(aplicacao: Starlette) -> AsyncIterator[None]:
    async with HostMcp(URL_MCP) as host:
        aplicacao.state.host = host
        yield


aplicacao = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    uvicorn.run(
        aplicacao,
        host=os.environ.get("AGENTE_HOST", "127.0.0.1"),
        port=PORTA,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
