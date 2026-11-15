---
name: sqlalchemy-best-practices
description: >-
  SQLAlchemy patterns — engine setup, session management, declarative models,
  relationships, eager loading, raw SQL safety, async patterns, bulk operations,
  and Alembic migrations. Use when building or reviewing Python apps with
  SQLAlchemy, when setting up a new database layer, or when debugging session,
  connection, or performance issues.
keywords: sqlalchemy, sqlalchemy model, sqlalchemy session, sqlalchemy engine,
  alembic, sqlalchemy migration, sqlalchemy relationship, sqlalchemy query,
  sqlalchemy async, sqlalchemy orm, sqlalchemy declarative, sqlalchemy
  connection pool, sqlalchemy best practices, n+1, eager loading, bulk insert
license: MIT
---

# SQLAlchemy Best Practices (2.0+)

Patterns ordered by impact. Every section shows WRONG vs RIGHT code.

---

## 1. Engine & Connection Pool Configuration

Production engines MUST configure pool parameters. Without them, defaults (pool_size=5, max_overflow=10) cause **QueuePool limit reached** errors under load.

### WRONG — bare create_engine with no pool settings
```python
engine = create_engine(os.getenv("DATABASE_URL"))
```

### RIGHT — production-ready engine
```python
from sqlalchemy import create_engine
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")

engine = create_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "false").lower() == "true",
    pool_pre_ping=True,        # Verify connections before use (avoids stale connection errors)
    pool_size=10,              # Persistent connections in pool
    max_overflow=20,           # Temporary connections above pool_size under burst load
    pool_recycle=1800,         # Recycle connections every 30 min (prevents MySQL "gone away")
)
```

Key parameters:
- `pool_pre_ping=True` — always enable; detects dead connections before use
- `pool_size=10` — tune to match expected concurrency (default 5 is too low for most APIs)
- `max_overflow=20` — extra connections for burst traffic
- `pool_recycle=1800` — MySQL/MariaDB close idle connections after `wait_timeout`; recycle before that

---

## 2. Session Management

### WRONG — session not closed on error
```python
def get_db():
    db = SessionLocal()
    return db  # Leaked if caller forgets to close!
```

### WRONG — using scoped_session incorrectly
```python
session = scoped_session(sessionmaker(bind=engine))
# ... use session ...
session.close()  # WRONG: close() doesn't remove the registry entry
```

### RIGHT — yield + finally for web frameworks
```python
from sqlalchemy.orm import sessionmaker, DeclarativeBase

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    """Session dependency — use with FastAPI Depends() or as context manager."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

### RIGHT — scoped_session for Flask/threaded apps
```python
from sqlalchemy.orm import scoped_session, sessionmaker

db_session = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))

# At request teardown, use remove() NOT close()
@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()  # Removes session from registry AND closes it
```

Critical rules:
- **Always** set `expire_on_commit=False` — without it, accessing attributes after `commit()` triggers lazy loads or raises `DetachedInstanceError`
- Use `session.remove()` (not `session.close()`) with `scoped_session`
- The session IS the transaction: `db.commit()` commits, `db.rollback()` rolls back — never use raw `BEGIN`/`COMMIT`

---

## 3. Models — Declarative 2.0 Style

### WRONG — legacy Column() style
```python
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)           # Legacy!
    name = Column(String(100))                        # No type hints
    posts = relationship("Post", backref="author")    # backref is implicit
```

### RIGHT — Mapped type annotations
```python
from sqlalchemy import String, Integer, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow
    )

    posts: Mapped[list["Post"]] = relationship(
        back_populates="author",
        cascade="all, delete-orphan",  # Delete orphaned children
    )

class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    author: Mapped["User"] = relationship(back_populates="posts")
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_posts_status_created", "status", "created_at"),
    )

class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    post: Mapped["Post"] = relationship(back_populates="comments")
```

Key rules:
- Use `Mapped[type]` + `mapped_column()` for all columns (SQLAlchemy 2.0)
- `index=True` on every foreign key and every frequently-filtered column
- `cascade="all, delete-orphan"` on the parent side of one-to-many relationships
- `ondelete="CASCADE"` on the ForeignKey itself (database-level cascade)
- `back_populates` (explicit) over `backref` (implicit)
- `onupdate=datetime.utcnow` for automatic updated_at tracking
- `Mapped[list["Child"]]` for one-to-many relationship typing

---

## 4. Queries — 2.0 Style with Eager Loading

### WRONG — legacy query() API with N+1
```python
# Legacy style — avoid
orders = db.query(Order).filter(Order.status == "pending").all()
for order in orders:
    for item in order.items:  # N+1: triggers a query PER order!
        print(item.product.name)  # N+1 again: query PER item!
```

### RIGHT — select() with chained eager loading
```python
from sqlalchemy import select
from sqlalchemy.orm import selectinload

stmt = (
    select(Order)
    .options(
        selectinload(Order.items).selectinload(OrderItem.product)
    )
    .where(Order.status == "pending")
    .order_by(Order.created_at)
)
orders = db.execute(stmt).scalars().all()
# All items and products are pre-loaded — no extra queries
```

### When to use which loading strategy:
- **`selectinload`** — default choice; uses `SELECT ... WHERE id IN (...)` — works well for most relationships
- **`joinedload`** — uses a JOIN; best for many-to-one or one-to-one; avoid for large one-to-many (duplicates parent rows)
- **`subqueryload`** — uses a subquery; **use instead of selectinload when loading >1000 parent rows**, because `selectinload` generates `WHERE id IN (...)` with all parent IDs as parameters, which exceeds database parameter limits on many backends

### Example: subqueryload for large result sets
```python
from sqlalchemy.orm import subqueryload, joinedload

# When loading thousands of articles with their authors:
# - Use subqueryload (not selectinload) because the article list may exceed 1000 IDs
# - Use joinedload for many-to-one (article -> author) since it's a simple JOIN
stmt = (
    select(Article)
    .options(
        joinedload(Article.author),           # many-to-one: JOIN is efficient
        subqueryload(Article.comments),       # one-to-many with large parent set: use subquery
    )
    .where(Article.id.in_(article_ids))       # article_ids may have >1000 entries
)
```

### Transaction pattern for multi-record inserts
```python
def create_order(db, customer_id: int, items: list[dict]) -> Order:
    order = Order(customer_id=customer_id, total_cents=0)
    db.add(order)
    db.flush()  # Get order.id without committing

    total = 0
    for item in items:
        line = OrderItem(
            order_id=order.id,
            product_id=item["product_id"],
            quantity=item["quantity"],
            unit_price_cents=item["unit_price_cents"],
        )
        total += item["quantity"] * item["unit_price_cents"]
        db.add(line)

    order.total_cents = total
    db.commit()
    db.refresh(order)  # Reload generated values (timestamps, etc.)
    return order
```

---

## 5. Raw SQL Safety — Never Use f-strings

### WRONG — SQL injection vulnerability
```python
# NEVER do this — SQL injection via user input!
db.execute(text(f"SELECT * FROM users WHERE email = '{email}'"))
db.execute(text(f"DELETE FROM orders WHERE id = {order_id}"))
```

### RIGHT — bound parameters
```python
from sqlalchemy import text

# Use :param_name for bound parameters
result = db.execute(
    text("SELECT * FROM users WHERE email = :email"),
    {"email": email}
)

# For dynamic queries, prefer the ORM
stmt = select(User).where(User.email == email)
result = db.execute(stmt).scalar_one_or_none()
```

Always use `text()` with `:named_params` for raw SQL. Prefer ORM constructs (`select()`, `insert()`, `update()`) over raw SQL when possible.

---

## 6. Bulk Operations — Performance at Scale

### WRONG — slow loop with individual adds
```python
# Slow: issues N individual INSERT statements
for row in data:
    db.add(MyModel(**row))
db.commit()
```

### RIGHT — bulk insert with Core insert()
```python
from sqlalchemy import insert

# Fast: single INSERT with multiple value sets
db.execute(
    insert(MyModel),
    [{"name": row["name"], "value": row["value"]} for row in data]
)
db.commit()
```

For 1000+ rows, `insert().values()` with executemany is 5-10x faster than individual `session.add()` calls.

---

## 7. Async SQLAlchemy (2.0)

### WRONG — using sync engine in async code
```python
# Blocks the event loop!
engine = create_engine("postgresql://...")
```

### RIGHT — async engine and session
```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Note: use async driver (asyncpg, aiosqlite)
async_engine = create_async_engine(
    "postgresql+asyncpg://user:pass@localhost/db",
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)

async def get_async_db():
    async with AsyncSessionLocal() as session:
        yield session
```

---

## 8. Alembic Migrations

```bash
pip install alembic
alembic init alembic
# In alembic/env.py, set: target_metadata = Base.metadata
alembic revision --autogenerate -m "add users table"
alembic upgrade head
```

Key rules:
- **Always review autogenerated migrations** — autogenerate misses: table/column renames (generates drop+create instead), changes to constraints/indexes on some backends, Enum type changes
- Use `alembic upgrade head` on deployment
- Commit migration files to git
- For **SQLite** migrations requiring ALTER TABLE, use `batch_alter_table`:
```python
with op.batch_alter_table("users") as batch_op:
    batch_op.add_column(sa.Column("bio", sa.String(500)))
```

---

## 9. Error Handling — IntegrityError

### WRONG — no handling for unique constraint violations
```python
user = User(email=email)
db.add(user)
db.commit()  # Crashes with IntegrityError if email exists!
```

### RIGHT — catch and handle gracefully
```python
from sqlalchemy.exc import IntegrityError

try:
    user = User(email=email)
    db.add(user)
    db.commit()
except IntegrityError:
    db.rollback()  # MUST rollback before reusing session
    raise ValueError(f"Email {email} already exists")
```

Always `rollback()` after an `IntegrityError` — the session is in a broken state until you do.

---

## References

- [SQLAlchemy 2.0 Migration Guide](https://docs.sqlalchemy.org/en/20/changelog/migration_20.html)
- [Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [Relationship Loading Techniques](https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html)
- [Engine Configuration](https://docs.sqlalchemy.org/en/20/core/engines.html)
- [Async Extension](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [Alembic Tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)

---

## Checklist

- [ ] Engine created with `pool_pre_ping=True`
- [ ] Engine has `pool_size`, `max_overflow`, `pool_recycle` for production
- [ ] `DATABASE_URL` read from environment variable
- [ ] Session managed via dependency (yield + close in finally)
- [ ] `expire_on_commit=False` on sessionmaker
- [ ] `scoped_session` uses `remove()` not `close()` at teardown
- [ ] Models use `Mapped[type]` + `mapped_column()` (2.0 style)
- [ ] Base inherits from `DeclarativeBase` (not `declarative_base()`)
- [ ] `index=True` on foreign keys and frequently-filtered columns
- [ ] `cascade="all, delete-orphan"` on parent side of one-to-many
- [ ] `ondelete="CASCADE"` on ForeignKey definitions
- [ ] `back_populates` used (not `backref`)
- [ ] `onupdate=datetime.utcnow` on updated_at columns
- [ ] Queries use `select()` API (not legacy `db.query()`)
- [ ] `selectinload`/`joinedload` to prevent N+1 queries
- [ ] Raw SQL uses `text()` with `:bound_params` (never f-strings)
- [ ] `IntegrityError` caught with `db.rollback()` before reuse
- [ ] Bulk inserts use `insert()` with value lists for performance
- [ ] Alembic migrations reviewed and committed to git
- [ ] Async code uses `create_async_engine` + `async_sessionmaker`