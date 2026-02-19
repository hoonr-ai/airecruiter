import os
from typing import List
from openai import AsyncOpenAI

class ChatService:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=api_key) if api_key else None

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

            response = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"I'm having trouble connecting to my brain right now. ({str(e)})"

chat_service = ChatService()
