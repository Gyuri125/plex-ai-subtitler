"""
llm_client.py - AI API Kliensek (OpenAI, Gemini) a feliratok fordításához.

Factory patternnel választjuk ki a megfelelő providert (Google Gemini vagy OpenAI/Local).
Erős rendszerpromptot használ, hogy a fordítás mozis minőségű maradjon,
és a formátumot az SRT Chunker-nek megfelelően visszaadja.
"""

from __future__ import annotations
import time
from abc import ABC, abstractmethod

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app2_config

log = get_logger(__name__, level=app2_config.log_level)


# Erős rendszer prompt a fordítónak
SYSTEM_PROMPT = """
You are a professional Hungarian movie and TV series subtitle translator.
Your task is to translate the provided English subtitle blocks into {target_language}.

Rules you MUST follow strictly:
1. The input format is a list of lines starting with an ID like: [ID] Subtitle text
2. You must output the EXACT SAME IDs in the EXACT SAME format. Do not skip any ID.
3. The translation must sound natural in {target_language}, matching the tone and context of movies. Use appropriate colloquialisms or slang if the original text uses them.
4. Keep the text concise to fit on a screen (avoid overly long explanations).
5. If you see ' \\n ' in the text, it means a line break in the subtitle. Keep it or adjust it so the subtitle remains readable on screen (max 2 lines per block).
6. DO NOT add any extra conversational text (like "Here is the translation"). ONLY return the translated blocks.
7. If previous context is provided under '[PREVIOUS SCENE CONTEXT]', use it strictly to maintain narrative tone, consistency, pronouns, and polite/informal address (tegezés/magázódás in Hungarian). DO NOT re-translate or output the context lines! ONLY translate the lines under '[BLOCKS TO TRANSLATE]'.

Additional instructions: {style_hint}
"""

class LLMClientBase(ABC):
    """Alaposztály az LLM kliensekhez."""
    
    def __init__(self):
        self.target_language = app2_config.target_language
        self.style_hint = app2_config.translation_style_hint
        self.system_prompt = SYSTEM_PROMPT.format(
            target_language=self.target_language,
            style_hint=self.style_hint
        )

    @abstractmethod
    def translate_chunk(self, chunk_text: str, context_text: str = "") -> str:
        """Lefordítja a kapott szövegblokkot."""
        pass


class OpenAIClient(LLMClientBase):
    """OpenAI API kliens (kompatibilis a lokális Ollama/LMStudio szerverekkel is)."""
    
    def __init__(self):
        super().__init__()
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Az 'openai' csomag nincs telepítve. pip install openai")
            
        api_key = app2_config.llm_api_key or "dummy-key-for-local"
        base_url = app2_config.llm_base_url
        
        self.model = app2_config.llm_model
        
        # Ha van base_url (pl. Ollama), azt is átadjuk
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
            
        self.client = OpenAI(**kwargs)

    def translate_chunk(self, chunk_text: str, context_text: str = "") -> str:
        user_content = chunk_text
        if context_text.strip():
            user_content = (
                f"[PREVIOUS SCENE CONTEXT (DO NOT TRANSLATE)]:\n{context_text.strip()}\n\n"
                f"[BLOCKS TO TRANSLATE]:\n{chunk_text}"
            )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content}
        ]
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3, # Alacsony hőmérséklet a pontos formátumért
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            log.error(f"OpenAI API hiba: {e}")
            raise


class GeminiClient(LLMClientBase):
    """Google Gemini API kliens."""
    
    def __init__(self):
        super().__init__()
        try:
            from google import genai
        except ImportError:
            raise ImportError("A 'google-genai' csomag nincs telepítve. pip install google-genai")
            
        api_key = app2_config.llm_api_key
        if not api_key:
            raise ValueError("Google Gemini API kulcs hiányzik a beállításokból!")
            
        self.client = genai.Client(api_key=api_key)
        self.model_name = app2_config.llm_model

    def translate_chunk(self, chunk_text: str, context_text: str = "") -> str:
        user_content = chunk_text
        if context_text.strip():
            user_content = (
                f"[PREVIOUS SCENE CONTEXT (DO NOT TRANSLATE)]:\n{context_text.strip()}\n\n"
                f"[BLOCKS TO TRANSLATE]:\n{chunk_text}"
            )

        try:
            from google.genai import types
            
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=self.system_prompt,
                    temperature=0.3,
                )
            )
            return response.text
        except Exception as e:
            log.error(f"Google Gemini API hiba: {e}")
            raise


def get_llm_client() -> LLMClientBase:
    """Factory függvény a megfelelő LLM kliens kiválasztásához."""
    provider = app2_config.llm_provider.lower().strip()
    
    log.info(f"LLM Kliens inicializálása: Provider='{provider}', Modell='{app2_config.llm_model}'")
    
    if provider == "google":
        return GeminiClient()
    elif provider in ["openai", "ollama", "local"]:
        return OpenAIClient()
    else:
        log.warning(f"Ismeretlen provider: '{provider}', fallback OpenAI-ra.")
        return OpenAIClient()

def translate_blocks_with_retry(client: LLMClientBase, chunk_text: str, context_text: str = "", retries: int = 15, stop_event=None) -> str:
    """Megkísérli lefordítani a blokkot, hiba esetén újrapróbálkozik."""
    import re
    attempt = 1
    while attempt <= retries:
        if stop_event and stop_event.is_set():
            return ""
            
        try:
            time.sleep(app2_config.api_request_delay)
            result = client.translate_chunk(chunk_text, context_text=context_text)
            return result
        except Exception as e:
            err_msg = str(e).lower()
            
            # 429 Too Many Requests vagy 500/503 szerver hiba
            if any(code in err_msg for code in ["429", "too many requests", "resource exhausted", "500", "503"]):
                wait_time = 60
                match = re.search(r'try again in (\d+)s', err_msg)
                if match:
                    wait_time = max(60, int(match.group(1)))
                
                log.warning(f"API kvóta limit vagy szerver hiba elérve (kísérlet {attempt}/{retries}). Várakozás {wait_time} másodpercet a folytatásig...")
                
                # Altatás mp-enként, hogy a leállításra reagáljunk
                for _ in range(wait_time):
                    if stop_event and stop_event.is_set():
                        return ""
                    time.sleep(1)
                attempt += 1
            else:
                log.warning(f"Fordítási kísérlet {attempt}/{retries} sikertelen: {e}")
                if attempt == retries:
                    log.error("Az összes fordítási kísérlet elbukott ehhez a blokkhoz.")
                    return ""
                
                wait_time = 2 * attempt # Exponenciális backoff
                for _ in range(wait_time):
                    if stop_event and stop_event.is_set():
                        return ""
                    time.sleep(1)
                attempt += 1
                
    return ""

def get_available_models(provider: str, api_key: str = None, local_url: str = "http://localhost:11434") -> list[str]:
    """Lekérdezi az elérhető modelleket a megadott providertől."""
    provider = provider.lower().strip()
    models = []
    
    if provider == "google":
        try:
            from google import genai
            if not api_key:
                raise ValueError("API kulcs szükséges a Google modellek lekérdezéséhez!")
            client = genai.Client(api_key=api_key)
            
            # Lekérdezzük a modelleket, és szűrünk a szöveggenerálásra alkalmasakra
            for m in client.models.list():
                # Biztosítjuk, hogy csak a generate_content hívásra alkalmas, releváns modellek kerüljenek a listába
                if "generateContent" in getattr(m, "supported_actions", []) or "gemini" in m.name.lower():
                    name = m.name.replace("models/", "")
                    if name not in models:
                        models.append(name)
            
            if not models:
                models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]
        except Exception as e:
            raise RuntimeError(f"Hiba a Google modellek lekérdezésekor: {e}")
            
    elif provider == "openai":
        try:
            from openai import OpenAI
            if not api_key:
                raise ValueError("API kulcs szükséges az OpenAI modellek lekérdezéséhez!")
            client = OpenAI(api_key=api_key)
            api_models = client.models.list()
            # Szűrünk a gpt/o1 modellekre, hogy releváns maradjon
            models = [m.id for m in api_models.data if "gpt" in m.id or "o1" in m.id]
            models.sort(reverse=True)
            if not models:
                models = ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"]
        except Exception as e:
            raise RuntimeError(f"Hiba az OpenAI modellek lekérdezésekor: {e}")
            
    elif provider in ["local", "ollama"]:
        import requests
        try:
            url = f"{local_url.rstrip('/')}/api/tags"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            models = [m.get("name") for m in data.get("models", [])]
            if not models:
                models = ["llama3", "mistral", "phi3"]
        except Exception as e:
            raise RuntimeError(f"Hiba a lokális (Ollama) modellek lekérdezésekor. Fut a szerver a {local_url} címen?\nRészletek: {e}")
            
    return models
