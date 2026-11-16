"""Keep OAuth secrets out of the uvicorn access log.

`GET /gcal/callback?code=…&state=…` carries Google's authorization code and our encrypted
state in its query string, and uvicorn's default access log prints the full request
target. `CallbackQueryRedactor` strips the query from `uvicorn.access` records for that
path (method, path and status are kept). Installed from app.main at import, which is after
uvicorn has configured logging and before it serves, so a plain `uvicorn app.main:app`
launch is covered. dictConfig never clears logger filters, so the install sticks.
"""

from __future__ import annotations

import logging

ACCESS_LOGGER = "uvicorn.access"
REDACTED_PATHS = frozenset({"/gcal/callback"})


class CallbackQueryRedactor(logging.Filter):
    """uvicorn access records: args = (client_addr, method, full_path, http_version,
    status_code). Rewrites full_path to drop the query for REDACTED_PATHS."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            # endswith: also covers a deployment root_path prefix.
            if sep and any(path.endswith(p) for p in REDACTED_PATHS):
                record.args = (args[0], args[1], path, args[3], args[4])
        return True


def install_access_log_redaction() -> CallbackQueryRedactor:
    """Idempotent: at most one redactor on the uvicorn.access logger."""
    logger = logging.getLogger(ACCESS_LOGGER)
    for existing in logger.filters:
        if isinstance(existing, CallbackQueryRedactor):
            return existing
    redactor = CallbackQueryRedactor()
    logger.addFilter(redactor)
    return redactor
