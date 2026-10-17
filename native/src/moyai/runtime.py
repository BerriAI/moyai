"""Trusted process resources. Workflows call only the registered native agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.models import Model
from temporalio.client import Client

from .agent import inference_models, make_agent
from .contracts import RunScope
from .database import Database
from .security import Security
from .settings import Settings
from .storage import ObjectStore, encrypted_converter
from .stores import NativeStores

if TYPE_CHECKING:
    from .product_automations import ProductAutomationService
    from .product_billing_routes import ProductBillingService
    from .product_config import ProductConfig
    from .product_connections import ProductConnections
    from .product_continuation import ProductContinuationService
    from .product_continuation_workspace import ProductWorkspaceCompatibility
    from .product_drain import ProductDrainService
    from .product_inference import ProductInferenceService
    from .product_memory import ProductMemoryService
    from .product_memory_review import ProductMemoryReviewService
    from .product_resources import ProductResources
    from .product_secrets import ProductSecrets
    from .product_skill_catalog import ProductSkillCatalog
    from .product_slack import ProductSlack


class Runtime:
    def __init__(self, settings: Settings, *, product_config: ProductConfig | None = None):
        if settings.product_mode == "existing" and product_config is None:
            from .product_config import load_product_config
            product_config = load_product_config()
            if product_config is None or product_config.product_mode != "existing":
                raise ValueError("Existing product mode requires explicit MOYAI_CORE=pydantic configuration.")
        if product_config is not None:
            if product_config.product_mode == "existing":
                from .product_deployment import require_startable
                # Also fence direct ASGI factories, not just the CLI/Render
                # selector. Constructing Runtime cannot bypass role admission.
                require_startable(product_config)
            settings = Settings.model_validate({**settings.model_dump(), **product_config.native_overrides()})
        self.product_config = product_config
        self.settings = settings
        self.security = Security(settings.master_key)
        self.objects = ObjectStore(settings, self.security)
        self.database = Database(settings, self.security)
        self.stores = NativeStores(settings)
        self.product: ProductResources | None = None
        if product_config is not None and product_config.product_mode == "existing":
            from .product_resources import ProductResources
            self.product = ProductResources(product_config.legacy_settings(), self.database.engine)
        self.product_identity = self.product.identity if self.product else None
        self.product_history = self.product.history if self.product else None
        self.product_files = self.product.files if self.product else None
        self.product_settings = self.product.settings if self.product else None
        self.product_engine = self.product.engine if self.product else None
        self.database.product_identity = self.product_identity
        self.product_billing: ProductBillingService | None = None
        self.product_automations: ProductAutomationService | None = None
        self.product_connections: ProductConnections | None = None
        self.product_inference: ProductInferenceService | None = None
        self.product_slack: ProductSlack | None = None
        self.product_memory: ProductMemoryService | None = None
        self.product_memory_review: ProductMemoryReviewService | None = None
        self.product_skill_catalog: ProductSkillCatalog | None = None
        self.product_secrets: ProductSecrets | None = None
        self.product_automation_events = None
        self.product_workspace: ProductWorkspaceCompatibility | None = None
        self.product_continuations: ProductContinuationService | None = None
        self.product_drain: ProductDrainService | None = None
        if self.product is not None:
            from .product_automations import ProductAutomationService
            from .product_billing_routes import ProductBillingService
            from .product_connections import ProductConnections
            from .product_continuation import ProductContinuationService
            from .product_continuation_workspace import ProductWorkspaceCompatibility
            from .product_inference import ProductInferenceService
            from .product_memory import ProductMemoryService
            from .product_secrets import ProductSecrets
            from .product_skill_catalog import ProductSkillCatalog
            from .product_slack import ProductSlack, ProductSlackAuthority
            self.product_connections = ProductConnections(self.product, self.database)
            self.database.product_connections = self.product_connections
            self.product_workspace = ProductWorkspaceCompatibility(self)
            self.product_continuations = ProductContinuationService(self, workspace=self.product_workspace)
            self.product_inference = ProductInferenceService.from_runtime(self)
            self.database.product_inference = self.product_inference
            self.product.identity.slack_authority = ProductSlackAuthority(self.product.identity)
            self.product_slack = ProductSlack(self)
            self.product_memory = ProductMemoryService(self.database, self.product.identity)
            self.product_skill_catalog = ProductSkillCatalog(self.product.identity)
            self.product_secrets = ProductSecrets(self)
            self.product_billing = ProductBillingService.from_runtime(self)
            self.product_automations = ProductAutomationService.from_runtime(self)
            self.product_automation_events = self.product_automations.events if self.product_automations else None
            if self.product_config is not None and self.product_config.role != "standalone":
                from .product_drain import ProductDrainService
                self.product_drain = ProductDrainService.from_runtime(self)
                self.database.product_drain = self.product_drain
        self.client: Client | None = None
        from .skill_invocations import SkillInvocations
        from .slack_activity import SlackActivityService
        from .work_progress import WorkProgress
        self.skill_invocations = SkillInvocations(self)
        self.work_progress = WorkProgress(self)
        self.slack_activity = SlackActivityService(self)
        self.agent: Agent[RunScope, str | DeferredToolRequests]
        self.durability: TemporalDurability[RunScope]
        self.models: dict[str, Model] = {}
        self._models_owned = False

    async def start(
        self, *, models: dict[str, Model] | None = None,
        workspace: AbstractCapability[RunScope] | None = None,
        skills_directory: str = "/opt/moyai/skills",
    ) -> None:
        try:
            if self.product is not None:
                await self.product.start()
            await self.database.start()
            await self.stores.start()
            if self.product is not None:
                from .product_continuation_schema import verify_source_continuation_fence
                await verify_source_continuation_fence(
                    self.database.engine,
                    source_schema=self.product.settings.moyai_database_schema,
                    native_schema=self.settings.database_schema,
                )
                authority = await self.product.admit_role()
                if authority is not None:
                    self.database.sessions = authority.sessions(self.database.engine)
                    self.stores.bind_authority(authority)
            if self.product_drain is not None:
                from .product_drain import ProductDrainError
                from .product_drain_models import ProductDrainState
                async with self.database.sessions() as db:
                    if await db.get(ProductDrainState, 1) is None:
                        raise ProductDrainError("The maintenance bootstrap receipt is missing.")
            if self.product_inference is not None:
                await self.product_inference.start(
                    recovery=bool(self.product_config and self.product_config.role == "standalone"),
                )
            self.models = models or inference_models(self.settings)
            self._models_owned = not models
            product_capabilities: list[AbstractCapability[RunScope]] = []
            memory = None
            if self.product_memory is not None:
                from .product_memory_review import ProductMemoryReviewService
                from .product_memory_store import memory_capability
                memory = memory_capability(
                    self.product_memory,
                    repository_scope=self.product_workspace.repository_scope if self.product_workspace else None,
                    native_store=self.stores.memory,
                )
                self.product_memory_review = ProductMemoryReviewService(
                    self.product_memory, self.models,
                    repository_scope=self.product_workspace.repository_scope if self.product_workspace else None,
                    runtime=self,
                )
                self.database.product_memory_review = self.product_memory_review
                if self.product_inference is not None:
                    self.product_inference.memory_review = self.product_memory_review
                # Availability is reported only after both actual Temporal
                # poller types advertise the registered review capability.
                self.product_memory_review.mark_registered()
            if self.product_automations is not None:
                from .product_automation_tools import capability
                product_capabilities.append(capability(self.product_automations))
                if self.product_skill_catalog is not None:
                    from .product_skill_tools import capability as skill_capability
                    product_capabilities.append(skill_capability(self.product_skill_catalog, self.product_automations))
            if self.product_secrets is not None:
                from .product_credential_tools import capability as credential_capability
                product_capabilities.append(credential_capability(self.product_secrets.execution))
            self.agent, self.durability = make_agent(
                self.settings, self.database, self.stores, self.objects, models=self.models, workspace=workspace,
                skills_directory=skills_directory, product_capabilities=product_capabilities, memory=memory,
            )
        except BaseException:
            await self.close()
            raise

    @property
    def product_store(self):
        return self.product.store_proxy if self.product else None

    async def connect(self) -> Client:
        if self.client is None:
            self.client = await Client.connect(
                self.settings.temporal_address,
                namespace=self.settings.temporal_namespace,
                tls=self.settings.temporal_tls,
                api_key=self.settings.temporal_api_key.get_secret_value() or None,
                plugins=[PydanticAIPlugin()],
                data_converter=encrypted_converter(self.security, self.objects),
            )
        return self.client

    async def close(self) -> None:
        self.work_progress.clear()
        try:
            if self.product_inference is not None:
                await self.product_inference.close()
        finally:
            try:
                if self._models_owned:
                    from pydantic_ai.models.openai import OpenAIResponsesModel
                    # Registered models share one public provider client. Close
                    # it once, after worker/request drain, before DB resources.
                    clients = {
                        id(model.client): model.client for model in self.models.values()
                        if isinstance(model, OpenAIResponsesModel)
                    }
                    self._models_owned = False
                    for client in clients.values():
                        await client.close()
            finally:
                try:
                    await self.stores.close()
                finally:
                    try:
                        await self.database.close()
                    finally:
                        if self.product is not None:
                            await self.product.close()


_runtime: Runtime | None = None


def configure(runtime: Runtime) -> None:
    global _runtime
    _runtime = runtime


def current() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Moyai runtime is not initialized.")
    return _runtime

