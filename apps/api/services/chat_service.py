from typing import List
from openai import AsyncOpenAI
from core.config import OPENAI_API_KEY
from services.usage_logger import usage_logger

class ChatService:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

    async def get_response(self, message: str, history: List[dict]) -> str:
        if not self.client:
            return "I am Aria, your AI assistant. (Mock Mode: OpenAI Key missing)"

        try:
            # Simple context window management
            messages = [
                {"role": "system", "content": "You are Aria, an intelligent recruiting assistant in the Hoonr platform. You are helpful, professional, and concise. You help recruiters find candidates, create jobs, and analyze data."}
            ]
            # Convert pydantic models to dicts if needed, or assume they are dicts
            messages.extend([{"role": h.role, "content": h.content} for h in history])
            messages.append({"role": "user", "content": message})

            model = "gpt-4o-mini"
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
            )
            
            # Log Usage
            usage_logger.log_usage(
                service="aria_chat",
                model=model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens
            )
            
            return response.choices[0].message.content
        except Exception as e:
            return f"I'm having trouble connecting to my brain right now. ({str(e)})"

chat_service = ChatService()
