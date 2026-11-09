"""Servidor MCP da Central de Salas, em Streamable HTTP na porta 7301.

Expoe tres tools e um resource. Toda regra de sala mora aqui: conflito, politica
e alternativas sao decisao deste processo, nunca do agente.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
)
from pydantic import BaseModel

DADOS = Path(__file__).resolve().parent.parent / "dados"
POLITICA = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
VERSAO_POLITICA = POLITICA.splitlines()[0].removeprefix("versao:").strip()

SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_ALTERNATIVAS = 3

# Validade do requestState: dentro da faixa de 5 a 30 minutos exigida.
VALIDADE_REQUEST_STATE_SEGUNDOS = 600.0
# Chave atribuida pelo servidor em inputRequests; o cliente devolve a mesma.
CHAVE_ELICITATION = "escolha_de_sala"

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVAS = "Sem alternativas disponiveis no intervalo"


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class ErroDeRegra(Exception):
    """Violacao de regra de negocio: vira isError com a mensagem exata."""


SALAS: dict[str, dict[str, Any]] = {
    sala["id"]: sala for sala in json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
}
# Em memoria: reservas criadas valem ate o processo terminar.
RESERVAS: list[dict[str, str]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))


def _carregar_segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com: "
            'python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return segredo


servidor = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(
        keys=[_carregar_segredo()],
        ttl=VALIDADE_REQUEST_STATE_SEGUNDOS,
    ),
)


def _instante(valor: str) -> datetime:
    """Interpreta um ISO 8601 no fuso da politica; sem offset, assume -03:00."""
    try:
        instante = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        raise ErroDeRegra(f"Data invalida: {valor}") from None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=SAO_PAULO)
    return instante.astimezone(SAO_PAULO)


def _validar(sala: str, inicio: str, fim: str) -> None:
    """Aplica as regras de sala e de politica comuns a consulta e a reserva."""
    if sala not in SALAS:
        raise ErroDeRegra(f"Sala inexistente: {sala}")
    comeco, termino = _instante(inicio), _instante(fim)
    if not (ABERTURA <= comeco.time() <= FECHAMENTO and ABERTURA <= termino.time() <= FECHAMENTO):
        raise ErroDeRegra(ERRO_JANELA)
    if termino - comeco > DURACAO_MAXIMA:
        raise ErroDeRegra(ERRO_DURACAO)
    if termino <= comeco:
        raise ErroDeRegra(ERRO_INTERVALO)


def _conflitos(sala: str, inicio: str, fim: str) -> list[dict[str, str]]:
    comeco, termino = _instante(inicio), _instante(fim)
    return [
        reserva
        for reserva in RESERVAS
        if reserva["sala"] == sala and comeco < _instante(reserva["fim"]) and _instante(reserva["inicio"]) < termino
    ]


def _alternativas(sala: str, inicio: str, fim: str) -> list[str]:
    """Salas livres com capacidade igual ou maior, por capacidade e depois por id."""
    minimo = SALAS[sala]["capacidade"]
    candidatas = [
        candidata
        for candidata in SALAS.values()
        if candidata["id"] != sala and candidata["capacidade"] >= minimo and not _conflitos(candidata["id"], inicio, fim)
    ]
    candidatas.sort(key=lambda candidata: (candidata["capacidade"], candidata["id"]))
    return [candidata["id"] for candidata in candidatas[:MAXIMO_ALTERNATIVAS]]


def _erro_de_execucao(mensagem: str) -> CallToolResult:
    """Devolvido no lugar do modelo declarado: o SDK nao valida resultados com isError."""
    return CallToolResult(content=[TextContent(type="text", text=mensagem)], is_error=True)


def _criar_reserva(pedido: dict[str, Any], sala: str) -> ReservaOut:
    reserva = {
        "id": f"res-{len(RESERVAS) + 1:04d}",
        "sala": sala,
        "inicio": pedido["inicio"],
        "fim": pedido["fim"],
        "responsavel": pedido["responsavel"],
    }
    RESERVAS.append(reserva)
    return ReservaOut(
        reserva=reserva["id"],
        sala=sala,
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=VERSAO_POLITICA,
    )


def _exigir_elicitation_form(ctx: Context) -> None:
    """Recusa com -32021 quem nao declarou elicitation em form mode neste request."""
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    # Pela spec, `elicitation: {}` equivale a form mode; so url, sem form, nao serve.
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data={"requiredCapabilities": {"elicitation": {"form": {}}}},
    )


def _perguntar_alternativa(pedido: dict[str, Any], ctx: Context) -> InputRequiredResult | CallToolResult:
    """Termina a resposta pedindo informacao: o servidor nao inicia request."""
    alternativas = _alternativas(pedido["sala"], pedido["inicio"], pedido["fim"])
    if not alternativas:
        return _erro_de_execucao(ERRO_SEM_ALTERNATIVAS)
    _exigir_elicitation_form(ctx)
    return InputRequiredResult(
        input_requests={
            CHAVE_ELICITATION: ElicitRequest(
                params=ElicitRequestFormParams(
                    mode="form",
                    message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                    requested_schema={
                        "type": "object",
                        "properties": {
                            "sala": {
                                "type": "string",
                                "title": "Sala",
                                "description": "Sala alternativa escolhida",
                                "enum": alternativas,
                            }
                        },
                        "required": ["sala"],
                    },
                )
            )
        },
        # Texto puro aqui: o RequestStateBoundary do SDK sela com AES-256-GCM ao sair
        # e verifica ao voltar, antes de esta funcao enxergar o valor.
        request_state=json.dumps({**pedido, "alternativas": alternativas}),
    )


def _retomar(ctx: Context) -> ReservaOut | InputRequiredResult | CallToolResult:
    """Conclui a reserva so com o que foi selado; os argumentos do retry sao ignorados."""
    selado = json.loads(ctx.request_state or "{}")
    pedido = {campo: selado[campo] for campo in ("sala", "inicio", "fim", "responsavel")}
    resposta = (ctx.input_responses or {}).get(CHAVE_ELICITATION)
    if not isinstance(resposta, ElicitResult):
        return _perguntar_alternativa(pedido, ctx)
    if resposta.action == "decline":
        return ReservaOut(reservado=False, motivo="recusado")
    if resposta.action == "cancel":
        return ReservaOut(reservado=False, motivo="cancelado")
    escolhida = (resposta.content or {}).get("sala")
    # A sala pode ter sido ocupada durante a pausa: nesse caso, pergunta de novo.
    if escolhida not in selado["alternativas"] or _conflitos(str(escolhida), pedido["inicio"], pedido["fim"]):
        return _perguntar_alternativa(pedido, ctx)
    return _criar_reserva(pedido, str(escolhida))


@servidor.tool()
async def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**sala) for sala in SALAS.values()])


@servidor.tool()
async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    try:
        _validar(sala, inicio, fim)
    except ErroDeRegra as erro:
        return _erro_de_execucao(str(erro))  # type: ignore[return-value]
    conflitos = [
        ConflitoOut(id=r["id"], inicio=r["inicio"], fim=r["fim"], responsavel=r["responsavel"])
        for r in _conflitos(sala, inicio, fim)
    ]
    return Disponibilidade(sala=sala, livre=not conflitos, conflitos=conflitos)


@servidor.tool()
async def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if ctx.request_state is not None:
        return _retomar(ctx)  # type: ignore[return-value]
    try:
        _validar(sala, inicio, fim)
    except ErroDeRegra as erro:
        return _erro_de_execucao(str(erro))  # type: ignore[return-value]
    pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    if _conflitos(sala, inicio, fim):
        return _perguntar_alternativa(pedido, ctx)  # type: ignore[return-value]
    return _criar_reserva(pedido, sala)


@servidor.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    """Politica de uso das salas."""
    return POLITICA


class RegistroDeRequests:
    """Registra no stderr cada POST recebido, inclusive os que o SDK vai recusar."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        mensagens = []
        corpo = b""
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break
        self._registrar(corpo)

        async def reproduzir() -> dict[str, Any]:
            return mensagens.pop(0) if mensagens else await receive()

        await self.app(scope, reproduzir, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            request = json.loads(corpo)
        except ValueError:
            print("mcp request ilegivel: corpo nao e JSON", file=sys.stderr, flush=True)
            return
        if not isinstance(request, dict):
            print("mcp request ilegivel: corpo nao e objeto", file=sys.stderr, flush=True)
            return
        params = request.get("params")
        params = params if isinstance(params, dict) else {}
        meta = params.get("_meta")
        meta = meta if isinstance(meta, dict) else {}
        print(
            f"mcp request method={request.get('method')} id={request.get('id')} "
            f"name={params.get('name') or params.get('uri')} traceparent={meta.get('traceparent')}",
            file=sys.stderr,
            flush=True,
        )


def main() -> None:
    aplicacao = RegistroDeRequests(servidor.streamable_http_app(json_response=True))
    uvicorn.run(
        aplicacao,
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "7301")),
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
