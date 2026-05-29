from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

client = OpenAI(
    api_key=os.getenv("PERPLEXITY_API_KEY"),
    base_url="https://api.perplexity.ai"
)

def analyze_stock(symbol):
    prompt = f"""
    Analyze {symbol} for a swing trade.

    Include:
    - bullish and bearish catalysts
    - analyst sentiment
    - AI trends
    - earnings outlook
    - market momentum
    - options flow
    - risk factors
    - overall swing trade probability

    Give a final score from 1-10.
    """

    response = client.chat.completions.create(
        model="sonar",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return response.choices[0].message.content

if __name__ == "__main__":
    result = analyze_stock("NVDA")
    print(result)
