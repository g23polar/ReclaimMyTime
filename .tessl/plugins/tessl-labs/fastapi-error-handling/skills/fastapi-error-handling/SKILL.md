---
name: fastapi-error-handling
description: >-
  Production error handling for FastAPI — exception handlers, structured error
  responses, async error patterns, and graceful shutdown. Use when building or
  reviewing FastAPI backends, when you see unhandled exceptions returning raw
  500s, or when error response formats are inconsistent.
keywords: fastapi error handling, exception handler, HTTPException, structured
  errors, validation error, pydantic, async errors, graceful shutdown, starlette,
  error response format, fastapi middleware, python api errors
license: MIT
---

# FastAPI Error Handling

Production error handling for FastAPI applications. Covers exception handlers, structured responses, validation errors, and shutdown.

---

## The Problem

FastAPI auto-handles Pydantic validation and returns 422, but most apps need more:
- Custom error format (not FastAPI's default `{"detail": ...}`)
- Business logic errors (order not found, invalid status transition)
- Database errors translated to user-facing messages
- Graceful shutdown on SIGTERM

---

## Quick Start — Add to an Existing App

### 1. Define error classes

```python
# app/errors.py
from fastapi import HTTPException

class AppError(HTTPException):
    def __init__(self, status_code: int, message: str, code: str = "ERROR"):
        self.code = code
        super().__init__(status_code=status_code, detail=message)

class NotFoundError(AppError):
    def __init__(self, resource: str, id: str | None = None):
        msg = f"{resource} {id} not found" if id else f"{resource} not found"
        super().__init__(404, msg, "NOT_FOUND")

class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__(400, message, "VALIDATION_ERROR")
```

### 2. Register all three exception handlers

Every FastAPI app MUST register these three handlers. Without them, errors leak internal details or use inconsistent formats.

```python
# app/main.py
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from app.errors import AppError
import logging

logger = logging.getLogger(__name__)

app = FastAPI()

# Handler 1: Custom app errors → structured response
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.detail}},
    )

# Handler 2: Pydantic validation errors → field-level details
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = [
        {"field": ".".join(str(l) for l in e["loc"][1:]), "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "details": errors}},
    )

# Handler 3: Catch-all for unexpected errors → safe generic message, log real error
@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
    )
```

**Why all three?**
- Without Handler 1: Custom exceptions fall through to the catch-all, losing specific error codes
- Without Handler 2: Clients get FastAPI's raw 422 format which is hard to parse and inconsistent with your other errors
- Without Handler 3: Unexpected errors return raw stack traces in development or opaque 500s in production

### 3. Use in routes

```python
from app.errors import NotFoundError, ValidationError

@app.post("/api/orders", status_code=201)
async def create_order(body: CreateOrderRequest):
    if not body.items:
        raise ValidationError("At least one item is required")

    order = db.create_order(body)
    return {"data": order}

@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    order = db.get_order(order_id)
    if not order:
        raise NotFoundError("Order", str(order_id))
    return {"data": order}
```

### 4. Graceful shutdown

Use the `lifespan` async context manager (FastAPI 0.93+). The older `@app.on_event("startup"|"shutdown")` decorators are deprecated.

```python
import signal
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: open pools, warm caches, start background tasks
    yield
    # Shutdown: close database connections, cancel background tasks
    db.close()

app = FastAPI(lifespan=lifespan)

# For uvicorn, SIGTERM handling is built in
# For custom runners:
def handle_sigterm(signum, frame):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
```

Code after `yield` runs on shutdown and is awaited before the process exits, giving in-flight requests time to finish. Exceptions raised before `yield` abort startup; exceptions after `yield` are logged but do not block shutdown.

---

## Error Response Format

All errors return:
```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Order 42 not found"
  }
}
```

Validation errors include field details:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request",
    "details": [
      {"field": "customer_name", "message": "field required"},
      {"field": "items", "message": "ensure this value has at least 1 items"}
    ]
  }
}
```

---

## Applying to Existing Code

1. Search for bare `raise HTTPException` — replace with typed errors
2. Search for routes without error handling — add try/except for database operations
3. Check if `@app.exception_handler` exists — if not, add the three handlers above
4. Check error response format consistency across routes

---

## Checklist

- [ ] Exception handlers registered for AppError, RequestValidationError, and generic Exception
- [ ] Consistent error response format across all routes
- [ ] No stack traces or internal details leaked to clients
- [ ] Database errors caught and translated to user-facing messages
- [ ] Graceful shutdown handler registered

## Verifiers

- [payment-api](../../verifiers/payment-api.json) — Error handling in payment/financial APIs
- [user-registration-api](../../verifiers/user-registration-api.json) — Error handling in user management APIs
- [inventory-api](../../verifiers/inventory-api.json) — Error handling in inventory/CRUD APIs
- [booking-api](../../verifiers/booking-api.json) — Error handling in booking/scheduling APIs
- [order-api](../../verifiers/order-api.json) — Error handling in order/workflow APIs
