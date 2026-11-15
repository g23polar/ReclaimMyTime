from fastapi import FastAPI

from app.auth.errors import register_error_handlers
from app.auth.router import router as auth_router

app = FastAPI()
register_error_handlers(app)
app.include_router(auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
