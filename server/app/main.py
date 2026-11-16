from fastapi import FastAPI

from app import api
from app.agent.router import router as agent_router
from app.auth.errors import register_error_handlers
from app.auth.router import router as auth_router
from app.log_redaction import install_access_log_redaction

# OAuth code/state must never reach the uvicorn access log (T4.1 review round 2).
install_access_log_redaction()

app = FastAPI()
register_error_handlers(app)
app.include_router(auth_router)
api.install(app)
app.include_router(agent_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
