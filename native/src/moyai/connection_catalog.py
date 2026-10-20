"""Bounded schema reuse on workers; live tools and credentials are never cached."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.capabilities import ToolSearch
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import AgentNativeTool, ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from .contracts import RunScope


class ConnectionDefinitionChanged(ToolFailed):
    """Provider discovery must refresh before another dispatch."""


def connection_search(
    _ctx: RunContext[RunScope], queries: Sequence[str], tools: Sequence[ToolDefinition],
) -> list[str]:
    """Rank requested operations and repository names above shared boilerplate.

    Each query gets a first match before filling the result window, and duplicate
    operations from other accounts cannot crowd every distinct operation out.
    This only orders already-authorized definitions; it never grants access.
    """
    def words(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.lower()))

    documents = {tool.name: words(f"{tool.name} {tool.description or ''}") for tool in tools}
    frequencies: dict[str, int] = {}
    for terms in documents.values():
        for term in terms:
            frequencies[term] = frequencies.get(term, 0) + 1
    ranked = []
    for query in queries:
        terms = words(query)
        scores = []
        for tool in tools:
            overlap = terms & documents[tool.name]
            if not overlap:
                continue
            name_words = words(tool.name)
            score = sum((3 if word in name_words else 1) * math.log(2 + len(tools) / frequencies[word]) for word in overlap)
            operation = re.sub(r"^[a-z]+_[a-z0-9]{8}_", "", tool.name).removeprefix("github_")
            operation_terms = words(operation)
            exact_operation = bool(operation_terms) and operation_terms <= terms
            repository = re.match(r"Connected GitHub repository: (.*?)\. ", tool.description or "")
            if repository:
                score += 20 * len(terms & words(repository[1]))
            # Operation matches precede repository-label matches regardless of
            # the number of words in a repository name.
            scores.append((exact_operation, score, tool.name))
        ranked.append(sorted(scores, key=lambda item: (-item[0], -item[1], item[2])))
    result = list(dict.fromkeys(matches[0][2] for matches in ranked if matches))
    operations = {re.sub(r"^[a-z]+_[a-z0-9]{8}_", "", name) for name in result}
    rest = sorted((item for matches in ranked for item in matches), key=lambda item: (-item[0], -item[1], item[2]))
    duplicates = []
    for _exact_operation, _score, name in rest:
        if name in result or name in duplicates:
            continue
        operation = re.sub(r"^[a-z]+_[a-z0-9]{8}_", "", name)
        if operation in operations:
            duplicates.append(name)
        else:
            result.append(name)
            operations.add(operation)
    return result + duplicates


class ConnectionToolSearch(ToolSearch[RunScope]):
    """Use SDK discovery without uploading the deferred corpus to the provider."""

    def get_native_tools(self) -> Sequence[AgentNativeTool[RunScope]]:
        # Native search, including client-executed native search, sends the whole
        # corpus on the wire. The SDK's local fallback sends only revealed tools.
        return ()


@dataclass(frozen=True)
class Definition:
    tool_def: ToolDefinition
    max_retries: int


@dataclass
class Loading:
    lock: asyncio.Lock
    users: int = 0


@dataclass(frozen=True)
class Entry:
    expires: float
    definitions: dict[str, Definition]
    size: int


class ConnectionCatalog:
    """An optimization, not authority: scoped to a turn and bounded by age/size."""

    def __init__(self, *, ttl: float = 300, max_bytes: int = 16_000_000):
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.entries: OrderedDict[str, Entry] = OrderedDict()
        self.loading: dict[str, Loading] = {}
        self.size = 0

    @staticmethod
    def key(ctx: RunContext[RunScope]) -> str:
        # Include the complete admitted grant snapshot, not just connection IDs.
        # Encrypted credential material contributes only to this digest.
        value = asdict(ctx.deps)
        # Execution-version decisions can resolve after initial SDK discovery;
        # Continue-As-New increments the segment of the same admitted turn.
        # Neither changes connector authority or definitions.
        # Keep all admitted identity/grant/provenance fields in the cache key.
        for field in ("interactive_delegation", "workflow_public_events", "history_segment"):
            value.pop(field)
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def discard(self, key: str) -> None:
        if entry := self.entries.pop(key, None):
            self.size -= entry.size

    def get(self, key: str) -> dict[str, Definition] | None:
        now = time.monotonic()
        for expired in [name for name, entry in self.entries.items() if entry.expires <= now]:
            self.discard(expired)
        if entry := self.entries.get(key):
            self.entries.move_to_end(key)
            return copy.deepcopy(entry.definitions)
        return None

    def put(self, key: str, tools: dict[str, ToolsetTool[RunScope]]) -> None:
        # Custom validators can depend on a live tool instance. Leave those
        # catalogs on the ordinary SDK path rather than dropping their hooks.
        if any(tool.args_validator_func is not None for tool in tools.values()):
            return
        definitions = {
            name: Definition(copy.deepcopy(tool.tool_def), tool.max_retries)
            for name, tool in tools.items()
        }
        size = len(TypeAdapter(list[ToolDefinition]).dump_json(
            [item.tool_def for item in definitions.values()]
        ))
        if size > self.max_bytes:
            return
        self.discard(key)
        while self.entries and (self.size + size > self.max_bytes or len(self.entries) >= 32):
            self.discard(next(iter(self.entries)))
        self.entries[key] = Entry(time.monotonic() + self.ttl, definitions, size)
        self.size += size


class CatalogToolset(AbstractToolset[RunScope]):
    """List cached schemas; reconstruct a native tool for validation/execution.

    Instances live for one SDK activity (or one ordinary agent run). Only the
    immutable definitions above survive across activities. Temporal still records
    discovery results and replays them without touching workers or providers.
    """

    def __init__(
        self, catalog: ConnectionCatalog, ctx: RunContext[RunScope],
        factory: Callable[[str | None], AbstractToolset[RunScope]],
        *, reconstruct: Callable[[ToolDefinition], AbstractToolset[RunScope] | None] | None = None,
    ):
        self.catalog = catalog
        self.key = catalog.key(ctx)
        self.factory = factory
        self.reconstruct = reconstruct
        self.native: AbstractToolset[RunScope] | None = None
        self.selection: str | None = None
        self.live_tools: dict[str, ToolsetTool[RunScope]] | None = None

    @property
    def id(self) -> str:
        return "moyai_connection_catalog"

    async def __aexit__(self, *args: object) -> bool | None:
        if self.native is not None:
            return await self.native.__aexit__(*args)
        return None

    async def _live(
        self, ctx: RunContext[RunScope], name: str | None = None, definition: ToolDefinition | None = None,
    ) -> dict[str, ToolsetTool[RunScope]]:
        if self.native is not None and self.selection is not None and self.selection != name:
            await self.native.__aexit__(None, None, None)
            self.native, self.live_tools = None, None
        if self.native is None:
            reconstructed = self.reconstruct(definition) if self.reconstruct and definition else None
            native = await (reconstructed if reconstructed is not None else self.factory(name)).for_run(ctx)
            await native.__aenter__()
            self.native = native
            self.selection = name
        if self.live_tools is None:
            self.native = await self.native.for_run_step(ctx)
            self.live_tools = await self.native.get_tools(ctx)
        return self.live_tools

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        cached = self.catalog.get(self.key)
        if cached is None:
            loading = self.catalog.loading.setdefault(self.key, Loading(asyncio.Lock()))
            loading.users += 1
            try:
                async with loading.lock:
                    cached = self.catalog.get(self.key)
                    if cached is None:
                        tools = await self._live(ctx)
                        self.catalog.put(self.key, tools)
                        return tools
            finally:
                loading.users -= 1
                if not loading.users:
                    del self.catalog.loading[self.key]
        # Durable discovery serializes only tool_def/max_retries. The SDK calls
        # get_tool_for_tool_def below to obtain the real validator before dispatch.
        return {
            name: ToolsetTool(
                toolset=self, tool_def=item.tool_def, max_retries=item.max_retries,
                args_validator=SchemaValidator(core_schema.dict_schema()),
            )
            for name, item in cached.items()
        }

    async def get_tool_for_tool_def(
        self, tool_def: ToolDefinition, ctx: RunContext[RunScope],
    ) -> ToolsetTool[RunScope]:
        current = (await self._live(ctx, tool_def.name, tool_def)).get(tool_def.name)
        if current is None or (
            current.tool_def.parameters_json_schema != tool_def.parameters_json_schema
            or current.tool_def.kind != tool_def.kind
            or current.tool_def.metadata != tool_def.metadata
        ):
            self.catalog.discard(self.key)
            raise ToolFailed("The connection's tool definition changed. Discover its tools again before retrying.")
        return current

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        current = await self.get_tool_for_tool_def(tool.tool_def, ctx)
        # This also validates ordinary (non-Temporal) calls that used a cached
        # listing. Durable calls already validated against this native validator.
        args = current.args_validator.validate_python(tool_args)
        assert self.native is not None
        native = self.native
        try:
            return await native.call_tool(name, args, ctx, current)
        except ConnectionDefinitionChanged:
            self.catalog.discard(self.key)
            if self.native is native:
                self.native, self.live_tools, self.selection = None, None, None
                await native.__aexit__(None, None, None)
            raise
