"""The FastAPI app: booking routes, and the MCP server mounted at /mcp.

One command runs everything (from the tt_mcp folder):
    uvicorn app.api.main:create_app --factory --reload

    http://127.0.0.1:8000/docs   try the booking routes
    http://127.0.0.1:8000/mcp    the MCP server (for MCP Inspector, or the LLM in step 7)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings

from .. import server as mcp_server
from ..availability import load_timetable
from ..db.session import init_db
from ..directory import FacultyDirectory
from .appointments import held_slots, router as appointments_router
from .config import Settings
from .context import ctx
from .mailer import make_mailer


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = settings or Settings.from_env()

    ctx.settings = s
    ctx.tt = load_timetable(s.timetable_file)
    ctx.sessions = init_db(s.database_url)
    ctx.directory = FacultyDirectory.load(s.faculty_directory)
    ctx.mailer = make_mailer(s)

    # The MCP server uses the same timetable copy, and is served from this app.
    # It also sees booked appointments, so the chat never offers a taken slot.
    mcp_server.use_timetable(ctx.tt)

    def booked(faculty_id, d):
        with ctx.sessions() as db:
            return [(start, end) for start, end, _ in held_slots(db, faculty_id, d)]

    mcp_server.use_bookings(booked)
    mcp_app = mcp_server.mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=s.mcp_allowed_hosts,
            allowed_origins=[f"http://{h}" for h in s.mcp_allowed_hosts]
                            + [f"https://{h}" for h in s.mcp_allowed_hosts],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_server.mcp.session_manager.run():   # MCP needs this running
            yield

    app = FastAPI(title="Faculty appointments", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.include_router(appointments_router)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "faculty": len(ctx.tt.people), "emails_known": len(ctx.directory),
                "email_mode": s.email_mode, "mcp": "/mcp"}

    app.mount("/mcp", mcp_app)
    return _McpPathFix(app)


class _McpPathFix:
    """Serve '/mcp' directly instead of redirecting to '/mcp/'.

    Some MCP clients don't follow redirects on POST, so the redirect would
    look like a failed connection to them.
    """

    def __init__(self, app: FastAPI):
        self.app = app

    def __getattr__(self, name):              # behave like the FastAPI app (tests use .state etc.)
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)