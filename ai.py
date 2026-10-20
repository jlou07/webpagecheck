"""Évalue si une condition en langage naturel est remplie sur le texte d'une page (Azure OpenAI)."""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

MAX_PAGE_CHARS = 12000

SYSTEM_PROMPT = (
    "You analyse the visible text of a web page. You are given a condition to check. "
    "Reply only with a JSON object of the form "
    '{"probability": <number between 0 and 1>, "reason": "<short explanation in English>"}. '
    "probability is your confidence that the condition is met on the page."
)

# evaluate(condition, page_text) -> (probabilité entre 0 et 1, explication)
Evaluator = Callable[[str, str], tuple[float, str]]


def build_messages(condition: str, page_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Condition: {condition}\n\nPage text:\n{page_text[:MAX_PAGE_CHARS]}",
        },
    ]


def parse_answer(content: str) -> tuple[float, str]:
    data = json.loads(content)
    probability = float(data["probability"])
    probability = min(1.0, max(0.0, probability))
    return probability, str(data.get("reason", "")).strip()


def make_azure_openai_evaluator(
    endpoint: str,
    deployment: str,
    api_key: Optional[str] = None,
) -> Evaluator:
    """Crée un évaluateur. Sans clé API, il utilise l'identité managée ou la connexion Azure CLI."""
    from openai import AzureOpenAI

    if api_key:
        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version="2024-10-21")
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2024-10-21",
        )

    def evaluate(condition: str, page_text: str) -> tuple[float, str]:
        response = client.chat.completions.create(
            model=deployment,
            messages=build_messages(condition, page_text),
            response_format={"type": "json_object"},
            temperature=0,
        )
        return parse_answer(response.choices[0].message.content or "{}")

    return evaluate


def evaluator_from_env(env=os.environ) -> Optional[Evaluator]:
    endpoint = env.get("AZURE_OPENAI_ENDPOINT", "").strip()
    deployment = env.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    if not endpoint or not deployment:
        return None
    return make_azure_openai_evaluator(
        endpoint, deployment, env.get("AZURE_OPENAI_API_KEY", "").strip() or None
    )
