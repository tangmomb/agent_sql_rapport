"""Contrats et clients des modèles de langage."""

from sql_report_agent.llm.mistral import MistralClient
from sql_report_agent.llm.openai import STANDARD_PRICING, OpenAIClient

DEFAULT_CONSTRUCT_MODEL = "gpt-6-luna"


def construct_client(model: str) -> OpenAIClient | MistralClient:
    if model in STANDARD_PRICING:
        return OpenAIClient(model)
    return MistralClient(model)
