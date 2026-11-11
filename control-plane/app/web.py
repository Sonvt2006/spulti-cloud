"""Serve the dashboard from the existing EC2 API image, behind Nginx."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles


def install_dashboard(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/dashboard/")

    @app.middleware("http")
    async def dashboard_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/dashboard", "/api/auth")):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/dashboard"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount(
        "/dashboard",
        StaticFiles(directory=Path(__file__).with_name("static"), html=True),
        name="dashboard",
    )
