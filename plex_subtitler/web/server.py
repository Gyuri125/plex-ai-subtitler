"""
server.py - Pehelysúlyú, beépített webes dashboard headless és NAS szerverekhez.

Felelősség:
  - Böngészőből és mobilról elérhető állapotkijelzés és konfigurációs felület.
  - REST API végpontok a beállítások lekérésére és módosítására.
  - Külső függőségek nélküli (standard Python http.server) futás.
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app1_config, app2_config, settings_manager, SETTINGS_FILE

log = get_logger(__name__, level=app1_config.log_level)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="hu">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Plex AI Subtitler Dashboard</title>
  <style>
    :root {
      --bg: #14171a;
      --card-bg: #1e2227;
      --card-border: #2d333b;
      --accent: #e5a00d;
      --accent-hover: #cca80c;
      --text: #e6edf3;
      --text-muted: #8b949e;
      --success: #238636;
      --code-bg: #0d1117;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 20px;
    }
    .container {
      max-width: 1000px;
      margin: 0 auto;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
    }
    h1 {
      font-size: 24px;
      font-weight: 700;
      color: var(--text);
    }
    .badge {
      display: inline-block;
      padding: 4px 10px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 600;
      background: var(--success);
      color: #fff;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 20px;
      margin-bottom: 24px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 20px;
    }
    .card h2 {
      font-size: 16px;
      font-weight: 600;
      color: var(--accent);
      margin-bottom: 12px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .stat-row {
      display: flex;
      justify-content: space-between;
      padding: 8px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      font-size: 14px;
    }
    .stat-label { color: var(--text-muted); }
    .stat-value { font-weight: 500; }
    .log-container {
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 16px;
      height: 250px;
      overflow-y: auto;
      font-family: monospace;
      font-size: 12px;
      color: #7ee787;
      white-space: pre-wrap;
    }
    .form-group {
      margin-bottom: 14px;
    }
    label {
      display: block;
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 6px;
    }
    input[type="text"], select {
      width: 100%;
      padding: 8px 12px;
      background: var(--bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      color: var(--text);
      font-size: 14px;
    }
    input[type="text"]:focus, select:focus {
      outline: none;
      border-color: var(--accent);
    }
    .btn {
      display: inline-block;
      padding: 10px 18px;
      background: var(--accent);
      color: #14171a;
      font-weight: 600;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      font-size: 14px;
      transition: background 0.2s;
    }
    .btn:hover { background: var(--accent-hover); }
    .footer {
      text-align: center;
      margin-top: 30px;
      color: var(--text-muted);
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Plex AI Subtitler</h1>
        <p style="color: var(--text-muted); font-size: 14px;">Headless & NAS Rendszerkezelő Dashboard</p>
      </div>
      <div>
        <span class="badge" id="status-badge">Online</span>
      </div>
    </header>

    <div class="grid">
      <!-- 1. Kártya: Állapot és Pipeline -->
      <div class="card">
        <h2>Rendszer Állapot</h2>
        <div class="stat-row">
          <span class="stat-label">Whisper Modell:</span>
          <span class="stat-value" id="stat-model">-</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Számítási Eszköz:</span>
          <span class="stat-value" id="stat-device">-</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">LLM Szolgáltató:</span>
          <span class="stat-value" id="stat-provider">-</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">LLM Modell:</span>
          <span class="stat-value" id="stat-llm-model">-</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Célnyelv:</span>
          <span class="stat-value" id="stat-lang">-</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Feldolgozott Fájlok:</span>
          <span class="stat-value" id="stat-processed">0</span>
        </div>
      </div>

      <!-- 2. Kártya: Gyors Beállítások -->
      <div class="card">
        <h2>Beállítások</h2>
        <div class="form-group">
          <label>Figyelt Mappák (vesszővel elválasztva):</label>
          <input type="text" id="input-dirs" placeholder="C:/Filmek, /mnt/media/movies">
        </div>
        <div class="form-group">
          <label>Célnyelv:</label>
          <select id="select-lang">
            <option value="Hungarian">Hungarian</option>
            <option value="English">English</option>
            <option value="German">German</option>
          </select>
        </div>
        <button class="btn" onclick="saveSettings()">Mentés</button>
        <span id="save-msg" style="margin-left: 10px; font-size: 13px; color: #7ee787;"></span>
      </div>
    </div>

    <!-- Napló Szekció -->
    <div class="card">
      <h2>Legutóbbi Rendszernapló</h2>
      <div class="log-container" id="log-box">Napló betöltése...</div>
    </div>

    <div class="footer">
      Plex AI Subtitler - Automatikus Audio Kinyerés, Feliratozás és Fordítás
    </div>
  </div>

  <script>
    async function loadStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) return;
        const data = await res.json();
        document.getElementById('stat-model').innerText = data.whisper_model || '-';
        document.getElementById('stat-device').innerText = (data.whisper_device || '-') + ' (' + (data.whisper_compute || '-') + ')';
        document.getElementById('stat-provider').innerText = data.llm_provider || '-';
        document.getElementById('stat-llm-model').innerText = data.llm_model || '-';
        document.getElementById('stat-lang').innerText = data.target_language || '-';
        document.getElementById('stat-processed').innerText = data.processed_count || '0';
        
        if (!document.getElementById('input-dirs').value && data.watch_dirs) {
          document.getElementById('input-dirs').value = data.watch_dirs.join(', ');
        }
        if (data.target_language) {
          document.getElementById('select-lang').value = data.target_language;
        }
      } catch (err) {
        console.error(err);
      }
    }

    async function loadLogs() {
      try {
        const res = await fetch('/api/logs');
        if (!res.ok) return;
        const text = await res.text();
        const box = document.getElementById('log-box');
        box.innerText = text || 'Nincs naplóbejegyzés.';
        box.scrollTop = box.scrollHeight;
      } catch (err) {
        console.error(err);
      }
    }

    async function saveSettings() {
      const dirs = document.getElementById('input-dirs').value.split(',').map(s => s.trim()).filter(Boolean);
      const lang = document.getElementById('select-lang').value;
      const msg = document.getElementById('save-msg');

      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ watch_dirs: dirs, target_language: lang })
        });
        if (res.ok) {
          msg.innerText = 'Sikeresen mentve!';
          setTimeout(() => { msg.innerText = ''; }, 3000);
          loadStatus();
        } else {
          msg.innerText = 'Hiba a mentés során!';
        }
      } catch (e) {
        msg.innerText = 'Hálózati hiba!';
      }
    }

    loadStatus();
    loadLogs();
    setInterval(loadStatus, 4000);
    setInterval(loadLogs, 4000);
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Kezeli a dashboard HTTP kéréseit."""

    def log_message(self, format, *args):
        # Elnémítja a konzolos HTTP access naplózást
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return

        elif self.path == "/api/status":
            processed_count = 0
            log_path = Path(app1_config.processed_log)
            if log_path.exists():
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        processed_count = sum(1 for _ in f)
                except Exception:
                    pass

            status_data = {
                "whisper_model": app1_config.whisper_model,
                "whisper_device": app1_config.whisper_device,
                "whisper_compute": app1_config.whisper_compute_type,
                "watch_dirs": app1_config.watch_dirs,
                "llm_provider": app2_config.llm_provider,
                "llm_model": app2_config.llm_model,
                "target_language": app2_config.target_language,
                "processed_count": processed_count,
                "auto_sync": app1_config.auto_sync_subtitles,
                "online_subs": app1_config.download_online_subtitles,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status_data).encode("utf-8"))
            return

        elif self.path == "/api/logs":
            lines = []
            log_path = Path(app1_config.processed_log)
            if log_path.exists():
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [line.strip() for line in f if line.strip()][-40:]
                except Exception:
                    pass

            log_text = "\n".join(lines) if lines else "A rendszer készenlétben áll. Még nincs feldolgozott videó."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(log_text.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/config":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                data = json.loads(body.decode("utf-8"))

                if "watch_dirs" in data and isinstance(data["watch_dirs"], list):
                    app1_config.watch_dirs = data["watch_dirs"]

                if "target_language" in data and isinstance(data["target_language"], str):
                    app2_config.target_language = data["target_language"]

                settings_manager.save()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
                return
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f'{{"error": "{e}"}}'.encode("utf-8"))
                return

        self.send_response(404)
        self.end_headers()


def run_web_dashboard(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Elindítja a beépített webes dashboard HTTP kiszolgálóját."""
    server_address = (host, port)
    httpd = HTTPServer(server_address, DashboardRequestHandler)
    log.info("Plex Subtitler Web Dashboard elindult: http://%s:%d", host if host != "0.0.0.0" else "localhost", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Web Dashboard leallitva.")
    finally:
        httpd.server_close()


def start_web_dashboard_thread(host: str = "0.0.0.0", port: int = 8080) -> threading.Thread:
    """Háttérszálban indítja el a webes dashboardot."""
    t = threading.Thread(target=run_web_dashboard, args=(host, port), name="WebDashboardServer", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    run_web_dashboard()
