"""USD per 1M tokens (input, output). Update when OpenAI changes prices."""

PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model, PRICES["gpt-4o-mini"])
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
