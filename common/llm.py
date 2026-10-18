from langchain_openai import ChatOpenAI

from common.config import Settings


def make_llm(settings: Settings, temperature: float = 0.1) -> ChatOpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=temperature,
        timeout=90,
        max_retries=2,
    )
