from __future__ import annotations
from .adapters import (
    AdapterRegistry,
    SourceBatchRejectedError,
    SourceContractError,
    validate_supplier_part,
)
from .models import SupplierPart
from .normalize import to_catalog_part, to_quote
from .sources import PartSource
from .storage import PartsStore


class PartsPipeline:
    """Core pipeline: validates the whole batch at the adapter boundary and only
    then writes. It never sees supplier-specific raw shapes, only SupplierPart."""

    def __init__(self, source: PartSource, store: PartsStore, registry: AdapterRegistry) -> None:
        self.source = source
        self.store = store
        self.registry = registry

    def run(self, currency: str = "USD", source_ref: str = "fixture") -> int:
        accepted: list[tuple[SupplierPart, str]] = []
        rejections: list[SourceContractError] = []
        for envelope in self.source.read():
            try:
                adapter = self.registry.resolve(
                    envelope.supplier, envelope.contract_version,
                    origin=envelope.origin, locator=envelope.locator)
                part = adapter.adapt(envelope)
                validate_supplier_part(part, envelope)
            except SourceContractError as exc:
                rejections.append(exc)
            else:
                accepted.append((part, f"{source_ref}#{envelope.locator}"))
        if rejections:
            raise SourceBatchRejectedError(rejections)
        for part, record_ref in accepted:
            self.store.save_part(to_catalog_part(part))
            self.store.save_quote(to_quote(part, currency, record_ref))
        self.store.commit()
        return len(accepted)
