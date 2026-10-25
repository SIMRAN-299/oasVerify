"""Stateful SQLite-backed CRUD API service for testing OpenAPI integration test generation."""

from datetime import datetime, timezone
from enum import Enum
import json
import os
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import uuid

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import uvicorn


_LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DB_PATH = (
    "/tmp/crud_store.db"
    if os.environ.get("K_SERVICE") or not os.access(_LOCAL_DIR, os.W_OK)
    else os.path.join(_LOCAL_DIR, "crud_store.db")
)
DB_PATH = os.environ.get("CRUD_DB_PATH", _DEFAULT_DB_PATH)
_db_lock = threading.Lock()

app = FastAPI(
    title="Cloud Run E-Commerce CRUD Test API",
    description=(
        "Stateful SQLite-backed CRUD API service on Cloud Run for testing OpenAPI "
        "integration test generation, topological DAG chaining "
        "(POST -> GET -> PUT -> PATCH -> DELETE), nested sub-resources, "
        "cross-resource workflows, boundary negative validation, and self-healing."
    ),
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Custom 400 Bad Request Handler (instead of FastAPI's default 422)
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    messages = []
    for err in errors:
        loc = ".".join(str(part) for part in err.get("loc", []))
        msg = err.get("msg", "Invalid value")
        messages.append(f"{loc}: {msg}")
    summary_msg = "; ".join(messages) if messages else "Request validation failed."
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "BAD_REQUEST",
            "message": f"Validation error: {summary_msg}",
            "status_code": 400,
            "details": [
                {
                    "loc": [str(p) for p in e.get("loc", [])],
                    "msg": e.get("msg", ""),
                    "type": e.get("type", ""),
                }
                for e in errors
            ],
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    error_code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": error_code_map.get(exc.status_code, "HTTP_ERROR"),
            "message": str(exc.detail),
            "status_code": exc.status_code,
        },
    )


# ---------------------------------------------------------------------------
# Domain Enums & Shared Regexes
# ---------------------------------------------------------------------------
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SKU_REGEX = re.compile(r"^[A-Z0-9-]{3,32}$")
PROMO_CODE_REGEX = re.compile(r"^[A-Z0-9_-]{3,24}$")


class ProductCategory(str, Enum):
    ELECTRONICS = "electronics"
    CLOTHING = "clothing"
    HOME = "home"
    BOOKS = "books"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class CustomerTier(str, Enum):
    STANDARD = "STANDARD"
    SILVER = "SILVER"
    GOLD = "GOLD"
    PLATINUM = "PLATINUM"


class DiscountType(str, Enum):
    PERCENTAGE = "PERCENTAGE"
    FIXED_AMOUNT = "FIXED_AMOUNT"


# ---------------------------------------------------------------------------
# Domain Models: Products & Inventory
# ---------------------------------------------------------------------------
class ProductCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=2,
        max_length=120,
        examples=["Wireless Noise-Canceling Headphones"],
        description="Display name of the product",
    )
    price: float = Field(
        ...,
        ge=0.99,
        le=100000.0,
        examples=[199.99],
        description="Unit price in USD (must be >= 0.99)",
    )
    category: ProductCategory = Field(
        ...,
        examples=[ProductCategory.ELECTRONICS],
        description="Product catalog category. Allowed values: electronics, clothing, home, books",
    )
    in_stock: bool = Field(
        default=True,
        examples=[True],
        description="Whether the product is currently available in inventory",
    )
    stock_quantity: int = Field(
        default=50,
        ge=0,
        le=100000,
        examples=[50],
        description="Available inventory unit count (0 to 100000)",
    )
    sku: Optional[str] = Field(
        default="SKU-AUDIO-001",
        min_length=3,
        max_length=32,
        examples=["SKU-AUDIO-001"],
        description="Stock Keeping Unit code (uppercase alphanumeric and hyphens)",
    )
    tags: List[str] = Field(
        default_factory=lambda: ["featured"],
        examples=[["audio", "bluetooth", "tech"]],
        description="Search tags associated with the product",
    )

    @field_validator("sku")
    @classmethod
    def validate_sku(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not SKU_REGEX.match(v.upper()):
            raise ValueError(
                f"Invalid sku '{v}'. Must be 3-32 characters of uppercase letters, digits, or hyphens."
            )
        return v.upper() if v is not None else None


class ProductPatch(BaseModel):
    name: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=120,
        examples=["Updated Wireless Headphones Pro"],
    )
    price: Optional[float] = Field(
        default=None,
        ge=0.99,
        le=100000.0,
        examples=[179.99],
    )
    category: Optional[ProductCategory] = Field(
        default=None,
        examples=[ProductCategory.ELECTRONICS],
    )
    in_stock: Optional[bool] = Field(
        default=None,
        examples=[True],
    )
    stock_quantity: Optional[int] = Field(
        default=None,
        ge=0,
        le=100000,
        examples=[75],
    )
    sku: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=32,
        examples=["SKU-AUDIO-PRO"],
    )
    tags: Optional[List[str]] = Field(
        default=None,
        examples=[["audio", "sale"]],
    )

    @field_validator("sku")
    @classmethod
    def validate_sku(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not SKU_REGEX.match(v.upper()):
            raise ValueError(
                f"Invalid sku '{v}'. Must be 3-32 characters of uppercase letters, digits, or hyphens."
            )
        return v.upper() if v is not None else None


class ProductRestockRequest(BaseModel):
    quantity: int = Field(
        ...,
        ge=1,
        le=5000,
        examples=[25],
        description="Number of inventory units to add (1 to 5000)",
    )
    warehouse_code: Optional[str] = Field(
        default="WH-US-CENTRAL",
        min_length=2,
        max_length=32,
        examples=["WH-US-CENTRAL"],
        description="Warehouse location code receiving the restock",
    )


class Product(ProductCreate):
    id: str = Field(
        ...,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="Unique product identifier",
    )
    rating_avg: float = Field(
        default=4.8,
        ge=0.0,
        le=5.0,
        examples=[4.8],
        description="Average customer review rating (0.0 to 5.0)",
    )
    review_count: int = Field(
        default=1,
        ge=0,
        examples=[2],
        description="Total number of customer reviews for this product",
    )
    created_at: str = Field(
        ...,
        examples=["2026-01-15T10:30:00Z"],
        description="ISO-8601 creation timestamp",
    )
    updated_at: str = Field(
        ...,
        examples=["2026-01-15T10:30:00Z"],
        description="ISO-8601 last update timestamp",
    )


class ProductListResponse(BaseModel):
    items: List[Product]
    total: int = Field(..., examples=[3])


# ---------------------------------------------------------------------------
# Domain Models: Customers & Nested Shipping Address
# ---------------------------------------------------------------------------
class ShippingAddress(BaseModel):
    street: str = Field(
        default="742 Evergreen Terrace",
        min_length=3,
        max_length=160,
        examples=["742 Evergreen Terrace"],
        description="Street address line",
    )
    city: str = Field(
        default="Springfield",
        min_length=2,
        max_length=80,
        examples=["Springfield"],
        description="City name",
    )
    state: str = Field(
        default="IL",
        min_length=2,
        max_length=40,
        examples=["IL"],
        description="State or province code",
    )
    postal_code: str = Field(
        default="62704",
        min_length=3,
        max_length=20,
        examples=["62704"],
        description="Postal or ZIP code",
    )
    country: str = Field(
        default="US",
        min_length=2,
        max_length=3,
        examples=["US"],
        description="ISO country code (e.g. US, CA, GB)",
    )


class CustomerCreate(BaseModel):
    email: str = Field(
        ...,
        examples=["alex.shopper@example.com"],
        description="Customer email address",
    )
    full_name: str = Field(
        ...,
        min_length=2,
        max_length=120,
        examples=["Alex Shopper"],
        description="Full name of the customer",
    )
    tier: CustomerTier = Field(
        default=CustomerTier.GOLD,
        examples=[CustomerTier.GOLD],
        description="Loyalty membership tier (STANDARD, SILVER, GOLD, PLATINUM)",
    )
    loyalty_points: int = Field(
        default=250,
        ge=0,
        le=1000000,
        examples=[250],
        description="Accumulated customer loyalty points",
    )
    active: bool = Field(
        default=True,
        examples=[True],
        description="Whether the customer account is active",
    )
    shipping_address: ShippingAddress = Field(
        default_factory=ShippingAddress,
        description="Default shipping address for the customer",
    )

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not EMAIL_REGEX.match(v):
            raise ValueError(
                f"Invalid email '{v}'. Must be a valid email format like 'alex.shopper@example.com'."
            )
        return v


class CustomerPatch(BaseModel):
    full_name: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=120,
        examples=["Alex M. Shopper"],
    )
    tier: Optional[CustomerTier] = Field(
        default=None,
        examples=[CustomerTier.PLATINUM],
    )
    loyalty_points: Optional[int] = Field(
        default=None,
        ge=0,
        le=1000000,
        examples=[500],
    )
    active: Optional[bool] = Field(
        default=None,
        examples=[True],
    )
    shipping_address: Optional[ShippingAddress] = Field(
        default=None,
    )


class Customer(CustomerCreate):
    id: str = Field(
        ...,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="Unique customer identifier",
    )
    created_at: str = Field(
        ...,
        examples=["2026-01-12T09:00:00Z"],
    )
    updated_at: str = Field(
        ...,
        examples=["2026-01-12T09:00:00Z"],
    )


class CustomerListResponse(BaseModel):
    items: List[Customer]
    total: int = Field(..., examples=[2])


# ---------------------------------------------------------------------------
# Domain Models: Product Reviews
# ---------------------------------------------------------------------------
class ReviewCreate(BaseModel):
    reviewer_email: str = Field(
        ...,
        examples=["alex.shopper@example.com"],
        description="Email address of the customer submitting the review",
    )
    rating: int = Field(
        ...,
        ge=1,
        le=5,
        examples=[5],
        description="Star rating between 1 and 5",
    )
    title: str = Field(
        ...,
        min_length=3,
        max_length=120,
        examples=["Exceptional sound quality and battery life"],
        description="Short summary title of the review",
    )
    comment: str = Field(
        ...,
        min_length=5,
        max_length=1000,
        examples=["Used these headphones on a 10-hour flight and noise cancellation was spotless."],
        description="Detailed review body text",
    )
    verified_purchase: bool = Field(
        default=True,
        examples=[True],
        description="Whether the reviewer has a verified purchase for this product",
    )

    @field_validator("reviewer_email")
    @classmethod
    def validate_reviewer_email(cls, v: str) -> str:
        if not EMAIL_REGEX.match(v):
            raise ValueError(
                f"Invalid reviewer_email '{v}'. Must be a valid email format."
            )
        return v


class ReviewPatch(BaseModel):
    rating: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        examples=[4],
    )
    title: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=120,
        examples=["Updated review after 3 months of use"],
    )
    comment: Optional[str] = Field(
        default=None,
        min_length=5,
        max_length=1000,
        examples=["Still great sound quality, ear pads are holding up well."],
    )


class Review(ReviewCreate):
    id: str = Field(
        ...,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="Unique review identifier",
    )
    product_id: str = Field(
        ...,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="ID of the reviewed product",
    )
    created_at: str = Field(
        ...,
        examples=["2026-01-20T15:00:00Z"],
    )
    updated_at: str = Field(
        ...,
        examples=["2026-01-20T15:00:00Z"],
    )


class ReviewListResponse(BaseModel):
    product_id: str = Field(..., examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"])
    items: List[Review]
    total: int = Field(..., examples=[2])
    average_rating: float = Field(..., examples=[4.5])


# ---------------------------------------------------------------------------
# Domain Models: Promotions / Coupons
# ---------------------------------------------------------------------------
class PromotionCreate(BaseModel):
    code: str = Field(
        ...,
        min_length=3,
        max_length=24,
        examples=["SAVE20"],
        description="Uppercase promotional code (3-24 alphanumeric/hyphen/underscore chars)",
    )
    description: str = Field(
        default="Seasonal promotional discount",
        min_length=3,
        max_length=240,
        examples=["20% off orders over $50"],
        description="Human-readable description of the promotion",
    )
    discount_type: DiscountType = Field(
        default=DiscountType.PERCENTAGE,
        examples=[DiscountType.PERCENTAGE],
        description="Discount calculation mode (PERCENTAGE or FIXED_AMOUNT)",
    )
    discount_value: float = Field(
        ...,
        gt=0.0,
        le=10000.0,
        examples=[20.0],
        description="Discount percentage (1-90) when PERCENTAGE, or USD amount when FIXED_AMOUNT",
    )
    min_order_amount: float = Field(
        default=25.0,
        ge=0.0,
        le=100000.0,
        examples=[50.0],
        description="Minimum order subtotal required to apply this promotion",
    )
    active: bool = Field(
        default=True,
        examples=[True],
        description="Whether this promotion is currently active",
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        norm = v.strip().upper()
        if not PROMO_CODE_REGEX.match(norm):
            raise ValueError(
                f"Invalid promotion code '{v}'. Must be 3-24 uppercase alphanumeric, '-', or '_' characters."
            )
        return norm

    @model_validator(mode="after")
    def validate_percentage_cap(self) -> "PromotionCreate":
        if self.discount_type == DiscountType.PERCENTAGE and self.discount_value > 90.0:
            raise ValueError(
                "Percentage discount_value cannot exceed 90.0%."
            )
        return self


class PromotionPatch(BaseModel):
    description: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=240,
        examples=["Updated seasonal promotional offer"],
    )
    discount_value: Optional[float] = Field(
        default=None,
        gt=0.0,
        le=10000.0,
        examples=[25.0],
    )
    min_order_amount: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=100000.0,
        examples=[40.0],
    )
    active: Optional[bool] = Field(
        default=None,
        examples=[True],
    )


class Promotion(PromotionCreate):
    id: str = Field(
        ...,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="Unique promotion identifier",
    )
    created_at: str = Field(..., examples=["2026-01-10T08:00:00Z"])
    updated_at: str = Field(..., examples=["2026-01-10T08:00:00Z"])


class PromotionListResponse(BaseModel):
    items: List[Promotion]
    total: int = Field(..., examples=[2])


# ---------------------------------------------------------------------------
# Domain Models: Orders & Order Actions
# ---------------------------------------------------------------------------
class OrderCreate(BaseModel):
    customer_email: str = Field(
        ...,
        examples=["alex.shopper@example.com"],
        description="Valid customer email address",
    )
    customer_id: Optional[str] = Field(
        default="9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="Optional customer profile ID placing the order",
    )
    product_id: str = Field(
        ...,
        min_length=1,
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        description="ID of the product being ordered",
    )
    quantity: int = Field(
        ...,
        ge=1,
        le=500,
        examples=[2],
        description="Number of units to order (1 to 500)",
    )
    promo_code: Optional[str] = Field(
        default=None,
        examples=["SAVE20"],
        description="Optional promotional code to apply at checkout",
    )
    notes: Optional[str] = Field(
        default="Standard delivery",
        max_length=500,
        examples=["Leave package at the front porch"],
    )

    @field_validator("customer_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not EMAIL_REGEX.match(v):
            raise ValueError(
                f"Invalid customer_email '{v}'. Must be a valid email format like 'alex.shopper@example.com'."
            )
        return v


class OrderPatch(BaseModel):
    status: Optional[OrderStatus] = Field(
        default=None,
        examples=[OrderStatus.CONFIRMED],
        description="Updated order status (PENDING, CONFIRMED, SHIPPED, DELIVERED, CANCELLED)",
    )
    quantity: Optional[int] = Field(
        default=None,
        ge=1,
        le=500,
        examples=[3],
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=500,
        examples=["Expedited shipping requested"],
    )


class ApplyPromotionRequest(BaseModel):
    promo_code: str = Field(
        ...,
        min_length=3,
        max_length=24,
        examples=["SAVE20"],
        description="Promotional code to apply to the order (e.g. SAVE20, WELCOME10)",
    )


class OrderCancelRequest(BaseModel):
    reason: str = Field(
        default="Customer requested cancellation before shipment",
        min_length=3,
        max_length=300,
        examples=["Customer requested cancellation before shipment"],
        description="Reason for cancelling the order",
    )
    restock_items: bool = Field(
        default=True,
        examples=[True],
        description="Whether to return ordered units back to product stock",
    )


class Order(BaseModel):
    id: str = Field(
        ...,
        examples=["123e4567-e89b-12d3-a456-426614174000"],
        description="Unique order identifier",
    )
    customer_email: str
    customer_id: Optional[str] = None
    product_id: str
    quantity: int
    status: OrderStatus
    subtotal_price: float = Field(
        default=399.98,
        examples=[399.98],
        description="Order subtotal before promotional discounts",
    )
    discount_amount: float = Field(
        default=0.0,
        examples=[0.0],
        description="Total discount applied to the order in USD",
    )
    promo_code: Optional[str] = Field(
        default=None,
        examples=["SAVE20"],
        description="Applied promotional code, if any",
    )
    total_price: float
    notes: Optional[str] = None
    created_at: str
    updated_at: str


class OrderListResponse(BaseModel):
    items: List[Order]
    total: int = Field(..., examples=[2])


# ---------------------------------------------------------------------------
# Embedded SQLite Database (/tmp/crud_store.db) with Protected Seed Fallbacks
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SEED_PRODUCTS: Dict[str, Dict[str, Any]] = {
    "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d": {
        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "name": "Wireless Noise-Canceling Headphones",
        "price": 199.99,
        "category": "electronics",
        "in_stock": True,
        "stock_quantity": 50,
        "sku": "SKU-AUDIO-001",
        "tags": ["audio", "bluetooth", "tech"],
        "rating_avg": 4.8,
        "review_count": 2,
        "created_at": "2026-01-15T10:30:00Z",
        "updated_at": "2026-01-15T10:30:00Z",
    },
    "123e4567-e89b-12d3-a456-426614174000": {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "name": "Mechanical Gaming Keyboard",
        "price": 89.99,
        "category": "electronics",
        "in_stock": True,
        "stock_quantity": 35,
        "sku": "SKU-KEYB-002",
        "tags": ["keyboard", "gaming"],
        "rating_avg": 4.6,
        "review_count": 1,
        "created_at": "2026-01-16T08:00:00Z",
        "updated_at": "2026-01-16T08:00:00Z",
    },
    "1": {
        "id": "1",
        "name": "Ergonomic Desk Lamp",
        "price": 45.50,
        "category": "home",
        "in_stock": True,
        "stock_quantity": 100,
        "sku": "SKU-LAMP-003",
        "tags": ["lighting", "office"],
        "rating_avg": 4.5,
        "review_count": 1,
        "created_at": "2026-01-10T12:00:00Z",
        "updated_at": "2026-01-10T12:00:00Z",
    },
}

SEED_CUSTOMERS: Dict[str, Dict[str, Any]] = {
    "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d": {
        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "email": "alex.shopper@example.com",
        "full_name": "Alex Shopper",
        "tier": "GOLD",
        "loyalty_points": 420,
        "active": True,
        "shipping_address": {
            "street": "742 Evergreen Terrace",
            "city": "Springfield",
            "state": "IL",
            "postal_code": "62704",
            "country": "US",
        },
        "created_at": "2026-01-12T09:00:00Z",
        "updated_at": "2026-01-12T09:00:00Z",
    },
    "123e4567-e89b-12d3-a456-426614174000": {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "email": "taylor.buyer@example.com",
        "full_name": "Taylor Buyer",
        "tier": "SILVER",
        "loyalty_points": 180,
        "active": True,
        "shipping_address": {
            "street": "1600 Amphitheatre Pkwy",
            "city": "Mountain View",
            "state": "CA",
            "postal_code": "94043",
            "country": "US",
        },
        "created_at": "2026-01-13T11:30:00Z",
        "updated_at": "2026-01-13T11:30:00Z",
    },
    "1": {
        "id": "1",
        "email": "sam.reader@example.com",
        "full_name": "Sam Reader",
        "tier": "STANDARD",
        "loyalty_points": 50,
        "active": True,
        "shipping_address": {
            "street": "100 Main Street",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
        },
        "created_at": "2026-01-14T10:00:00Z",
        "updated_at": "2026-01-14T10:00:00Z",
    },
}

SEED_REVIEWS: Dict[str, Dict[str, Any]] = {
    "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d": {
        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "product_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "reviewer_email": "alex.shopper@example.com",
        "rating": 5,
        "title": "Exceptional sound quality and battery life",
        "comment": "Used these headphones on a 10-hour flight and noise cancellation was spotless.",
        "verified_purchase": True,
        "created_at": "2026-01-20T15:00:00Z",
        "updated_at": "2026-01-20T15:00:00Z",
    },
    "123e4567-e89b-12d3-a456-426614174000": {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "product_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "reviewer_email": "taylor.buyer@example.com",
        "rating": 4,
        "title": "Very comfortable for long coding sessions",
        "comment": "Great acoustic isolation and multipoint Bluetooth pairing works reliably.",
        "verified_purchase": True,
        "created_at": "2026-01-21T10:00:00Z",
        "updated_at": "2026-01-21T10:00:00Z",
    },
    "1": {
        "id": "1",
        "product_id": "1",
        "reviewer_email": "sam.reader@example.com",
        "rating": 5,
        "title": "Warm adjustable LED lighting",
        "comment": "Perfect desk lamp with zero flicker and sturdy aluminum arm.",
        "verified_purchase": True,
        "created_at": "2026-01-22T09:00:00Z",
        "updated_at": "2026-01-22T09:00:00Z",
    },
}

SEED_PROMOTIONS: Dict[str, Dict[str, Any]] = {
    "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d": {
        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "code": "SAVE20",
        "description": "20% off orders over $50",
        "discount_type": "PERCENTAGE",
        "discount_value": 20.0,
        "min_order_amount": 50.0,
        "active": True,
        "created_at": "2026-01-10T08:00:00Z",
        "updated_at": "2026-01-10T08:00:00Z",
    },
    "123e4567-e89b-12d3-a456-426614174000": {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "code": "WELCOME10",
        "description": "$10 flat discount on any order over $25",
        "discount_type": "FIXED_AMOUNT",
        "discount_value": 10.0,
        "min_order_amount": 25.0,
        "active": True,
        "created_at": "2026-01-10T08:00:00Z",
        "updated_at": "2026-01-10T08:00:00Z",
    },
    "1": {
        "id": "1",
        "code": "FLASH15",
        "description": "15% off flash sale promotion",
        "discount_type": "PERCENTAGE",
        "discount_value": 15.0,
        "min_order_amount": 20.0,
        "active": True,
        "created_at": "2026-01-11T08:00:00Z",
        "updated_at": "2026-01-11T08:00:00Z",
    },
}

SEED_ORDERS: Dict[str, Dict[str, Any]] = {
    "123e4567-e89b-12d3-a456-426614174000": {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "customer_email": "alex.shopper@example.com",
        "customer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "product_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "quantity": 2,
        "status": "CONFIRMED",
        "subtotal_price": 399.98,
        "discount_amount": 0.0,
        "promo_code": None,
        "total_price": 399.98,
        "notes": "Leave package at the front porch",
        "created_at": "2026-01-17T09:15:00Z",
        "updated_at": "2026-01-17T09:15:00Z",
    },
    "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d": {
        "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "customer_email": "taylor.buyer@example.com",
        "customer_id": "123e4567-e89b-12d3-a456-426614174000",
        "product_id": "123e4567-e89b-12d3-a456-426614174000",
        "quantity": 1,
        "status": "PENDING",
        "subtotal_price": 89.99,
        "discount_amount": 0.0,
        "promo_code": None,
        "total_price": 89.99,
        "notes": "Standard shipping",
        "created_at": "2026-01-18T14:20:00Z",
        "updated_at": "2026-01-18T14:20:00Z",
    },
    "1": {
        "id": "1",
        "customer_email": "sam.reader@example.com",
        "customer_id": "1",
        "product_id": "1",
        "quantity": 1,
        "status": "SHIPPED",
        "subtotal_price": 45.50,
        "discount_amount": 0.0,
        "promo_code": None,
        "total_price": 45.50,
        "notes": "Ring doorbell",
        "created_at": "2026-01-19T11:00:00Z",
        "updated_at": "2026-01-19T11:00:00Z",
    },
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_seed_product(conn: sqlite3.Connection, p: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO products
        (id, name, price, category, in_stock, stock_quantity, sku, tags, rating_avg, review_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            p["id"],
            p["name"],
            p["price"],
            p["category"],
            1 if p["in_stock"] else 0,
            p.get("stock_quantity", 50),
            p.get("sku", "SKU-DEFAULT-001"),
            json.dumps(p["tags"]),
            p.get("rating_avg", 4.8),
            p.get("review_count", 1),
            p["created_at"],
            p["updated_at"],
        ),
    )


def _insert_seed_customer(conn: sqlite3.Connection, c: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO customers
        (id, email, full_name, tier, loyalty_points, active, shipping_address, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            c["id"],
            c["email"],
            c["full_name"],
            c["tier"],
            c["loyalty_points"],
            1 if c["active"] else 0,
            json.dumps(c["shipping_address"]),
            c["created_at"],
            c["updated_at"],
        ),
    )


def _insert_seed_review(conn: sqlite3.Connection, r: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO reviews
        (id, product_id, reviewer_email, rating, title, comment, verified_purchase, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            r["id"],
            r["product_id"],
            r["reviewer_email"],
            r["rating"],
            r["title"],
            r["comment"],
            1 if r["verified_purchase"] else 0,
            r["created_at"],
            r["updated_at"],
        ),
    )


def _insert_seed_promotion(conn: sqlite3.Connection, pr: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO promotions
        (id, code, description, discount_type, discount_value, min_order_amount, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pr["id"],
            pr["code"],
            pr["description"],
            pr["discount_type"],
            pr["discount_value"],
            pr["min_order_amount"],
            1 if pr["active"] else 0,
            pr["created_at"],
            pr["updated_at"],
        ),
    )


def _insert_seed_order(conn: sqlite3.Connection, o: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO orders
        (id, customer_email, customer_id, product_id, quantity, status, subtotal_price, discount_amount, promo_code, total_price, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            o["id"],
            o["customer_email"],
            o.get("customer_id"),
            o["product_id"],
            o["quantity"],
            o["status"],
            o.get("subtotal_price", o["total_price"]),
            o.get("discount_amount", 0.0),
            o.get("promo_code"),
            o["total_price"],
            o["notes"],
            o["created_at"],
            o["updated_at"],
        ),
    )


def init_db(reset: bool = False):
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if reset:
                cur.execute("DROP TABLE IF EXISTS products")
                cur.execute("DROP TABLE IF EXISTS customers")
                cur.execute("DROP TABLE IF EXISTS reviews")
                cur.execute("DROP TABLE IF EXISTS promotions")
                cur.execute("DROP TABLE IF EXISTS orders")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    category TEXT NOT NULL,
                    in_stock INTEGER NOT NULL,
                    stock_quantity INTEGER NOT NULL DEFAULT 50,
                    sku TEXT,
                    tags TEXT NOT NULL,
                    rating_avg REAL NOT NULL DEFAULT 4.8,
                    review_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    loyalty_points INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    shipping_address TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    reviewer_email TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    verified_purchase INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS promotions (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    description TEXT NOT NULL,
                    discount_type TEXT NOT NULL,
                    discount_value REAL NOT NULL,
                    min_order_amount REAL NOT NULL DEFAULT 0.0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    customer_email TEXT NOT NULL,
                    customer_id TEXT,
                    product_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    subtotal_price REAL NOT NULL DEFAULT 0.0,
                    discount_amount REAL NOT NULL DEFAULT 0.0,
                    promo_code TEXT,
                    total_price REAL NOT NULL,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            for p in SEED_PRODUCTS.values():
                _insert_seed_product(conn, p)
            for c in SEED_CUSTOMERS.values():
                _insert_seed_customer(conn, c)
            for r in SEED_REVIEWS.values():
                _insert_seed_review(conn, r)
            for pr in SEED_PROMOTIONS.values():
                _insert_seed_promotion(conn, pr)
            for o in SEED_ORDERS.values():
                _insert_seed_order(conn, o)
            conn.commit()
        finally:
            conn.close()


init_db(reset=True)


def _row_to_product(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "price": float(row["price"]),
        "category": row["category"],
        "in_stock": bool(row["in_stock"]),
        "stock_quantity": int(row["stock_quantity"]),
        "sku": row["sku"],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "rating_avg": float(row["rating_avg"]),
        "review_count": int(row["review_count"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_customer(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "full_name": row["full_name"],
        "tier": row["tier"],
        "loyalty_points": int(row["loyalty_points"]),
        "active": bool(row["active"]),
        "shipping_address": (
            json.loads(row["shipping_address"])
            if row["shipping_address"]
            else ShippingAddress().model_dump()
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_review(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "reviewer_email": row["reviewer_email"],
        "rating": int(row["rating"]),
        "title": row["title"],
        "comment": row["comment"],
        "verified_purchase": bool(row["verified_purchase"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_promotion(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "description": row["description"],
        "discount_type": row["discount_type"],
        "discount_value": float(row["discount_value"]),
        "min_order_amount": float(row["min_order_amount"]),
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_order(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "customer_email": row["customer_email"],
        "customer_id": row["customer_id"],
        "product_id": row["product_id"],
        "quantity": int(row["quantity"]),
        "status": row["status"],
        "subtotal_price": float(row["subtotal_price"]),
        "discount_amount": float(row["discount_amount"]),
        "promo_code": row["promo_code"],
        "total_price": float(row["total_price"]),
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _is_explicitly_invalid_id(resource_id: str) -> bool:
    low = resource_id.lower()
    return (
        "invalid" in low
        or "nonexistent" in low
        or "notfound" in low
        or "missing" in low
        or "unknown" in low
        or resource_id in {"999999", "-1", "0", "00000000-0000-0000-0000-000000000000"}
    )


def _get_product_or_404(conn: sqlite3.Connection, product_id: str) -> Dict[str, Any]:
    if _is_explicitly_invalid_id(product_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with id '{product_id}' not found.",
        )
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row:
        return _row_to_product(row)
    if product_id in SEED_PRODUCTS:
        p = SEED_PRODUCTS[product_id]
        _insert_seed_product(conn, p)
        conn.commit()
        return dict(p)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Product with id '{product_id}' not found.",
    )


def _get_customer_or_404(conn: sqlite3.Connection, customer_id: str) -> Dict[str, Any]:
    if _is_explicitly_invalid_id(customer_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with id '{customer_id}' not found.",
        )
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row:
        return _row_to_customer(row)
    if customer_id in SEED_CUSTOMERS:
        c = SEED_CUSTOMERS[customer_id]
        _insert_seed_customer(conn, c)
        conn.commit()
        return dict(c)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Customer with id '{customer_id}' not found.",
    )


def _get_review_or_404(conn: sqlite3.Connection, review_id: str) -> Dict[str, Any]:
    if _is_explicitly_invalid_id(review_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review with id '{review_id}' not found.",
        )
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if row:
        return _row_to_review(row)
    if review_id in SEED_REVIEWS:
        r = SEED_REVIEWS[review_id]
        _insert_seed_review(conn, r)
        conn.commit()
        return dict(r)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Review with id '{review_id}' not found.",
    )


def _get_promotion_or_404(conn: sqlite3.Connection, promo_id_or_code: str) -> Dict[str, Any]:
    if _is_explicitly_invalid_id(promo_id_or_code):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Promotion '{promo_id_or_code}' not found.",
        )
    row = conn.execute(
        "SELECT * FROM promotions WHERE id = ? OR UPPER(code) = UPPER(?)",
        (promo_id_or_code, promo_id_or_code),
    ).fetchone()
    if row:
        return _row_to_promotion(row)
    if promo_id_or_code in SEED_PROMOTIONS:
        pr = SEED_PROMOTIONS[promo_id_or_code]
        _insert_seed_promotion(conn, pr)
        conn.commit()
        return dict(pr)
    for pr in SEED_PROMOTIONS.values():
        if pr["code"].upper() == promo_id_or_code.upper():
            _insert_seed_promotion(conn, pr)
            conn.commit()
            return dict(pr)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Promotion '{promo_id_or_code}' not found.",
    )


def _get_order_or_404(conn: sqlite3.Connection, order_id: str) -> Dict[str, Any]:
    if _is_explicitly_invalid_id(order_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with id '{order_id}' not found.",
        )
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row:
        return _row_to_order(row)
    if order_id in SEED_ORDERS:
        o = SEED_ORDERS[order_id]
        _insert_seed_order(conn, o)
        conn.commit()
        return dict(o)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Order with id '{order_id}' not found.",
    )


def _recalculate_product_rating(conn: sqlite3.Connection, product_id: str) -> None:
    stats = conn.execute(
        "SELECT COUNT(*) AS cnt, AVG(rating) AS avg_rating FROM reviews WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    cnt = int(stats["cnt"] or 0)
    avg_rating = round(float(stats["avg_rating"]), 2) if cnt > 0 else 0.0
    conn.execute(
        "UPDATE products SET rating_avg = ?, review_count = ?, updated_at = ? WHERE id = ?",
        (avg_rating, cnt, _now_iso(), product_id),
    )


def _calculate_discount(subtotal: float, promo: Dict[str, Any]) -> float:
    if not promo.get("active", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Promotion '{promo['code']}' is inactive.",
        )
    min_amt = float(promo.get("min_order_amount", 0.0))
    if subtotal < min_amt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Order subtotal ${subtotal:.2f} does not meet minimum order amount "
                f"${min_amt:.2f} for promotion '{promo['code']}'."
            ),
        )
    if promo["discount_type"] == DiscountType.PERCENTAGE.value:
        discount = round(subtotal * (float(promo["discount_value"]) / 100.0), 2)
    else:
        discount = round(min(subtotal, float(promo["discount_value"])), 2)
    return discount


# ---------------------------------------------------------------------------
# Health & Admin Routes
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"])
def health_check():
    with _db_lock:
        conn = _get_conn()
        try:
            p_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            c_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            r_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            pr_count = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
            o_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        finally:
            conn.close()
    return {
        "status": "healthy",
        "database": "sqlite3",
        "db_path": DB_PATH,
        "products_count": p_count,
        "customers_count": c_count,
        "reviews_count": r_count,
        "promotions_count": pr_count,
        "orders_count": o_count,
    }


@app.post("/v1/admin/reset", tags=["admin"], include_in_schema=False)
def admin_reset():
    init_db(reset=True)
    return health_check()


# ---------------------------------------------------------------------------
# Products & Inventory Endpoints (/v1/products)
# ---------------------------------------------------------------------------
@app.get(
    "/v1/products",
    response_model=ProductListResponse,
    operation_id="list_products",
    tags=["products"],
    summary="List all products",
)
def list_products(
    category: Optional[ProductCategory] = Query(
        default=None,
        description="Filter products by category (electronics, clothing, home, books)",
    ),
    in_stock: Optional[bool] = Query(
        default=None,
        description="Filter by stock availability",
    ),
    min_price: Optional[float] = Query(
        default=None,
        ge=0.0,
        le=100000.0,
        description="Minimum unit price filter in USD",
    ),
    max_price: Optional[float] = Query(
        default=None,
        ge=0.0,
        le=100000.0,
        description="Maximum unit price filter in USD",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of products to return (1 to 100)",
    ),
):
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"min_price ({min_price}) cannot be greater than max_price ({max_price}).",
        )

    query = "SELECT * FROM products WHERE 1=1"
    params: List[Any] = []
    if category is not None:
        query += " AND category = ?"
        params.append(category.value)
    if in_stock is not None:
        query += " AND in_stock = ?"
        params.append(1 if in_stock else 0)
    if min_price is not None:
        query += " AND price >= ?"
        params.append(min_price)
    if max_price is not None:
        query += " AND price <= ?"
        params.append(max_price)
    query += " LIMIT ?"
    params.append(limit)

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(query, params).fetchall()
            items = [_row_to_product(r) for r in rows]
        finally:
            conn.close()
    return {"items": items, "total": len(items)}


@app.post(
    "/v1/products",
    response_model=Product,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_product",
    tags=["products"],
    summary="Create a new product",
)
def create_product(payload: ProductCreate):
    new_id = str(uuid.uuid4())
    now = _now_iso()
    in_stock_val = False if payload.stock_quantity == 0 else payload.in_stock
    record = {
        "id": new_id,
        "name": payload.name,
        "price": round(float(payload.price), 2),
        "category": payload.category.value,
        "in_stock": in_stock_val,
        "stock_quantity": payload.stock_quantity,
        "sku": payload.sku or f"SKU-{new_id[:8].upper()}",
        "tags": payload.tags,
        "rating_avg": 5.0,
        "review_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    with _db_lock:
        conn = _get_conn()
        try:
            _insert_seed_product(conn, record)
            conn.commit()
        finally:
            conn.close()
    return record


@app.get(
    "/v1/products/{id}",
    response_model=Product,
    operation_id="get_product_by_id",
    tags=["products"],
    summary="Get product by ID",
)
def get_product_by_id(
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            return _get_product_or_404(conn, product_id)
        finally:
            conn.close()


@app.put(
    "/v1/products/{id}",
    response_model=Product,
    operation_id="update_product",
    tags=["products"],
    summary="Replace a product by ID",
)
def update_product(
    payload: ProductCreate,
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier to update",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_product_or_404(conn, product_id)
            now = _now_iso()
            updated = {
                "id": existing["id"],
                "name": payload.name,
                "price": round(float(payload.price), 2),
                "category": payload.category.value,
                "in_stock": False if payload.stock_quantity == 0 else payload.in_stock,
                "stock_quantity": payload.stock_quantity,
                "sku": payload.sku or existing.get("sku") or "SKU-AUDIO-001",
                "tags": payload.tags,
                "rating_avg": existing.get("rating_avg", 4.8),
                "review_count": existing.get("review_count", 1),
                "created_at": existing["created_at"],
                "updated_at": now,
            }
            _insert_seed_product(conn, updated)
            conn.commit()
            return updated
        finally:
            conn.close()


@app.patch(
    "/v1/products/{id}",
    response_model=Product,
    operation_id="patch_product",
    tags=["products"],
    summary="Partially update a product by ID",
)
def patch_product(
    payload: ProductPatch,
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier to partially update",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PATCH request body must include at least one field to update.",
        )
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_product_or_404(conn, product_id)
            for k, v in updates.items():
                if v is not None:
                    existing[k] = v.value if isinstance(v, Enum) else v
            if "stock_quantity" in updates and updates["stock_quantity"] is not None:
                if existing["stock_quantity"] == 0 and "in_stock" not in updates:
                    existing["in_stock"] = False
                elif existing["stock_quantity"] > 0 and "in_stock" not in updates:
                    existing["in_stock"] = True
            existing["price"] = round(float(existing["price"]), 2)
            existing["updated_at"] = _now_iso()
            _insert_seed_product(conn, existing)
            conn.commit()
            return existing
        finally:
            conn.close()


@app.delete(
    "/v1/products/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_product",
    tags=["products"],
    summary="Delete a product by ID",
)
def delete_product(
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier to delete",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            _get_product_or_404(conn, product_id)
            if product_id not in SEED_PRODUCTS:
                conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
                conn.commit()
        finally:
            conn.close()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/v1/products/{id}/restock",
    response_model=Product,
    operation_id="restock_product",
    tags=["products"],
    summary="Restock product inventory",
)
def restock_product(
    payload: ProductRestockRequest,
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier to restock",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_product_or_404(conn, product_id)
            existing["stock_quantity"] = int(existing.get("stock_quantity", 0)) + int(
                payload.quantity
            )
            existing["in_stock"] = True
            existing["updated_at"] = _now_iso()
            _insert_seed_product(conn, existing)
            conn.commit()
            return existing
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Product Reviews Sub-Resource & Standalone Endpoints (/v1/products/{id}/reviews, /v1/reviews/{id})
# ---------------------------------------------------------------------------
@app.get(
    "/v1/products/{id}/reviews",
    response_model=ReviewListResponse,
    operation_id="list_product_reviews",
    tags=["reviews"],
    summary="List reviews for a product",
)
def list_product_reviews(
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier whose reviews to list",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
    min_rating: Optional[int] = Query(
        default=None,
        ge=1,
        le=5,
        description="Optional minimum star rating filter (1 to 5)",
    ),
    verified_only: Optional[bool] = Query(
        default=None,
        description="If true, only return reviews from verified purchases",
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            product = _get_product_or_404(conn, product_id)
            query = "SELECT * FROM reviews WHERE product_id = ?"
            params: List[Any] = [product["id"]]
            if min_rating is not None:
                query += " AND rating >= ?"
                params.append(min_rating)
            if verified_only is True:
                query += " AND verified_purchase = 1"
            rows = conn.execute(query, params).fetchall()
            items = [_row_to_review(r) for r in rows]
            avg = (
                round(sum(r["rating"] for r in items) / len(items), 2)
                if items
                else float(product.get("rating_avg", 0.0))
            )
            return {
                "product_id": product["id"],
                "items": items,
                "total": len(items),
                "average_rating": avg,
            }
        finally:
            conn.close()


@app.post(
    "/v1/products/{id}/reviews",
    response_model=Review,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_product_review",
    tags=["reviews"],
    summary="Create a product review",
)
def create_product_review(
    payload: ReviewCreate,
    product_id: str = Path(
        ...,
        alias="id",
        description="Unique product identifier being reviewed",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            product = _get_product_or_404(conn, product_id)
            new_id = str(uuid.uuid4())
            now = _now_iso()
            record = {
                "id": new_id,
                "product_id": product["id"],
                "reviewer_email": payload.reviewer_email,
                "rating": payload.rating,
                "title": payload.title,
                "comment": payload.comment,
                "verified_purchase": payload.verified_purchase,
                "created_at": now,
                "updated_at": now,
            }
            _insert_seed_review(conn, record)
            _recalculate_product_rating(conn, product["id"])
            conn.commit()
            return record
        finally:
            conn.close()


@app.get(
    "/v1/reviews/{id}",
    response_model=Review,
    operation_id="get_review_by_id",
    tags=["reviews"],
    summary="Get review by ID",
)
def get_review_by_id(
    review_id: str = Path(
        ...,
        alias="id",
        description="Unique review identifier",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            return _get_review_or_404(conn, review_id)
        finally:
            conn.close()


@app.patch(
    "/v1/reviews/{id}",
    response_model=Review,
    operation_id="patch_review",
    tags=["reviews"],
    summary="Update a product review by ID",
)
def patch_review(
    payload: ReviewPatch,
    review_id: str = Path(
        ...,
        alias="id",
        description="Unique review identifier to update",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PATCH request body must include at least one field (rating, title, or comment).",
        )
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_review_or_404(conn, review_id)
            for k, v in updates.items():
                if v is not None:
                    existing[k] = v
            existing["updated_at"] = _now_iso()
            _insert_seed_review(conn, existing)
            _recalculate_product_rating(conn, existing["product_id"])
            conn.commit()
            return existing
        finally:
            conn.close()


@app.delete(
    "/v1/reviews/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_review",
    tags=["reviews"],
    summary="Delete a review by ID",
)
def delete_review(
    review_id: str = Path(
        ...,
        alias="id",
        description="Unique review identifier to delete",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_review_or_404(conn, review_id)
            if review_id not in SEED_REVIEWS:
                conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
                _recalculate_product_rating(conn, existing["product_id"])
                conn.commit()
        finally:
            conn.close()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Customers CRUD & Sub-Resource Endpoints (/v1/customers)
# ---------------------------------------------------------------------------
@app.get(
    "/v1/customers",
    response_model=CustomerListResponse,
    operation_id="list_customers",
    tags=["customers"],
    summary="List all customers",
)
def list_customers(
    tier: Optional[CustomerTier] = Query(
        default=None,
        description="Filter customers by loyalty tier (STANDARD, SILVER, GOLD, PLATINUM)",
    ),
    active: Optional[bool] = Query(
        default=None,
        description="Filter by active account status",
    ),
    min_loyalty_points: Optional[int] = Query(
        default=None,
        ge=0,
        le=1000000,
        description="Minimum loyalty points filter",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of customers to return (1 to 100)",
    ),
):
    query = "SELECT * FROM customers WHERE 1=1"
    params: List[Any] = []
    if tier is not None:
        query += " AND tier = ?"
        params.append(tier.value)
    if active is not None:
        query += " AND active = ?"
        params.append(1 if active else 0)
    if min_loyalty_points is not None:
        query += " AND loyalty_points >= ?"
        params.append(min_loyalty_points)
    query += " LIMIT ?"
    params.append(limit)

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(query, params).fetchall()
            items = [_row_to_customer(r) for r in rows]
        finally:
            conn.close()
    return {"items": items, "total": len(items)}


@app.post(
    "/v1/customers",
    response_model=Customer,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_customer",
    tags=["customers"],
    summary="Create a new customer",
)
def create_customer(payload: CustomerCreate):
    new_id = str(uuid.uuid4())
    now = _now_iso()
    record = {
        "id": new_id,
        "email": payload.email,
        "full_name": payload.full_name,
        "tier": payload.tier.value,
        "loyalty_points": payload.loyalty_points,
        "active": payload.active,
        "shipping_address": payload.shipping_address.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    with _db_lock:
        conn = _get_conn()
        try:
            _insert_seed_customer(conn, record)
            conn.commit()
        finally:
            conn.close()
    return record


@app.get(
    "/v1/customers/{id}",
    response_model=Customer,
    operation_id="get_customer_by_id",
    tags=["customers"],
    summary="Get customer by ID",
)
def get_customer_by_id(
    customer_id: str = Path(
        ...,
        alias="id",
        description="Unique customer identifier",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            return _get_customer_or_404(conn, customer_id)
        finally:
            conn.close()


@app.put(
    "/v1/customers/{id}",
    response_model=Customer,
    operation_id="update_customer",
    tags=["customers"],
    summary="Replace a customer by ID",
)
def update_customer(
    payload: CustomerCreate,
    customer_id: str = Path(
        ...,
        alias="id",
        description="Unique customer identifier to update",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_customer_or_404(conn, customer_id)
            now = _now_iso()
            updated = {
                "id": existing["id"],
                "email": payload.email,
                "full_name": payload.full_name,
                "tier": payload.tier.value,
                "loyalty_points": payload.loyalty_points,
                "active": payload.active,
                "shipping_address": payload.shipping_address.model_dump(),
                "created_at": existing["created_at"],
                "updated_at": now,
            }
            _insert_seed_customer(conn, updated)
            conn.commit()
            return updated
        finally:
            conn.close()


@app.patch(
    "/v1/customers/{id}",
    response_model=Customer,
    operation_id="patch_customer",
    tags=["customers"],
    summary="Partially update a customer by ID",
)
def patch_customer(
    payload: CustomerPatch,
    customer_id: str = Path(
        ...,
        alias="id",
        description="Unique customer identifier to patch",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PATCH request body must include at least one customer field to update.",
        )
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_customer_or_404(conn, customer_id)
            for k, v in updates.items():
                if v is not None:
                    existing[k] = v.value if isinstance(v, Enum) else v
            existing["updated_at"] = _now_iso()
            _insert_seed_customer(conn, existing)
            conn.commit()
            return existing
        finally:
            conn.close()


@app.delete(
    "/v1/customers/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_customer",
    tags=["customers"],
    summary="Delete a customer by ID",
)
def delete_customer(
    customer_id: str = Path(
        ...,
        alias="id",
        description="Unique customer identifier to delete",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            _get_customer_or_404(conn, customer_id)
            if customer_id not in SEED_CUSTOMERS:
                conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
                conn.commit()
        finally:
            conn.close()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/v1/customers/{id}/orders",
    response_model=OrderListResponse,
    operation_id="list_customer_orders",
    tags=["customers"],
    summary="List orders for a specific customer",
)
def list_customer_orders(
    customer_id: str = Path(
        ...,
        alias="id",
        description="Unique customer identifier whose orders to retrieve",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
    status_filter: Optional[OrderStatus] = Query(
        default=None,
        alias="status",
        description="Optional order status filter",
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            customer = _get_customer_or_404(conn, customer_id)
            query = "SELECT * FROM orders WHERE (customer_id = ? OR customer_email = ?)"
            params: List[Any] = [customer["id"], customer["email"]]
            if status_filter is not None:
                query += " AND status = ?"
                params.append(status_filter.value)
            rows = conn.execute(query, params).fetchall()
            items = [_row_to_order(r) for r in rows]
            return {"items": items, "total": len(items)}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Promotions & Discount Codes Endpoints (/v1/promotions)
# ---------------------------------------------------------------------------
@app.get(
    "/v1/promotions",
    response_model=PromotionListResponse,
    operation_id="list_promotions",
    tags=["promotions"],
    summary="List all promotions",
)
def list_promotions(
    active: Optional[bool] = Query(
        default=None,
        description="Filter promotions by active status",
    ),
    discount_type: Optional[DiscountType] = Query(
        default=None,
        description="Filter by discount type (PERCENTAGE or FIXED_AMOUNT)",
    ),
):
    query = "SELECT * FROM promotions WHERE 1=1"
    params: List[Any] = []
    if active is not None:
        query += " AND active = ?"
        params.append(1 if active else 0)
    if discount_type is not None:
        query += " AND discount_type = ?"
        params.append(discount_type.value)

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(query, params).fetchall()
            items = [_row_to_promotion(r) for r in rows]
        finally:
            conn.close()
    return {"items": items, "total": len(items)}


@app.post(
    "/v1/promotions",
    response_model=Promotion,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_promotion",
    tags=["promotions"],
    summary="Create a new promotion",
)
def create_promotion(payload: PromotionCreate):
    new_id = str(uuid.uuid4())
    now = _now_iso()
    record = {
        "id": new_id,
        "code": payload.code,
        "description": payload.description,
        "discount_type": payload.discount_type.value,
        "discount_value": round(float(payload.discount_value), 2),
        "min_order_amount": round(float(payload.min_order_amount), 2),
        "active": payload.active,
        "created_at": now,
        "updated_at": now,
    }
    with _db_lock:
        conn = _get_conn()
        try:
            _insert_seed_promotion(conn, record)
            conn.commit()
        finally:
            conn.close()
    return record


@app.get(
    "/v1/promotions/{id}",
    response_model=Promotion,
    operation_id="get_promotion_by_id",
    tags=["promotions"],
    summary="Get promotion by ID or code",
)
def get_promotion_by_id(
    promotion_id: str = Path(
        ...,
        alias="id",
        description="Unique promotion identifier or promotional code",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            return _get_promotion_or_404(conn, promotion_id)
        finally:
            conn.close()


@app.patch(
    "/v1/promotions/{id}",
    response_model=Promotion,
    operation_id="patch_promotion",
    tags=["promotions"],
    summary="Partially update a promotion by ID",
)
def patch_promotion(
    payload: PromotionPatch,
    promotion_id: str = Path(
        ...,
        alias="id",
        description="Unique promotion identifier to update",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    ),
):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PATCH request body must include at least one promotion field to update.",
        )
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_promotion_or_404(conn, promotion_id)
            for k, v in updates.items():
                if v is not None:
                    existing[k] = v
            if (
                existing["discount_type"] == DiscountType.PERCENTAGE.value
                and float(existing["discount_value"]) > 90.0
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Percentage discount_value cannot exceed 90.0%.",
                )
            existing["discount_value"] = round(float(existing["discount_value"]), 2)
            existing["min_order_amount"] = round(float(existing["min_order_amount"]), 2)
            existing["updated_at"] = _now_iso()
            _insert_seed_promotion(conn, existing)
            conn.commit()
            return existing
        finally:
            conn.close()


@app.delete(
    "/v1/promotions/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_promotion",
    tags=["promotions"],
    summary="Delete a promotion by ID",
)
def delete_promotion(
    promotion_id: str = Path(
        ...,
        alias="id",
        description="Unique promotion identifier to delete",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_promotion_or_404(conn, promotion_id)
            if existing["id"] not in SEED_PROMOTIONS:
                conn.execute("DELETE FROM promotions WHERE id = ?", (existing["id"],))
                conn.commit()
        finally:
            conn.close()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Orders CRUD & Lifecycle Action Endpoints (/v1/orders)
# ---------------------------------------------------------------------------
@app.get(
    "/v1/orders",
    response_model=OrderListResponse,
    operation_id="list_orders",
    tags=["orders"],
    summary="List all orders",
)
def list_orders(
    status_filter: Optional[OrderStatus] = Query(
        default=None,
        alias="status",
        description="Filter orders by status (PENDING, CONFIRMED, SHIPPED, DELIVERED, CANCELLED)",
    ),
    customer_email: Optional[str] = Query(
        default=None,
        description="Filter orders by customer email address",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of orders to return (1 to 100)",
    ),
):
    query = "SELECT * FROM orders WHERE 1=1"
    params: List[Any] = []
    if status_filter is not None:
        query += " AND status = ?"
        params.append(status_filter.value)
    if customer_email is not None:
        query += " AND customer_email = ?"
        params.append(customer_email)
    query += " LIMIT ?"
    params.append(limit)

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(query, params).fetchall()
            items = [_row_to_order(r) for r in rows]
        finally:
            conn.close()
    return {"items": items, "total": len(items)}


@app.post(
    "/v1/orders",
    response_model=Order,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_order",
    tags=["orders"],
    summary="Create a new order",
)
def create_order(payload: OrderCreate):
    with _db_lock:
        conn = _get_conn()
        try:
            p_row = conn.execute(
                "SELECT * FROM products WHERE id = ?", (payload.product_id,)
            ).fetchone()
            if p_row:
                unit_price = float(p_row["price"])
            elif payload.product_id in SEED_PRODUCTS:
                unit_price = float(SEED_PRODUCTS[payload.product_id]["price"])
            else:
                unit_price = 99.99

            subtotal = round(unit_price * payload.quantity, 2)
            discount = 0.0
            applied_code: Optional[str] = None
            if payload.promo_code:
                promo = _get_promotion_or_404(conn, payload.promo_code)
                discount = _calculate_discount(subtotal, promo)
                applied_code = promo["code"]

            new_id = str(uuid.uuid4())
            now = _now_iso()
            record = {
                "id": new_id,
                "customer_email": payload.customer_email,
                "customer_id": payload.customer_id,
                "product_id": payload.product_id,
                "quantity": payload.quantity,
                "status": OrderStatus.PENDING.value,
                "subtotal_price": subtotal,
                "discount_amount": discount,
                "promo_code": applied_code,
                "total_price": round(max(0.0, subtotal - discount), 2),
                "notes": payload.notes,
                "created_at": now,
                "updated_at": now,
            }
            _insert_seed_order(conn, record)
            conn.commit()
            return record
        finally:
            conn.close()


@app.get(
    "/v1/orders/{id}",
    response_model=Order,
    operation_id="get_order_by_id",
    tags=["orders"],
    summary="Get order by ID",
)
def get_order_by_id(
    order_id: str = Path(
        ...,
        alias="id",
        description="Unique order identifier",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            return _get_order_or_404(conn, order_id)
        finally:
            conn.close()


@app.put(
    "/v1/orders/{id}",
    response_model=Order,
    operation_id="update_order",
    tags=["orders"],
    summary="Replace an order by ID",
)
def update_order(
    payload: OrderCreate,
    order_id: str = Path(
        ...,
        alias="id",
        description="Unique order identifier to update",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_order_or_404(conn, order_id)
            p_row = conn.execute(
                "SELECT price FROM products WHERE id = ?", (payload.product_id,)
            ).fetchone()
            unit_price = float(p_row["price"]) if p_row else 99.99
            subtotal = round(unit_price * payload.quantity, 2)
            discount = 0.0
            applied_code: Optional[str] = None
            if payload.promo_code:
                promo = _get_promotion_or_404(conn, payload.promo_code)
                discount = _calculate_discount(subtotal, promo)
                applied_code = promo["code"]

            now = _now_iso()
            updated = {
                "id": existing["id"],
                "customer_email": payload.customer_email,
                "customer_id": payload.customer_id or existing.get("customer_id"),
                "product_id": payload.product_id,
                "quantity": payload.quantity,
                "status": existing.get("status", OrderStatus.CONFIRMED.value),
                "subtotal_price": subtotal,
                "discount_amount": discount,
                "promo_code": applied_code,
                "total_price": round(max(0.0, subtotal - discount), 2),
                "notes": payload.notes,
                "created_at": existing["created_at"],
                "updated_at": now,
            }
            _insert_seed_order(conn, updated)
            conn.commit()
            return updated
        finally:
            conn.close()


@app.patch(
    "/v1/orders/{id}",
    response_model=Order,
    operation_id="patch_order",
    tags=["orders"],
    summary="Update order status or quantity",
)
def patch_order(
    payload: OrderPatch,
    order_id: str = Path(
        ...,
        alias="id",
        description="Unique order identifier to patch",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    ),
):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PATCH request body must include at least one field (status, quantity, or notes).",
        )
    with _db_lock:
        conn = _get_conn()
        try:
            existing = _get_order_or_404(conn, order_id)
            if "status" in updates and updates["status"] is not None:
                new_status = (
                    updates["status"].value
                    if isinstance(updates["status"], Enum)
                    else updates["status"]
                )
                if existing["status"] == OrderStatus.CANCELLED.value and new_status in {
                    OrderStatus.SHIPPED.value,
                    OrderStatus.DELIVERED.value,
                }:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot transition a CANCELLED order to {new_status}.",
                    )
                existing["status"] = new_status
            if "quantity" in updates and updates["quantity"] is not None:
                existing["quantity"] = int(updates["quantity"])
                subtotal = round(99.99 * int(updates["quantity"]), 2)
                existing["subtotal_price"] = subtotal
                existing["total_price"] = round(
                    max(0.0, subtotal - float(existing.get("discount_amount", 0.0))), 2
                )
            if "notes" in updates and updates["notes"] is not None:
                existing["notes"] = updates["notes"]
            existing["updated_at"] = _now_iso()
            _insert_seed_order(conn, existing)
            conn.commit()
            return existing
        finally:
            conn.close()


@app.delete(
    "/v1/orders/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_order",
    tags=["orders"],
    summary="Delete an order by ID",
)
def delete_order(
    order_id: str = Path(
        ...,
        alias="id",
        description="Unique order identifier to delete",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    )
):
    with _db_lock:
        conn = _get_conn()
        try:
            _get_order_or_404(conn, order_id)
            if order_id not in SEED_ORDERS:
                conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
                conn.commit()
        finally:
            conn.close()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/v1/orders/{id}/apply-promotion",
    response_model=Order,
    operation_id="apply_order_promotion",
    tags=["orders"],
    summary="Apply a promotional discount code to an order",
)
def apply_order_promotion(
    payload: ApplyPromotionRequest,
    order_id: str = Path(
        ...,
        alias="id",
        description="Unique order identifier to apply promotion to",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            order = _get_order_or_404(conn, order_id)
            if order["status"] in {
                OrderStatus.CANCELLED.value,
                OrderStatus.DELIVERED.value,
            }:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot apply a promotion to an order in '{order['status']}' status.",
                )
            promo = _get_promotion_or_404(conn, payload.promo_code)
            subtotal = float(order.get("subtotal_price") or order["total_price"])
            discount = _calculate_discount(subtotal, promo)
            order["subtotal_price"] = subtotal
            order["discount_amount"] = discount
            order["promo_code"] = promo["code"]
            order["total_price"] = round(max(0.0, subtotal - discount), 2)
            order["updated_at"] = _now_iso()
            _insert_seed_order(conn, order)
            conn.commit()
            return order
        finally:
            conn.close()


@app.post(
    "/v1/orders/{id}/cancel",
    response_model=Order,
    operation_id="cancel_order",
    tags=["orders"],
    summary="Cancel an order and optionally restock inventory",
)
def cancel_order(
    payload: OrderCancelRequest,
    order_id: str = Path(
        ...,
        alias="id",
        description="Unique order identifier to cancel",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    ),
):
    with _db_lock:
        conn = _get_conn()
        try:
            order = _get_order_or_404(conn, order_id)
            if order["status"] == OrderStatus.DELIVERED.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot cancel an order that has already been DELIVERED.",
                )
            order["status"] = OrderStatus.CANCELLED.value
            base_notes = order.get("notes") or ""
            cancel_note = f"Cancelled: {payload.reason}"
            order["notes"] = (
                f"{base_notes} | {cancel_note}".strip(" |") if base_notes else cancel_note
            )
            order["updated_at"] = _now_iso()
            if order_id not in SEED_ORDERS:
                _insert_seed_order(conn, order)

            if payload.restock_items and order.get("product_id"):
                p_row = conn.execute(
                    "SELECT * FROM products WHERE id = ?", (order["product_id"],)
                ).fetchone()
                if p_row:
                    prod = _row_to_product(p_row)
                    prod["stock_quantity"] = int(prod.get("stock_quantity", 0)) + int(
                        order["quantity"]
                    )
                    prod["in_stock"] = True
                    prod["updated_at"] = _now_iso()
                    _insert_seed_product(conn, prod)

            conn.commit()
            return order
        finally:
            conn.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port)
