from typing import List
from openai import AsyncOpenAI
from core.config import OPENAI_API_KEY

class ChatService:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

    async def get_response(self, message: str, history: List[dict]) -> str:
        if not self.client:
            return "I'm Tira, your recruiting sidekick. (Mock Mode: OpenAI Key missing)"

        try:
            # Simple context window management
            messages = [
                {"role": "system", "content": "You are Tira, a recruiting sidekick inside Hoonr. You help recruiters find candidates, shape job rubrics, score resumes, and move submissions forward. Keep replies short, specific, and action-oriented. Refer to the product as Hoonr. When a user asks about something you can actually do from the Tira panel (score a resume against a job, report a bug), point them to the right mode."}
            ]
            # Convert pydantic models to dicts if needed, or assume they are dicts
            messages.extend([{"role": h.role, "content": h.content} for h in history])
            messages.append({"role": "user", "content": message})

            model = "gpt-4o-mini"
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
            )
            
            return response.choices[0].message.content
        except Exception as e:
            return f"I'm having trouble connecting to my brain right now. ({str(e)})"

chat_service = ChatService()
