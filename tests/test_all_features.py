"""
test_all_features.py - Teljes átfogó funkcióteszt a Plex AI Subtitler pipeline-hoz.
"""

import sys
import unittest
import urllib.request
import json
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, r"c:\Users\gyuri\PycharmProjects\Gemini-AI_plexfordító")

class TestPlexSubtitler(unittest.TestCase):

    def test_01_config_and_settings(self):
        from plex_subtitler.config import settings_manager, app1_config, app2_config
        self.assertIsNotNone(app1_config)
        self.assertIsNotNone(app2_config)
        self.assertTrue(hasattr(app1_config, "extract_embedded_subs"))
        self.assertTrue(hasattr(app1_config, "download_online_subtitles"))
        self.assertTrue(hasattr(app1_config, "auto_sync_subtitles"))
        self.assertTrue(hasattr(app1_config, "quality_gate_enabled"))
        self.assertTrue(hasattr(app2_config, "series_memory_enabled"))
        self.assertTrue(hasattr(app2_config, "parallel_translation_workers"))
        print("[PASS] 1. Konfigurációs modul és új beállításmezők sikeresen betöltve.")

    def test_02_srt_parser_and_chunking(self):
        from plex_subtitler.app2_translator.srt_parser import SrtParser, SrtBlock
        
        sample_srt = """1
00:00:01,000 --> 00:00:04,000
Hello world!
This is a test.

2
00:00:05,000 --> 00:00:08,000
Another line here.
"""
        test_file = Path("test_sample.srt")
        test_file.write_text(sample_srt, encoding="utf-8")
        try:
            parser = SrtParser(test_file)
            self.assertEqual(len(parser.blocks), 2)
            self.assertEqual(parser.blocks[0].index, 1)
            self.assertEqual(parser.blocks[0].timestamp, "00:00:01,000 --> 00:00:04,000")
            self.assertIn("Hello world!", parser.blocks[0].text)

            chunks = list(parser.get_chunks(batch_size=1))
            self.assertEqual(len(chunks), 2)

            text_format = SrtParser.blocks_to_text_format(parser.blocks)
            self.assertIn("[1] Hello world! \\n This is a test.", text_format)

            translated_text = "[1] Szia vilag! \\n Ez egy teszt.\n[2] Egy masik sor itt."
            translated_blocks = SrtParser.apply_translation_to_blocks(parser.blocks, translated_text)
            self.assertIn("Szia vilag!", translated_blocks[0].text)
            self.assertIn("Egy masik sor itt.", translated_blocks[1].text)

            out_file = Path("test_sample.hu.srt")
            SrtParser.save_srt(translated_blocks, out_file)
            self.assertTrue(out_file.exists())
            out_file.unlink()
        finally:
            if test_file.exists():
                test_file.unlink()
        print("[PASS] 2. SRT Parser, blokkcsomagolás és újraépítés sikeres.")

    def test_03_sliding_context_llm_prompt(self):
        from plex_subtitler.app2_translator.llm_client import LLMClientBase, SYSTEM_PROMPT
        self.assertIn("PREVIOUS SCENE CONTEXT", SYSTEM_PROMPT)
        self.assertIn("BLOCKS TO TRANSLATE", SYSTEM_PROMPT)
        print("[PASS] 3. Sliding Context prompt szabályok ellenőrizve.")

    def test_04_subtitle_extractor_probe(self):
        from plex_subtitler.app1_extractor.subtitle_extractor import probe_subtitle_streams, try_extract_embedded_subtitles
        dummy_path = Path("non_existent_movie.mkv")
        streams = probe_subtitle_streams(dummy_path)
        self.assertEqual(streams, [])
        srt, is_hu = try_extract_embedded_subtitles(dummy_path)
        self.assertIsNone(srt)
        self.assertFalse(is_hu)
        print("[PASS] 4. Beágyazott felirat szondázó és hibakezelés ellenőrizve.")

    def test_05_notifier_graceful_failures(self):
        from plex_subtitler.notifier import refresh_plex_library, send_discord_notification, send_telegram_notification
        res_plex = refresh_plex_library()
        self.assertFalse(res_plex)
        res_discord = send_discord_notification("Teszt", "Üzenet")
        self.assertFalse(res_discord)
        res_telegram = send_telegram_notification("Teszt", "Üzenet")
        self.assertFalse(res_telegram)
        print("[PASS] 5. Értesítő modul hibatűrése ellenőrizve.")

    def test_06_hardware_manager(self):
        from plex_subtitler.app1_extractor.hardware_manager import HardwareManager
        hw = HardwareManager()
        profile = hw.get_recommended_profile()
        self.assertIn("whisper_model", profile)
        self.assertIn("whisper_device", profile)
        self.assertIn("whisper_compute_type", profile)
        print(f"[PASS] 6. Hardverkezelő detektálás működik (Ajánlott profil: {profile}).")

    def test_07_gui_classes(self):
        from plex_subtitler.app1_extractor.gui import HAS_TRAY
        self.assertTrue(HAS_TRAY, "A pystray és PIL csomagoknak elérhetőnek kell lenniük!")
        print("[PASS] 7. Rendszertálca támogatás aktív.")

    def test_08_online_subtitle_downloader(self):
        from plex_subtitler.subtitle_downloader import compute_movie_hash, clean_title_from_filename, try_download_subtitles
        cleaned = clean_title_from_filename("Breaking.Bad.S01E01.Pilot.1080p.BluRay.x264.mkv")
        self.assertNotIn("1080p", cleaned)
        self.assertNotIn("BluRay", cleaned)
        self.assertIn("Breaking Bad S01E01 Pilot", cleaned)

        # Hash ellenőrzése nem létező vagy kis méretű fájlra
        dummy_file = Path("test_dummy.mkv")
        dummy_file.write_bytes(b"0" * 100)
        try:
            h, sz = compute_movie_hash(dummy_file)
            self.assertEqual(sz, 100)
            self.assertEqual(h, "")
        finally:
            if dummy_file.exists():
                dummy_file.unlink()
        print("[PASS] 8. Online feliratletöltő modul működése ellenőrizve.")

    def test_09_sub_sync(self):
        from plex_subtitler.sub_sync import is_sync_tool_available, auto_sync_subtitle
        available = is_sync_tool_available()
        # Ha nincs telepítve, nem crashelhet, False-t ad
        res = auto_sync_subtitle(Path("fake_video.mkv"), Path("fake_sub.srt"))
        self.assertFalse(res)
        print(f"[PASS] 9. Auto-sync modul működése ellenőrizve (ffsubsync elérhető: {available}).")

    def test_10_series_memory(self):
        from plex_subtitler.app2_translator.series_memory import (
            detect_series_info, load_series_context, get_series_prompt_injection
        )
        sample_path = Path("Shows/Loki/Season 01/Loki.S01E03.1080p.mkv")
        info = detect_series_info(sample_path)
        self.assertIsNotNone(info)
        self.assertEqual(info["season"], 1)
        self.assertEqual(info["episode"], 3)
        self.assertEqual(info["title"], "Loki")

        prompt_inj = get_series_prompt_injection(sample_path)
        self.assertIn("SERIES CONTEXT MEMORY", prompt_inj)
        self.assertIn("Loki", prompt_inj)
        print("[PASS] 10. Sorozat-memória és karakter szótár működése ellenőrizve.")

    def test_11_quality_gate(self):
        from plex_subtitler.quality_gate import is_hallucination, clean_whisper_blocks, validate_and_repair_translated_blocks
        from plex_subtitler.app2_translator.srt_parser import SrtBlock

        self.assertTrue(is_hallucination("Thank you for watching!"))
        self.assertTrue(is_hallucination("Please subscribe to my channel."))
        self.assertTrue(is_hallucination("Subtitles by Amara.org"))
        self.assertFalse(is_hallucination("Hello officer, where is the station?"))

        blocks = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Thank you for watching!"),
            SrtBlock(2, "00:00:03,000 --> 00:00:05,000", "Valodi beszedhang."),
            SrtBlock(3, "00:00:06,000 --> 00:00:07,000", "Please subscribe"),
        ]
        cleaned = clean_whisper_blocks(blocks)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].text, "Valodi beszedhang.")

        # Hiányzó fordítás helyreállítása
        translated = [] # üres válasz az LLM-től
        repaired = validate_and_repair_translated_blocks(blocks, translated)
        self.assertEqual(len(repaired), 3) # Visszaállította az eredetieket
        print("[PASS] 11. Quality Gate hallucináció-szűrés és integritás-ellenőrzés sikeres.")

    def test_12_web_dashboard_server(self):
        from http.server import HTTPServer
        from plex_subtitler.web.server import DashboardRequestHandler

        server = HTTPServer(("127.0.0.1", 8999), DashboardRequestHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            # 1. Dashboard HTML kérés
            req = urllib.request.urlopen("http://127.0.0.1:8999/")
            self.assertEqual(req.status, 200)
            html = req.read().decode("utf-8")
            self.assertIn("Plex AI Subtitler", html)

            # 2. Status API kérés
            req_api = urllib.request.urlopen("http://127.0.0.1:8999/api/status")
            self.assertEqual(req_api.status, 200)
            data = json.loads(req_api.read().decode("utf-8"))
            self.assertIn("whisper_model", data)
            self.assertIn("llm_model", data)
        finally:
            server.shutdown()
            server.server_close()
        print("[PASS] 12. Beépített Web Dashboard szerver és REST API ellenőrizve.")

if __name__ == "__main__":
    unittest.main()
