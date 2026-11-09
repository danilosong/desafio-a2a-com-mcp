"""O lado de dentro do agente: host MCP que fala com o servidor por HTTP.

Cada request e um POST independente, com versao, capabilities e trace no proprio
`_meta`: nada e inferido de request anterior ou de conexao aberta. O README explica
por que o `ClientSession` do SDK nao e usado aqui; os tipos do protocolo sao os dele.
"""

from __future__ import annotations

import itertools
import re
from types import TracebackType
from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ElicitResult,
    InputRequiredResult,
    ListToolsResult,
    ReadResourceResult,
    TextResourceContents,
)

PROTOCOLO = "2026-07-28"
URI_POLITICA = "politica://uso"
VERSAO_NA_POLITICA = re.compile(r"^versao:\s*(\S+)")
TEMPO_LIMITE_SEGUNDOS = 30.0

META_DO_CLIENTE = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
    "io.modelcontextprotocol/clientInfo": {"name": "agente-central-de-salas", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
}


class HostMcp:
    """Cliente MCP de vida longa: guarda o pool HTTP, nunca estado de protocolo."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._http = httpx2.AsyncClient(timeout=TEMPO_LIMITE_SEGUNDOS)
        self._ids = itertools.count(1)

    async def __aenter__(self) -> HostMcp:
        return self

    async def __aexit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        rastro: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def _request(
        self, metodo: str, params: dict[str, Any], meta: dict[str, Any], nome: str | None = None
    ) -> dict[str, Any]:
        """Emite um request com id novo e os headers que espelham o corpo."""
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome is not None:
            cabecalhos["Mcp-Name"] = nome
        corpo = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": metodo,
            "params": {**params, "_meta": {**META_DO_CLIENTE, **meta}},
        }
        resposta = (await self._http.post(self._url, json=corpo, headers=cabecalhos)).json()
        if "error" in resposta:
            erro = resposta["error"]
            raise MCPError(code=erro["code"], message=erro["message"], data=erro.get("data"))
        return resposta["result"]

    async def descobrir_ferramentas(self, meta: dict[str, Any]) -> set[str]:
        resultado = ListToolsResult.model_validate(await self._request("tools/list", {}, meta))
        return {ferramenta.name for ferramenta in resultado.tools}

    async def ler_versao_da_politica(self, meta: dict[str, Any]) -> str:
        bruto = await self._request("resources/read", {"uri": URI_POLITICA}, meta, nome=URI_POLITICA)
        conteudo = ReadResourceResult.model_validate(bruto).contents[0]
        if not isinstance(conteudo, TextResourceContents):
            raise ValueError("o resource da politica nao veio como texto")
        versao = VERSAO_NA_POLITICA.match(conteudo.text.splitlines()[0])
        if versao is None:
            raise ValueError("a primeira linha da politica nao declara a versao")
        return versao.group(1)

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        meta: dict[str, Any],
        input_responses: dict[str, ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Um unico tools/call. O input_required volta cru para quem chamou decidir."""
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = {
                chave: resposta.model_dump(mode="json", by_alias=True, exclude_none=True)
                for chave, resposta in input_responses.items()
            }
        if request_state is not None:
            params["requestState"] = request_state
        bruto = await self._request("tools/call", params, meta, nome=nome)
        if bruto.get("resultType") == "input_required":
            return InputRequiredResult.model_validate(bruto)
        return CallToolResult.model_validate(bruto)
