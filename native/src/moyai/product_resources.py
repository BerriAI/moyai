"""Reuse product services without importing or starting an old agent runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .product_files import ProductFilesAdapter
from .product_history import ProductHistoryAdapter
from .product_identity import ProductIdentityAdapter

if TYPE_CHECKING:
    from app.config import Settings as ProductSettings

    from .product_role_authority import ProductRoleAuthority


class _StoreProxy:
    """Bind existing route services before startup without opening a database."""

    schema_updates = False

    def __init__(self, resources: ProductResources):
        self.resources = resources

    def __getattr__(self, name: str) -> Any:
        if self.resources.store is None:
            raise RuntimeError("Product resources have not started.")
        return getattr(self.resources.store, name)


class ProductResources:
    def __init__(self, settings: ProductSettings, native_engine: AsyncEngine):
        if not settings.moyai_database_url:
            raise ValueError("Existing-product execution requires the canonical PostgreSQL database.")
        self.settings, self.native_engine = settings, native_engine
        url = settings.moyai_database_url
        self.engine = create_async_engine(
            "postgresql+asyncpg://" + url.split("://", 1)[1],
            pool_pre_ping=True,
            connect_args={"server_settings": {"statement_timeout": "5000"}},
        )
        from app.blob_storage import ObjectStorage
        from app.google_sso import GoogleSignIn
        from app.security import Security
        from app.user_roles import UserRoles

        self.store: Any = None
        self.authority: ProductRoleAuthority | None = None
        self.objects = ObjectStorage(settings)
        self.store_proxy = _StoreProxy(self)
        self.roles = UserRoles(self.store_proxy, settings)
        self.security = Security(settings, self.roles)
        self.google = GoogleSignIn(settings, self.security, self.store_proxy)
        self.identity = ProductIdentityAdapter(
            settings, self.engine, native_engine=self.native_engine,
            allow_shared_identities=True,
        )
        self.history = ProductHistoryAdapter(
            self.identity, self.engine, schema=settings.moyai_database_schema,
        )
        self.files = ProductFilesAdapter(self.history, objects=self.objects)

    async def start(self) -> None:
        # The existing data/auth helpers have no dependency on the execution
        # engines. Verify mode prevents Store from initializing or migrating the
        # product schema; canonical auth writes still use its existing locks.
        from app.db import Store
        from app.db import database as source_io

        settings = self.settings
        def construct() -> None:
            # Assign inside the drained operation: cancellation after allocation
            # must leave the owner/pool reachable by Runtime.start's cleanup.
            self.store = Store(
                settings.data_dir, settings.agent_model,
                object_storage=self.objects, database_url=settings.moyai_database_url,
                database_schema=settings.moyai_database_schema, schema_mode="verify",
                database_pool_size=settings.moyai_database_pool_size,
                application_instance=settings.moyai_runtime_role != "standalone",
                runtime_role=settings.moyai_runtime_role, runtime_settings=settings,
            )
        await source_io(construct)
        self.files.artifacts = self.store.artifacts

    async def admit_role(self) -> ProductRoleAuthority | None:
        """Publish/join the canonical contract only after both schemas verify."""
        if self.settings.moyai_runtime_role == "standalone":
            return None  # Explicit local preview; never a distributed deployment.
        if self.store is None:
            raise RuntimeError("Product resources have not started.")
        from app.db import database as source_io

        from .product_role_authority import ProductRoleAuthority
        await source_io(self.store.database.configure_runtime, self.settings)
        self.authority = await source_io(ProductRoleAuthority.from_source, self.store.database)
        return self.authority

    async def close(self) -> None:
        try:
            if self.store is not None:
                from app.db import database as source_io
                await source_io(self.store.close)
                self.store = None
        finally:
            await self.engine.dispose()
