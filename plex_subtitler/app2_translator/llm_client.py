"""AI API kliensek (Google Gemini, OpenAI, lokális Ollama/LMStudio) a feliratfordításhoz."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from plex_subtitler.config import app2_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app2_config.log_level)

SYSTEM_PROMPT = """You are a professional movie and TV series subtitle translator.
Your task is to translate the provided subtitle blocks from {source_language} into {target_language}.

Rules you MUST follow strictly:
1. The input format is a list of lines starting with an ID like: [ID] Subtitle text
2. You must output the EXACT SAME IDs in the EXACT SAME format. Do not skip or merge any ID.
3. The translation must sound natural in {target_language}, matching the tone and context of movies. Use appropriate colloquialisms or slang if the original text uses them.
4. Keep the text concise to fit on a screen (avoid overly long explanations).
5. If you see ' \\n ' in the text, it means a line break in the subtitle. Keep it or adjust it so the subtitle remains readable on screen (max 2 lines per block).
6. DO NOT add any extra conversational text (like "Here is the translation"). ONLY return the translated blocks.

{movie_context_section}Additional instructions: {style_hint}"""

CONTEXT_ANALYSIS_PROMPT = """Analyze the following movie title and subtitle excerpt.
Return a concise 3-5 bullet guide (in English, max 120 words) to help a subtitle translator understand:
1. Genre, setting, and overall tone (e.g., formal historical drama vs. casual modern comedy).
2. Main character names (keep untranslated) and whether characters speak formally or informally.
3. Any recurring jargon, titles, or key phrases to translate consistently.

Do NOT translate the lines. Return ONLY the concise guide."""


def _normalize_openai_base_url(base_url: str | None, provider: str) -> str | None:
    """Normalizálja a lokális Ollama/OpenAI-kompatibilis URL-t (/v1 végződéssel)."""
    cleaned = (base_url or "").strip()
    if not cleaned:
        if provider in ("local", "ollama"):
            return "http://localhost:11434/v1"
        return None
    if provider in ("local", "ollama") and not cleaned.rstrip("/").endswith("/v1"):
        return cleaned.rstrip("/") + "/v1"
    return cleaned


def _ollama_root_url(base_url: str | None) -> str:
    base = (base_url or "http://localhost:11434").strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def ensure_ollama_running(base_url: str | None = None, timeout_sec: float = 12.0) -> bool:
    """Ellenőrzi, hogy fut-e az Ollama; ha nem és engedélyezett, automatikusan elindítja."""
    import requests

    root = _ollama_root_url(base_url)
    try:
        resp = requests.get(f"{root}/api/tags", timeout=2)
        if resp.status_code == 200:
            return True
    except Exception:
        pass

    if not app2_config.auto_start_ollama:
        return False

    ollama_exe = shutil.which("ollama")
    if not ollama_exe:
        log.warning("Az 'ollama' parancs nem található a PATH-ban.")
        return False

    log.info("🚀 Ollama háttérszolgáltatás indítása (%s)...", root)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            [ollama_exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as exc:
        log.warning("Nem sikerült elindítani az Ollama szervert: %s", exc)
        return False

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{root}/api/tags", timeout=1.5)
            if resp.status_code == 200:
                log.info("✅ Ollama szerver sikeresen elindult.")
                return True
        except Exception:
            pass
        time.sleep(0.5)

    log.warning("Ollama nem válaszolt %.0f másodpercen belül.", timeout_sec)
    return False


def unload_ollama_model(base_url: str | None = None, model: str | None = None) -> None:
    """Megkéri az Ollama-t, hogy engedje el a modellt a VRAM-ból (keep_alive=0)."""
    import requests

    root = _ollama_root_url(base_url)
    target_model = model or app2_config.llm_model
    if not target_model:
        return
    try:
        requests.post(
            f"{root}/api/generate",
            json={"model": target_model, "keep_alive": 0},
            timeout=5,
        )
        log.debug("Ollama modell VRAM felszabadítás elküldve: %s", target_model)
    except Exception:
        pass


def ensure_ollama_model_pulled(base_url: str | None = None, model: str | None = None) -> bool:
    """Ellenőrzi, hogy a kért Ollama modell le van-e töltve; ha nincs, automatikusan lehúzza."""
    import json
    import requests

    root = _ollama_root_url(base_url)
    target_model = (model or app2_config.llm_model or "").strip()
    if not target_model:
        return False

    try:
        resp = requests.get(f"{root}/api/tags", timeout=5)
        resp.raise_for_status()
        installed = [m.get("name", "") for m in resp.json().get("models", [])]
        if any(
            name == target_model or name == f"{target_model}:latest" or name.split(":")[0] == target_model
            for name in installed
        ):
            return True
    except Exception as exc:
        log.warning("Nem sikerült ellenőrizni az Ollama modelleket (%s): %s", root, exc)
        return False

    if not app2_config.auto_pull_ollama_model:
        log.warning("A(z) '%s' Ollama modell hiányzik. Futtasd: ollama pull %s", target_model, target_model)
        return False

    log.info("📥 A(z) '%s' Ollama modell még nincs letöltve — automatikus letöltés indul...", target_model)
    try:
        last_pct = -10
        with requests.post(
            f"{root}/api/pull",
            json={"name": target_model, "stream": True},
            stream=True,
            timeout=3600,
        ) as r:
            r.raise_for_status()
            for raw_line in r.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    evt = json.loads(raw_line)
                except ValueError:
                    continue
                if evt.get("error"):
                    raise RuntimeError(evt["error"])
                total = evt.get("total") or 0
                completed = evt.get("completed") or 0
                if total > 0:
                    pct = int((completed / total) * 100)
                    if pct >= last_pct + 10 or pct == 100:
                        last_pct = pct
                        log.info("📥 Ollama letöltés (%s): %d%%", target_model, pct)
        log.info("✅ Ollama modell sikeresen letöltve és készen áll: '%s'", target_model)
        return True
    except Exception as exc:
        log.error("❌ Hiba az Ollama modell letöltésekor ('%s'): %s", target_model, exc)
        return False


class LLMClientBase(ABC):
    """Alaposztály az LLM kliensekhez (auto film-kontextus és gördülő folytonosság támogatással)."""

    def __init__(self) -> None:
        self.target_language = app2_config.target_language
        raw_source = (app2_config.source_language or "").strip()
        self.source_language = (
            "the original language" if not raw_source or raw_source.lower() == "auto" else raw_source
        )
        self.style_hint = app2_config.translation_style_hint
        self.glossary: dict[str, str] = dict(app2_config.glossary or {})
        self.movie_context: str = ""
        self.recent_context: str = ""
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        sections: list[str] = []
        if self.movie_context.strip():
            sections.append(f"Movie context & glossary:\n{self.movie_context.strip()}\n")
        if self.glossary:
            glossary_lines = "\n".join(f"- {k} => {v}" for k, v in self.glossary.items() if k.strip())
            if glossary_lines:
                sections.append(
                    "Strict character & terminology glossary (MUST follow these exact translations):\n"
                    f"{glossary_lines}\n"
                )
        if self.recent_context.strip():
            sections.append(
                "Recent preceding dialogue for continuity only (DO NOT repeat or output these lines):\n"
                f"{self.recent_context.strip()}\n"
            )
        context_block = "\n".join(sections)
        if context_block:
            context_block += "\n"
        return SYSTEM_PROMPT.format(
            source_language=self.source_language,
            target_language=self.target_language,
            movie_context_section=context_block,
            style_hint=self.style_hint,
        )

    def set_movie_context(self, movie_context: str) -> None:
        """Beállítja az automatikusan felismert film-kontextust a rendszerpromptba."""
        self.movie_context = movie_context or ""
        self.system_prompt = self._build_system_prompt()

    def set_recent_context(self, recent_lines: str) -> None:
        """Frissíti az előző blokkok pár soros kontextusát a folyamatos párbeszédhez."""
        self.recent_context = recent_lines or ""
        self.system_prompt = self._build_system_prompt()

    def analyze_movie_context(self, movie_title: str, sample_text: str) -> str:
        """Rövid elemzést készít a film műfajáról, szereplőiről és stílusáról fordítás előtt."""
        prompt = f"Movie filename/title: {movie_title}\n\nSubtitle excerpt:\n{sample_text}"
        return self._generate_raw(CONTEXT_ANALYSIS_PROMPT, prompt)

    def _generate_raw(self, system_instruction: str, user_content: str) -> str:
        """Alapértelmezett belső szöveggenerálás (alacsony szintű alosztályok felülírhatják)."""
        old_prompt = self.system_prompt
        try:
            self.system_prompt = system_instruction
            return self.translate_chunk(user_content)
        finally:
            self.system_prompt = old_prompt

    @abstractmethod
    def translate_chunk(self, chunk_text: str) -> str:
        """Lefordítja a kapott szövegblokkot."""


class OpenAIClient(LLMClientBase):
    """OpenAI API kliens (kompatibilis a lokális Ollama/LMStudio szerverekkel is)."""

    def __init__(self) -> None:
        super().__init__()
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("Az 'openai' csomag nincs telepítve. pip install openai") from exc

        provider = app2_config.llm_provider.lower().strip()
        api_key = (app2_config.llm_api_key or "").strip() or "dummy-key-for-local"
        base_url = _normalize_openai_base_url(app2_config.llm_base_url, provider)
        self.model = app2_config.llm_model
        self.is_local = provider in ("local", "ollama")

        if self.is_local:
            if ensure_ollama_running(base_url):
                ensure_ollama_model_pulled(base_url, self.model)

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)

    def translate_chunk(self, chunk_text: str) -> str:
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": chunk_text},
                ],
                "temperature": 0.3,
            }
            if self.is_local and app2_config.ollama_keep_alive:
                kwargs["extra_body"] = {"keep_alive": app2_config.ollama_keep_alive}
            response = self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as exc:
            log.error("OpenAI API hiba: %s", exc)
            raise


class GeminiClient(LLMClientBase):
    """Google Gemini API kliens."""

    def __init__(self) -> None:
        super().__init__()
        try:
            from google import genai
        except ImportError as exc:
            raise ImportError("A 'google-genai' csomag nincs telepítve. pip install google-genai") from exc

        api_key = (app2_config.llm_api_key or "").strip()
        if not api_key:
            raise ValueError("Google Gemini API kulcs hiányzik a beállításokból!")

        self.client = genai.Client(api_key=api_key)
        self.model_name = app2_config.llm_model

    def translate_chunk(self, chunk_text: str) -> str:
        try:
            from google.genai import types

            response = self.client.models.generate_content(
                model=self.model_name,
                contents=chunk_text,
                config=types.GenerateContentConfig(
                    system_instruction=self.system_prompt,
                    temperature=0.3,
                ),
            )
            return response.text or ""
        except Exception as exc:
            log.error("Google Gemini API hiba: %s", exc)
            raise


def get_llm_client() -> LLMClientBase:
    """Factory függvény a megfelelő LLM kliens kiválasztásához."""
    provider = app2_config.llm_provider.lower().strip()
    log.info("LLM Kliens inicializálása: Provider='%s', Modell='%s'", provider, app2_config.llm_model)

    if provider == "google":
        return GeminiClient()
    if provider in ("openai", "ollama", "local"):
        return OpenAIClient()

    log.warning("Ismeretlen provider: '%s', fallback OpenAI-ra.", provider)
    return OpenAIClient()


def translate_blocks_with_retry(
    client: LLMClientBase,
    chunk_text: str,
    retries: int = 15,
    stop_event: threading.Event | None = None,
) -> str:
    """Megkísérli lefordítani a blokkot, átmeneti vagy kvóta hibánál újrapróbálkozik."""
    for attempt in range(retries):
        if stop_event and stop_event.is_set():
            return ""
        try:
            if attempt == 0 and app2_config.api_request_delay > 0:
                time.sleep(app2_config.api_request_delay)
            return client.translate_chunk(chunk_text)
        except Exception as exc:
            if stop_event and stop_event.is_set():
                return ""
            err_msg = str(exc).lower()
            is_rate_or_server = any(
                code in err_msg for code in ("429", "too many requests", "resource exhausted", "500", "503")
            )
            if is_rate_or_server:
                match = re.search(r"try again in (\d+)s", err_msg)
                wait_time = int(match.group(1)) + 2 if match else max(5, (attempt + 1) * 4)
                log.warning(
                    "API kvóta limit vagy szerver hiba elérve (kísérlet %d/%d). Várakozás %d másodpercet...",
                    attempt + 1,
                    retries,
                    wait_time,
                )
                for _ in range(wait_time):
                    if stop_event and stop_event.is_set():
                        return ""
                    time.sleep(1)
            else:
                wait_time = min(15, (attempt + 1) * 2)
                log.warning(
                    "Fordítási kísérlet %d/%d sikertelen: %s",
                    attempt + 1,
                    retries,
                    exc,
                )
                time.sleep(wait_time)

    log.error("Az összes fordítási kísérlet elbukott ehhez a blokkhoz.")
    return ""


_NON_TEXT_MODEL_KEYWORDS = (
    "image",
    "tts",
    "transcribe",
    "computer-use",
    "robotics",
    "nano-banana",
    "embedding",
    "audio",
    "realtime",
    "whisper",
)


def _is_text_translation_model(model_id: str) -> bool:
    """Kiszűri a kép-, hang-, TTS- és speciális végpont-modelleket a fordítási listából."""
    lowered = model_id.lower()
    return not any(kw in lowered for kw in _NON_TEXT_MODEL_KEYWORDS)


def get_available_models(
    provider: str,
    api_key: str | None = None,
    local_url: str | None = "http://localhost:11434",
) -> list[str]:
    """Lekérdezi az elérhető szövegfordító modelleket a megadott providertől."""
    prov = provider.lower().strip()

    if prov == "google":
        if not api_key:
            raise ValueError("API kulcs szükséges a Google modellek lekérdezéséhez!")
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            models = []
            for m in client.models.list():
                actions = getattr(m, "supported_actions", []) or []
                name = getattr(m, "name", "")
                short_name = name.replace("models/", "")
                if (
                    ("generateContent" in actions or not actions)
                    and "gemini" in short_name.lower()
                    and _is_text_translation_model(short_name)
                ):
                    models.append(short_name)
            return sorted(set(models))
        except Exception as exc:
            raise RuntimeError(f"Hiba a Google modellek lekérdezésekor: {exc}") from exc

    if prov == "openai":
        if not api_key:
            raise ValueError("API kulcs szükséges az OpenAI modellek lekérdezéséhez!")
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            resp = client.models.list()
            models = [
                m.id for m in resp.data
                if any(prefix in m.id.lower() for prefix in ("gpt", "o1", "o3", "o4"))
                and _is_text_translation_model(m.id)
            ]
            models.sort(reverse=True)
            return models
        except Exception as exc:
            raise RuntimeError(f"Hiba az OpenAI modellek lekérdezésekor: {exc}") from exc

    # Lokális Ollama szerver
    import requests

    base = (local_url or "http://localhost:11434").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        resp = requests.get(f"{base}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", []) if "name" in m]
    except Exception as exc:
        raise RuntimeError(
            f"Hiba a lokális (Ollama) modellek lekérdezésekor. Fut a szerver a {base} címen?\nRészletek: {exc}"
        ) from exc
