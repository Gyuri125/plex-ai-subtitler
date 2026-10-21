"""
CLI entry point for running the Web Dashboard standalone.
Usage: python -m plex_subtitler.web
"""
from .server import run_web_dashboard
from plex_subtitler.config import app1_config

if __name__ == "__main__":
    run_web_dashboard(
        host=app1_config.web_dashboard_host,
        port=app1_config.web_dashboard_port
    )
