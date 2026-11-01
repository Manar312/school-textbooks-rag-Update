from app.core.errors import ConfigError
from app.providers.llm.compat_llms import GEMINI_BASE_URL, GROQ_BASE_URL
from app.providers.vlm.openai_vlm import OpenAIVLM


class GroqVLM(OpenAIVLM):
    """Groq vision (free tier): meta-llama/llama-4-scout-17b-16e-instruct (Preview)."""
    name = "groq"

    def __init__(self, model: str, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ConfigError("Groq provider needs GROQ_API_KEY (or VLM_API_KEY). Get one free at console.groq.com")
        super().__init__(model, api_key, base_url or GROQ_BASE_URL)


class GeminiVLM(OpenAIVLM):
    name = "gemini"

    def __init__(self, model: str, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ConfigError("Gemini provider needs GEMINI_API_KEY (or VLM_API_KEY)")
        super().__init__(model, api_key, base_url or GEMINI_BASE_URL)
