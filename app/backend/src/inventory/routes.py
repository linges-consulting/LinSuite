"""Settings → Products: the retail catalog (M4 #56).

**`catalog.manage` gates every write**, reused rather than a new key: its registry
description already reads "Add and change services, products, prices and the resources they
need" (`auth/capabilities.py`), and inventing `inventory.manage` beside it would be two keys
answering the same question — an administrator granted one expecting the other to follow.

**Reading the catalog needs no capability**, mirroring `scheduling/services.py`'s
`/catalog/services`: retail checkout (a later ticket) is front-desk work like booking, not
administration, so any signed-in account may read `GET /api/catalog/products`. Only active
products, and only their active variants, come back — the same reasoning
`CatalogServiceOut.staff_ids` gives for leaving a departed practitioner out: nothing
downstream has a use for what cannot be sold.

**Products and variants are edited on separate endpoints, never as one bulk save.** Unlike
`Service.eligible_staff`/`requirements` — sets with no identity of their own, replaced whole
— a variant *has* identity: its id is what a future invoice line will point at. So a variant
is created, edited, deactivated and reactivated one at a time, the same shape `staff.py` and
`resources.py` already use for rows with a life of their own.
"""

import uuid
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires, RequiresAny
from auth.models import User
from auth.session import CurrentUser
from billing.tax_routes import TaxConvention, normalize_tax_component_keys
from core.audit import record_event
from core.db import SessionDep
from core.forms import blank_to_none, refuse_emptied_field
from inventory.models import Product, ProductVariant
from inventory.stock import is_below_threshold

router = APIRouter(prefix="/admin/products", tags=["inventory"])
# The read side: any signed-in account, no capability — see the module docstring.
public = APIRouter(prefix="/catalog", tags=["inventory"])

CatalogManager = Annotated[User, Depends(Requires("catalog.manage"))]
# #95: `list_products` also backs Settings → Products for a receive- or adjust-only lead, who
# needs the roster to pick a variant but holds neither write capability.
ProductReader = Annotated[
    User, Depends(RequiresAny("catalog.manage", "inventory.receive", "inventory.adjust"))
]

# --- what goes over the wire ---------------------------------------------------------------


class VariantOut(BaseModel):
    """The administrator's view of one variant: every stored fact, inactive ones included —
    this is the editing surface, and a screen that hid a deactivated SKU could never explain
    why a barcode scan comes back "already in use"."""

    id: str
    product_id: str
    name: str
    sku: str
    barcode: str | None
    price_cents: int
    quantity_on_hand: int
    low_stock_threshold: int
    # Always shown, regardless of the business's email opt-in (#62's own acceptance
    # criterion) — computed fresh off the live pair below, never off the persisted
    # `low_stock_alerted` email-dedup flag, so a PATCH to the threshold (which does not pass
    # through `inventory/stock.py::record_movement`) can never leave a stale badge on screen.
    is_low_stock: bool
    tax_component_keys: list[str]
    tax_convention: str
    active: bool
    sort_order: int


class ProductOut(BaseModel):
    """The administrator's table and dialog: the product, and every variant under it."""

    id: str
    name: str
    description: str | None
    active: bool
    sort_order: int
    variants: list[VariantOut]


class CatalogVariantOut(BaseModel):
    """The checkout screen's shape. `active` is absent because everything here is active —
    the same reasoning `CatalogServiceOut` gives."""

    id: str
    name: str
    sku: str
    barcode: str | None
    price_cents: int
    quantity_on_hand: int
    low_stock_threshold: int
    is_low_stock: bool
    tax_component_keys: list[str]
    tax_convention: str


class CatalogProductOut(BaseModel):
    id: str
    name: str
    description: str | None
    variants: list[CatalogVariantOut]


def _variant_out(variant: ProductVariant) -> VariantOut:
    return VariantOut(
        id=str(variant.id),
        product_id=str(variant.product_id),
        name=variant.name,
        sku=variant.sku,
        barcode=variant.barcode,
        price_cents=variant.price_cents,
        quantity_on_hand=variant.quantity_on_hand,
        low_stock_threshold=variant.low_stock_threshold,
        is_low_stock=is_below_threshold(variant.quantity_on_hand, variant.low_stock_threshold),
        tax_component_keys=list(variant.tax_component_keys),
        tax_convention=variant.tax_convention,
        active=variant.active,
        sort_order=variant.sort_order,
    )


def product_out(product: Product) -> ProductOut:
    return ProductOut(
        id=str(product.id),
        name=product.name,
        description=product.description,
        active=product.active,
        sort_order=product.sort_order,
        variants=[_variant_out(v) for v in product.variants],
    )


def _catalog_out(product: Product) -> CatalogProductOut:
    return CatalogProductOut(
        id=str(product.id),
        name=product.name,
        description=product.description,
        variants=[
            CatalogVariantOut(
                id=str(v.id),
                name=v.name,
                sku=v.sku,
                barcode=v.barcode,
                price_cents=v.price_cents,
                quantity_on_hand=v.quantity_on_hand,
                low_stock_threshold=v.low_stock_threshold,
                is_low_stock=is_below_threshold(v.quantity_on_hand, v.low_stock_threshold),
                tax_component_keys=list(v.tax_component_keys),
                tax_convention=v.tax_convention,
            )
            for v in product.variants
            if v.active
        ],
    )


# --- what comes in --------------------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=200)]
Sku = Annotated[str, Field(min_length=1, max_length=64)]
Barcode = Annotated[str | None, Field(max_length=64)]
# Integer cents, never negative — ten million dollars for a bottle of shampoo is a typo.
Price = Annotated[int, Field(ge=0, le=1_000_000_00)]
# Whole units. A ceiling well above anything a single-location retailer stocks, for the same
# reason `Service.duration_minutes` caps at a day: a typo should 422, not silently accept.
Count = Annotated[int, Field(ge=0, le=1_000_000)]


def _trimmed(value: str | None) -> str | None:
    return blank_to_none(value)


class ProductFields(BaseModel):
    """Everything an administrator sets on a product, create and edit alike."""

    name: Name
    description: Annotated[str | None, Field(max_length=2000)] = None
    sort_order: int = 0

    @field_validator("name", mode="after")
    @classmethod
    def _real_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()

    @field_validator("description", mode="after")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        return _trimmed(value)


_PRODUCT_NOT_NULLABLE = ("name", "sort_order")


class ProductPatch(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    sort_order: int | None = None

    @field_validator("name", "description", mode="after")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        return _trimmed(value)


class VariantFields(BaseModel):
    """Everything an administrator sets on a variant, create and edit alike.

    **No `quantity_on_hand`** (#61, spec §162): a new variant starts at zero and every stock
    change after that is a `stock_movements` row written by `inventory/stock.py::
    record_movement`, behind `inventory.receive`/`inventory.adjust` (`stock_routes.py`).
    `extra="forbid"` makes a client still sending it a 422, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    sku: Sku
    barcode: Barcode = None
    price_cents: Price = 0
    low_stock_threshold: Count = 0
    tax_component_keys: list[str] = []
    tax_convention: TaxConvention = "exclusive"
    sort_order: int = 0

    @field_validator("name", "sku", mode="after")
    @classmethod
    def _real_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()

    @field_validator("barcode", mode="after")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        return _trimmed(value)


_VARIANT_NOT_NULLABLE = (
    "name",
    "sku",
    "price_cents",
    "low_stock_threshold",
    "tax_component_keys",
    "tax_convention",
    "sort_order",
)


class VariantPatch(BaseModel):
    # No `quantity_on_hand` — see `VariantFields`.
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    sku: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    barcode: Barcode = None
    price_cents: Price | None = None
    low_stock_threshold: Count | None = None
    tax_component_keys: list[str] | None = None
    tax_convention: TaxConvention | None = None
    sort_order: int | None = None

    @field_validator("name", "sku", mode="after")
    @classmethod
    def _real_value(cls, value: str | None) -> str | None:
        return _trimmed(value)

    @field_validator("barcode", mode="after")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        return _trimmed(value)


# --- duplicate handling ----------------------------------------------------------------------

# Named here so the handlers below can tell a real conflict apart from anything else an
# IntegrityError might mean — the same reasoning `scheduling/services.py::_is_duplicate_name`
# gives, generalised to the three unique indexes this module has instead of one.
_CONFLICTS = {
    "ux_products_name": lambda name: f"A product named “{name}” already exists.",
    "ux_product_variants_product_name": lambda name: (
        f"This product already has a variant named “{name}”."
    ),
    "ux_product_variants_sku": lambda name: f"SKU “{name}” is already in use.",
    "ux_product_variants_barcode": lambda name: f"Barcode “{name}” is already in use.",
}


def _violated_index(error: IntegrityError) -> str | None:
    """Which of `_CONFLICTS`' indexes this violation is, or `None` when it is something
    else — a foreign key, a CHECK reached by a route that skipped its own validation. asyncpg
    carries `constraint_name` on the exception it raises; SQLAlchemy's dialect wraps that, so
    the real one may be a `__cause__` down, and the message is the last resort."""
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name in _CONFLICTS:
            return name
    for name in _CONFLICTS:
        if name in str(error.orig):
            return name
    return None


def _raise_conflict(error: IntegrityError, value: str) -> NoReturn:
    """409 for a known unique-index violation; the original `error` for anything else — a
    foreign key or a CHECK reached by a route that skipped its own validation should read as
    a 500, not a false "already exists"."""
    index = _violated_index(error)
    if index is None:
        raise error
    raise HTTPException(status_code=409, detail=_CONFLICTS[index](value)) from None


# --- reading -----------------------------------------------------------------------------


async def _roster(db: SessionDep, *, include_inactive: bool) -> list[Product]:
    rows = await db.scalars(
        select(Product)
        .where(*([] if include_inactive else [Product.active]))
        .order_by(Product.active.desc(), Product.sort_order, Product.name)
    )
    return list(rows)


@router.get("")
async def list_products(
    _: ProductReader, db: SessionDep, include_inactive: bool = False
) -> dict[str, list[ProductOut]]:
    return {
        "products": [product_out(p) for p in await _roster(db, include_inactive=include_inactive)]
    }


@public.get("/products")
async def catalog(_: CurrentUser, db: SessionDep) -> dict[str, list[CatalogProductOut]]:
    """What retail checkout reads: active products, active variants only. Any signed-in
    account, no capability — see the module docstring."""
    products = await _roster(db, include_inactive=False)
    return {"products": [_catalog_out(p) for p in products]}


async def load_product(db: SessionDep, product_id: uuid.UUID) -> Product:
    product = await db.scalar(
        select(Product).where(Product.id == product_id).execution_options(populate_existing=True)
    )
    if product is None:
        raise HTTPException(status_code=404, detail="No such product.")
    return product


async def load_variant(
    db: SessionDep, product_id: uuid.UUID, variant_id: uuid.UUID
) -> ProductVariant:
    variant = await db.scalar(
        select(ProductVariant).where(
            ProductVariant.id == variant_id, ProductVariant.product_id == product_id
        )
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="No such variant.")
    return variant


# --- products: creating and editing --------------------------------------------------------


@router.post("", status_code=201)
async def create_product(
    payload: ProductFields, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    product = Product(**payload.model_dump(), active=True)
    db.add(product)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        _raise_conflict(error, payload.name)

    record_event(
        db,
        "product.created",
        target_type="product",
        target_id=str(product.id),
        actor_user_id=admin.id,
        metadata={"name": product.name},
    )
    await db.commit()
    return product_out(await load_product(db, product.id))


@router.patch("/{product_id}")
async def update_product(
    product_id: uuid.UUID, payload: ProductPatch, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    product = await load_product(db, product_id)
    sent = payload.model_dump(exclude_unset=True)
    refuse_emptied_field(sent, _PRODUCT_NOT_NULLABLE)

    changed = []
    for field, value in sent.items():
        if getattr(product, field) != value:
            setattr(product, field, value)
            changed.append(field)

    if changed:
        name = product.name
        try:
            await db.flush()
        except IntegrityError as error:
            await db.rollback()
            _raise_conflict(error, name)
        record_event(
            db,
            "product.updated",
            target_type="product",
            target_id=str(product.id),
            actor_user_id=admin.id,
            metadata={"changed": sorted(changed)},
        )
    await db.commit()
    return product_out(await load_product(db, product_id))


@router.post("/{product_id}/deactivate")
async def deactivate_product(
    product_id: uuid.UUID, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    return await _set_product_active(db, product_id, admin, active=False)


@router.post("/{product_id}/reactivate")
async def reactivate_product(
    product_id: uuid.UUID, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    return await _set_product_active(db, product_id, admin, active=True)


async def _set_product_active(
    db: SessionDep, product_id: uuid.UUID, admin: User, *, active: bool
) -> ProductOut:
    """No hard delete, ever: an invoice line that already sold a variant of this product
    must never point at nothing. Variants keep whatever `active` state they already had — a
    product coming back does not silently resurrect a variant somebody deliberately retired.
    """
    product = await load_product(db, product_id)
    if product.active == active:
        return product_out(product)

    product.active = active
    record_event(
        db,
        "product.reactivated" if active else "product.deactivated",
        target_type="product",
        target_id=str(product.id),
        actor_user_id=admin.id,
        metadata={"name": product.name},
    )
    await db.commit()
    return product_out(await load_product(db, product_id))


# --- variants: creating and editing ---------------------------------------------------------


@router.post("/{product_id}/variants", status_code=201)
async def create_variant(
    product_id: uuid.UUID, payload: VariantFields, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    await load_product(db, product_id)  # 404 before anything is written
    fields = payload.model_dump()
    fields["tax_component_keys"] = await normalize_tax_component_keys(
        db, payload.tax_component_keys
    )
    variant = ProductVariant(**fields, product_id=product_id, active=True)
    db.add(variant)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        _raise_conflict(error, payload.sku)

    record_event(
        db,
        "product_variant.created",
        target_type="product_variant",
        target_id=str(variant.id),
        actor_user_id=admin.id,
        metadata={"product_id": str(product_id), "name": variant.name, "sku": variant.sku},
    )
    await db.commit()
    return product_out(await load_product(db, product_id))


@router.patch("/{product_id}/variants/{variant_id}")
async def update_variant(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    payload: VariantPatch,
    admin: CatalogManager,
    db: SessionDep,
) -> ProductOut:
    variant = await load_variant(db, product_id, variant_id)
    sent = payload.model_dump(exclude_unset=True)
    refuse_emptied_field(sent, _VARIANT_NOT_NULLABLE)
    if "tax_component_keys" in sent:
        sent["tax_component_keys"] = await normalize_tax_component_keys(
            db, sent["tax_component_keys"]
        )

    changed = []
    for field, value in sent.items():
        if getattr(variant, field) != value:
            setattr(variant, field, value)
            changed.append(field)

    if changed:
        sku = variant.sku
        try:
            await db.flush()
        except IntegrityError as error:
            await db.rollback()
            _raise_conflict(error, sku)
        record_event(
            db,
            "product_variant.updated",
            target_type="product_variant",
            target_id=str(variant.id),
            actor_user_id=admin.id,
            metadata={"changed": sorted(changed)},
        )
    await db.commit()
    return product_out(await load_product(db, product_id))


@router.post("/{product_id}/variants/{variant_id}/deactivate")
async def deactivate_variant(
    product_id: uuid.UUID, variant_id: uuid.UUID, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    return await _set_variant_active(db, product_id, variant_id, admin, active=False)


@router.post("/{product_id}/variants/{variant_id}/reactivate")
async def reactivate_variant(
    product_id: uuid.UUID, variant_id: uuid.UUID, admin: CatalogManager, db: SessionDep
) -> ProductOut:
    return await _set_variant_active(db, product_id, variant_id, admin, active=True)


async def _set_variant_active(
    db: SessionDep,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    admin: User,
    *,
    active: bool,
) -> ProductOut:
    """No hard delete: an invoice line that already sold this variant must never lose what
    it points at."""
    variant = await load_variant(db, product_id, variant_id)
    if variant.active != active:
        variant.active = active
        record_event(
            db,
            "product_variant.reactivated" if active else "product_variant.deactivated",
            target_type="product_variant",
            target_id=str(variant.id),
            actor_user_id=admin.id,
            metadata={"sku": variant.sku},
        )
        await db.commit()
    return product_out(await load_product(db, product_id))
