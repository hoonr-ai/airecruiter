import os
import logging
import uuid
import sqlalchemy
from sqlalchemy import text
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

from core.config import DATABASE_URL

logger = logging.getLogger(__name__)

class UsageLogger:
    def __init__(self):
        # Use local DATABASE_URL for project-wide consistency
        self.db_url = DATABASE_URL
        self.engine = None
        if self.db_url:
            self.engine = sqlalchemy.create_engine(self.db_url)

    def log_usage(
        self, 
        service: str, 
        model: str, 
        prompt_tokens: int, 
        completion_tokens: int, 
        job_id: Optional[str] = None
    ) -> float:
        """
        Calculates cost and logs OpenAI usage to the database.
        Returns the calculated cost in USD.
        """
        # Pricing as of Mar 2024
        PRICING = {
            "gpt-4o-mini": {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
            "gpt-4o": {"input": 2.50 / 1_000_000, "output": 10.00 / 1_000_000},
            "gpt-3.5-turbo": {"input": 0.50 / 1_000_000, "output": 1.50 / 1_000_000},
            "gemini-2.0-flash": {"input": 0.0, "output": 0.0}, # Personal Free Tier
            "gemini-1.5-flash": {"input": 0.0, "output": 0.0},
        }

        # Default to gpt-4o-mini pricing if unknown
        price_config = PRICING.get(model, PRICING["gpt-4o-mini"])
        
        cost_usd = (prompt_tokens * price_config["input"]) + (completion_tokens * price_config["output"])
        total_tokens = prompt_tokens + completion_tokens

        if not self.engine:
            logger.warning(f"⚠️ UsageLogger: No DB engine. Mock Log: {service} | {model} | {total_tokens} tokens | ${cost_usd:.6f}")
            return cost_usd

        try:
            with self.engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO api_usage_logs 
                    (id, service, model, prompt_tokens, completion_tokens, total_tokens, cost_usd, job_id)
                    VALUES (:id, :service, :model, :prompt, :completion, :total, :cost, :job_id)
                """), {
                    "id": str(uuid.uuid4()),
                    "service": service,
                    "model": model,
                    "prompt": prompt_tokens,
                    "completion": completion_tokens,
                    "total": total_tokens,
                    "cost": cost_usd,
                    "job_id": job_id
                })
                conn.commit()
            
            logger.info(f"📊 Logged Usage: {service} used {total_tokens} tokens (${cost_usd:.6f})")
        except Exception as e:
            logger.error(f"❌ Failed to log usage to DB: {e}")

        return cost_usd

    def get_total_usage_summary(self):
        """Returns a rich summary from the persistent api_usage_summary table."""
        if not self.engine: return []
        try:
            with self.engine.connect() as conn:
                res = conn.execute(text("SELECT * FROM api_usage_summary ORDER BY total_cost DESC"))
                return [dict(row._mapping) for row in res.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch usage summary from table: {e}")
            return []

usage_logger = UsageLogger()
