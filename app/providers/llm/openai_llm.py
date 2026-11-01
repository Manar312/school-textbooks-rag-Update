from typing import AsyncIterator

from openai import AsyncOpenAI, BadRequestError

from app.providers.base import LLMProvider, LLMResult, StreamChunk


def token_limit_kwargs(base_url: str | None, max_tokens: int) -> dict:
    # api.openai.com expects max_completion_tokens; OpenAI-compatible servers (Groq, vLLM, Ollama, OpenRouter...) max_tokens
    return {"max_completion_tokens": max_tokens} if base_url is None else {"max_tokens": max_tokens}


def usage_of(chunk) -> tuple[int, int] | None:
    """Token usage from a stream chunk: standard `usage`, or Groq's `x_groq.usage` extension."""
    u = getattr(chunk, "usage", None)
    if u:
        return u.prompt_tokens, u.completion_tokens
    extra = getattr(chunk, "model_extra", None) or {}
    xu = (extra.get("x_groq") or {}).get("usage")
    if xu:
        return xu.get("prompt_tokens", 0), xu.get("completion_tokens", 0)
    return None


class OpenAILLM(LLMProvider):
    """OpenAI, or ANY OpenAI-compatible server (Groq, Gemini, vLLM, Ollama, LM Studio, OpenRouter...) via LLM_BASE_URL."""
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str | None = None):
        self.model = model
        self.base_url = base_url
        self.client = AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url, max_retries=4, timeout=120)

    def _msgs(self, system: str, messages: list[dict]) -> list[dict]:
        return [{"role": "system", "content": system}, *messages]

    async def complete(self, *, system: str, messages: list[dict], max_tokens: int = 1024) -> LLMResult:
        r = await self.client.chat.completions.create(model=self.model, messages=self._msgs(system, messages),
                                                      **token_limit_kwargs(self.base_url, max_tokens))
        ch = r.choices[0]
        u = r.usage
        return LLMResult((ch.message.content or "").strip(), u.prompt_tokens if u else 0,
                         u.completion_tokens if u else 0, ch.finish_reason == "length")

    async def stream(self, *, system: str, messages: list[dict], max_tokens: int = 1800) -> AsyncIterator[StreamChunk]:
        msgs = self._msgs(system, messages)
        kwargs = dict(model=self.model, messages=msgs, stream=True, **token_limit_kwargs(self.base_url, max_tokens))
        try:
            stream = await self.client.chat.completions.create(stream_options={"include_usage": True}, **kwargs)
        except BadRequestError:  # some compatible servers reject stream_options: retry without it
            stream = await self.client.chat.completions.create(**kwargs)
        usage, out_chars = None, 0
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                out_chars += len(chunk.choices[0].delta.content)
                yield StreamChunk(text=chunk.choices[0].delta.content)
            usage = usage_of(chunk) or usage
        if usage is None:  # server reported nothing: estimate (~3 chars/token) so quotas still count
            usage = (sum(len(m["content"]) for m in msgs) // 3, out_chars // 3)
        yield StreamChunk(tokens_in=usage[0], tokens_out=usage[1])

    async def aclose(self) -> None:
        await self.client.close()
