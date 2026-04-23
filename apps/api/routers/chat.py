from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional
import logging

from services.chat_service import chat_service
from models import ChatRequest, ChatResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/chat", response_model=ChatResponse)
async def chat_with_tira(request: ChatRequest):
    response = await chat_service.get_response(request.message, request.history)
    return {"response": response}
