"""Free-tier-friendly OpenAI-compatible vendors. They reuse OpenAILLM; only the default endpoint and key rule differ."""
from app.core.errors import ConfigError
from app.providers.llm.openai_llm import OpenAILLM

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GroqLLM(OpenAILLM):
    """Groq (free tier, no card): llama-4-scout / llama-3.3-70b / llama-3.1-8b-instant ..."""
    name = "groq"

    def __init__(self, model: str, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ConfigError("Groq provider needs GROQ_API_KEY (or the role's *_API_KEY). Get one free at console.groq.com")
        super().__init__(model, api_key, base_url or GROQ_BASE_URL)


class GeminiLLM(OpenAILLM):
    """Google Gemini through its OpenAI-compatible endpoint (free tier is small and its terms exclude services aimed at minors)."""
    name = "gemini"

    def __init__(self, model: str, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ConfigError("Gemini provider needs GEMINI_API_KEY (or the role's *_API_KEY)")
        super().__init__(model, api_key, base_url or GEMINI_BASE_URL)
