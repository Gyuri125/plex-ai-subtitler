"""Megosztott pytest fixture-ök (egyetlen Tk/CustomTkinter főablak a Windows Tcl stabilitásáért)."""

from __future__ import annotations

import pytest

from plex_subtitler.config import app2_config, settings_manager


@pytest.fixture(scope="session")
def gui_app():
    from plex_subtitler.app1_extractor.gui import PlexSubtitlerApp

    orig_mode = app2_config.ui_mode
    orig_save = settings_manager.save
    settings_manager.save = lambda: None  # type: ignore[method-assign]
    app2_config.ui_mode = "simple"
    app = PlexSubtitlerApp()
    app.update()
    try:
        yield app
    finally:
        app2_config.ui_mode = orig_mode
        settings_manager.save = orig_save  # type: ignore[method-assign]
        try:
            app.destroy()
        except Exception:
            pass
