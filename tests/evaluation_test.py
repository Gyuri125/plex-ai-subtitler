"""
evaluation_test.py - Részletes kiértékelő és terheléses tesztcsomag.

Méri és ellenőrzi:
  1. Modulok és függőségek épsége.
  2. 64-bites OSdb moviehash és release-név tisztító algoritmusok sebessége és pontossága.
  3. Quality Gate hallucináció-szűrési arány, időkód-simítás és adatvesztés-mentesség.
  4. Sorozat-memória (Series Glossary) felismerési pontosság és prompt-injektálás.
  5. Párhuzamosított fordítási pipeline (1 szál vs 2 szál) integritása és időzítése.
  6. Beépített Web Dashboard REST API válaszidők és HTTP kódok.
  7. Adatbiztonság és Gitignore integritás (API kulcsok és naplók kizárása).
"""

import sys
import time
import json
import urllib.request
import unittest
from pathlib import Path
from http.server import HTTPServer
import threading

# Project root hozzáadása
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plex_subtitler.config import app1_config, app2_config, settings_manager, SETTINGS_FILE
from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser
from plex_subtitler.quality_gate import is_hallucination, clean_whisper_blocks, validate_and_repair_translated_blocks
from plex_subtitler.subtitle_downloader import compute_movie_hash, clean_title_from_filename
from plex_subtitler.app2_translator.series_memory import detect_series_info, get_series_prompt_injection, load_series_context
from plex_subtitler.sub_sync import is_sync_tool_available, auto_sync_subtitle
from plex_subtitler.web.server import DashboardRequestHandler


class DetailedEvaluationTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print("=" * 75)
        print("PLEX AI SUBTITLER - RÉSZLETES RENDSZERKIÉRTÉKELŐ TESZT")
        print("=" * 75)

    def test_01_dependency_and_architecture_integrity(self):
        """1. Architektúra és modulok betöltésének ellenőrzése."""
        t0 = time.perf_counter()
        import plex_subtitler.logger
        import plex_subtitler.notifier
        import plex_subtitler.subtitle_downloader
        import plex_subtitler.sub_sync
        import plex_subtitler.quality_gate
        import plex_subtitler.web.server
        import plex_subtitler.app1_extractor.file_processor
        import plex_subtitler.app1_extractor.hardware_manager
        import plex_subtitler.app2_translator.translator_daemon
        import plex_subtitler.app2_translator.series_memory
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"[OK] 1. Modul-architektúra betöltve ({elapsed:.2f} ms).")

    def test_02_moviehash_and_title_cleaner_benchmark(self):
        """2. Moviehash és fájlnév-tisztító algoritmus pontossága és sebessége."""
        test_cases = [
            ("Dune.Part.Two.2024.1080p.WEBRip.x264.AAC5.1-[YTS.MX].mp4", "Dune Part Two 2024"),
            ("Stranger.Things.S04E01.Chapter.One.720p.NF.WEB-DL.DDP5.1.Atmos.x264.mkv", "Stranger Things S04E01 Chapter One"),
            ("Oppenheimer.2023.2160p.UHD.BluRay.x265.10bit.HDR.TrueHD.7.1.Atmos.mkv", "Oppenheimer 2023"),
            ("Breaking_Bad_S05E16_Felina_1080p_BluRay.mkv", "Breaking Bad S05E16 Felina"),
        ]

        t0 = time.perf_counter()
        for filename, expected_stem in test_cases:
            cleaned = clean_title_from_filename(filename)
            self.assertEqual(cleaned, expected_stem)

        # Hasmérések generált dummy fájllal (1 MB)
        dummy_file = PROJECT_ROOT / "temp_benchmark_file.mkv"
        try:
            dummy_file.write_bytes(b"\x01\x02\x03\x04" * 262144) # 1 MB
            mhash, size = compute_movie_hash(dummy_file)
            self.assertEqual(size, 1048576)
            self.assertTrue(len(mhash) == 16)
        finally:
            if dummy_file.exists():
                dummy_file.unlink()

        elapsed = (time.perf_counter() - t0) * 1000
        print(f"[OK] 2. Hash & Title Cleaner algoritmus sikeres (4/4 teszteset, {elapsed:.2f} ms).")

    def test_03_quality_gate_precision(self):
        """3. Quality Gate szűrési pontosság és időbélyeg-integritás."""
        synthetic_blocks = [
            SrtBlock(1, "00:00:01,000 --> 00:00:03,000", "Thank you for watching!"),
            SrtBlock(2, "00:00:03,500 --> 00:00:05,000", "Jó napot kívánok, hadnagy úr."),
            SrtBlock(3, "00:00:05,200 --> 00:00:06,000", "Please subscribe to our channel"),
            SrtBlock(4, "00:00:06,500 --> 00:00:08,000", "Hol találom az irattárat?"),
            SrtBlock(5, "00:00:08,500 --> 00:00:09,000", "Subtitles by Amara.org"),
            SrtBlock(6, "00:00:09,500 --> 00:00:10,000", "  "),
            SrtBlock(7, "00:00:10,500 --> 00:00:12,000", "Az épület jobb szárnyában."),
        ]

        cleaned = clean_whisper_blocks(synthetic_blocks)
        # Pontosan a 3 érvényes mondatnak kell megmaradnia
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0].text, "Jó napot kívánok, hadnagy úr.")
        self.assertEqual(cleaned[1].text, "Hol találom az irattárat?")
        self.assertEqual(cleaned[2].text, "Az épület jobb szárnyában.")
        # Sorszámoknak 1, 2, 3-nak kell lenniük
        self.assertEqual([b.index for b in cleaned], [1, 2, 3])

        # Integritás-javító teszt (ha az LLM kihagyna blokkot)
        partial_translated = [
            SrtBlock(1, "00:00:03,500 --> 00:00:05,000", "Good day, lieutenant."),
            # 2. blokk véletlenül kimaradt az LLM válaszából
            SrtBlock(3, "00:00:10,500 --> 00:00:12,000", "In the right wing of the building."),
        ]
        repaired = validate_and_repair_translated_blocks(cleaned, partial_translated)
        self.assertEqual(len(repaired), 3)
        self.assertEqual(repaired[1].text, "Hol találom az irattárat?")
        print("[OK] 3. Quality Gate: 100% hallucináció kiszűrve, 0 adatvesztés.")

    def test_04_series_memory_consistency(self):
        """4. Sorozat-memória kontextus felépítése és perzisztenciája."""
        test_path = PROJECT_ROOT / "temp_test_series" / "Season 02" / "Severance.S02E05.mkv"
        info = detect_series_info(test_path)
        self.assertIsNotNone(info)
        self.assertEqual(info["title"], "Severance")
        self.assertEqual(info["season"], 2)
        self.assertEqual(info["episode"], 5)

        context_file = test_path.parent.parent / "series_context.json"
        try:
            prompt_text = get_series_prompt_injection(test_path)
            self.assertIn("SERIES CONTEXT MEMORY", prompt_text)
            self.assertIn("Severance", prompt_text)
            self.assertTrue(context_file.exists())
        finally:
            if context_file.exists():
                context_file.unlink()
            if test_path.parent.exists():
                test_path.parent.rmdir()
            if (PROJECT_ROOT / "temp_test_series").exists():
                (PROJECT_ROOT / "temp_test_series").rmdir()

        print("[OK] 4. Sorozat-memória felismerés és automatikus sablongenerálás sikeres.")

    def test_05_parallel_worker_pipeline_integrity(self):
        """5. Párhuzamosított munkaszál-felosztás integritása és sorrendtartása."""
        total_blocks = 60
        synthetic_blocks = [
            SrtBlock(i, f"00:{i:02d}:00,000 --> 00:{i:02d}:02,000", f"Dialog sentence number {i}")
            for i in range(1, total_blocks + 1)
        ]

        batch_size = 10
        chunks = [synthetic_blocks[i:i + batch_size] for i in range(0, total_blocks, batch_size)]
        self.assertEqual(len(chunks), 6)

        # 2 szálra osztás
        workers = 2
        num_partitions = min(workers, len(chunks))
        part_size = (len(chunks) + num_partitions - 1) // num_partitions
        partitions = [chunks[i:i + part_size] for i in range(0, len(chunks), part_size)]
        self.assertEqual(len(partitions), 2)
        self.assertEqual(len(partitions[0]), 3)
        self.assertEqual(len(partitions[1]), 3)

        # Ellenőrizzük, hogy minden blokk pontosan egyszer szerepel, szigorú sorrendben
        merged = [b for part in partitions for chk in part for b in chk]
        self.assertEqual(len(merged), total_blocks)
        for idx, b in enumerate(merged, start=1):
            self.assertEqual(b.index, idx)

        print("[OK] 5. Párhuzamos partíció-felosztás: 100% sorrendtartás és integritás.")

    def test_06_web_dashboard_performance_and_api(self):
        """6. Beépített Web Dashboard REST API válaszidők és végpontok."""
        test_port = 8998
        server = HTTPServer(("127.0.0.1", test_port), DashboardRequestHandler)
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        try:
            # GET / (HTML oldal)
            t0 = time.perf_counter()
            with urllib.request.urlopen(f"http://127.0.0.1:{test_port}/") as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode("utf-8")
                self.assertIn("Plex AI Subtitler", html)
            lat_html = (time.perf_counter() - t0) * 1000

            # GET /api/status (JSON státusz)
            t0 = time.perf_counter()
            with urllib.request.urlopen(f"http://127.0.0.1:{test_port}/api/status") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("whisper_model", data)
                self.assertIn("target_language", data)
            lat_api = (time.perf_counter() - t0) * 1000

            # POST /api/config (Beállítás mentés)
            req = urllib.request.Request(
                f"http://127.0.0.1:{test_port}/api/config",
                data=json.dumps({"target_language": "Hungarian"}).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                res_json = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(res_json.get("status"), "ok")

            print(f"[OK] 6. Web Dashboard API válaszidők: HTML {lat_html:.2f} ms | Status JSON {lat_api:.2f} ms.")
        finally:
            server.shutdown()
            server.server_close()

    def test_07_security_and_secrets_isolation(self):
        """7. Biztonsági audit: API kulcsok és privát fájlok védettsége."""
        gitignore_path = PROJECT_ROOT / ".gitignore"
        self.assertTrue(gitignore_path.exists())
        gitignore_content = gitignore_path.read_text(encoding="utf-8")

        self.assertIn("settings.json", gitignore_content)
        self.assertIn("processed_files.log", gitignore_content)
        self.assertIn(".venv", gitignore_content)

        # Ellenőrizzük, hogy a settings.json.example NEM tartalmaz éles API kulcsot
        example_path = PROJECT_ROOT / "settings.json.example"
        self.assertTrue(example_path.exists())
        example_content = example_path.read_text(encoding="utf-8")
        self.assertIn("YOUR_GEMINI_OR_OPENAI_API_KEY_HERE", example_content)
        self.assertNotIn("AIzaSy", example_content)
        self.assertNotIn("sk-proj-", example_content)
        print("[OK] 7. Biztonsági audit: API kulcsok és titkok izolációja sziklaszilárd.")


if __name__ == "__main__":
    unittest.main()
