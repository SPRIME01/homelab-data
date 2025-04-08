#!/usr/bin/env python3

import os
import sys
import json
import time
import uuid
import logging
import redis
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('mcp_handler.log')
    ]
)
logger = logging.getLogger("mcp_handler")

@dataclass
class ConversationContext:
    """Represents a conversation context for stateful inference."""
    context_id: str
    model_name: str
    created_at: float
    updated_at: float
    turn_count: int
    metadata: Dict[str, Any]
    context_data: Dict[str, Any]

class ModelContextManager:
    """Manages conversation contexts for models using Redis."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize with configuration."""
        self.config = config
        self.redis_client = self._setup_redis()
        self.ttl_seconds = config.get("context_ttl_seconds", 3600)
        self.key_prefix = config.get("context_store", {}).get("key_prefix", "mcp:")
        self.max_context_size = config.get("max_context_size", 16384)

    def _setup_redis(self) -> redis.Redis:
        """Set up Redis connection."""
        redis_config = self.config.get("context_store", {})
        return redis.Redis(
            host=redis_config.get("host", "localhost"),
            port=redis_config.get("port", 6379),
            password=redis_config.get("password", None),
            db=redis_config.get("database", 0),
            decode_responses=True,
            socket_timeout=5.0
        )

    def _get_key(self, context_id: str) -> str:
        """Get Redis key for a context ID."""
        return f"{self.key_prefix}{context_id}"

    async def create_context(self, model_name: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Create a new conversation context."""
        context_id = str(uuid.uuid4())
        now = time.time()

        context = ConversationContext(
            context_id=context_id,
            model_name=model_name,
            created_at=now,
            updated_at=now,
            turn_count=0,
            metadata=metadata or {},
            context_data={}
        )

        await self.save_context(context)
        return context_id

    async def get_context(self, context_id: str) -> Optional[ConversationContext]:
        """Retrieve a conversation context by ID."""
        try:
            key = self._get_key(context_id)
            data = self.redis_client.get(key)

            if not data:
                return None

            context_dict = json.loads(data)
            return ConversationContext(
                context_id=context_dict["context_id"],
                model_name=context_dict["model_name"],
                created_at=context_dict["created_at"],
                updated_at=context_dict["updated_at"],
                turn_count=context_dict["turn_count"],
                metadata=context_dict["metadata"],
                context_data=context_dict["context_data"]
            )
        except Exception as e:
            logger.error(f"Error retrieving context {context_id}: {e}")
            return None

    async def save_context(self, context: ConversationContext) -> bool:
        """Save conversation context."""
        try:
            context.updated_at = time.time()

            # Serialize context to JSON
            context_dict = {
                "context_id": context.context_id,
                "model_name": context.model_name,
                "created_at": context.created_at,
                "updated_at": context.updated_at,
                "turn_count": context.turn_count,
                "metadata": context.metadata,
                "context_data": context.context_data
            }

            serialized = json.dumps(context_dict)

            # Check if context exceeds max size
            if len(serialized) > self.max_context_size:
                logger.warning(f"Context {context.context_id} exceeds max size ({len(serialized)} > {self.max_context_size})")
                return False

            # Save to Redis with TTL
            key = self._get_key(context.context_id)
            self.redis_client.setex(key, self.ttl_seconds, serialized)
            return True
        except Exception as e:
            logger.error(f"Error saving context {context.context_id}: {e}")
            return False

    async def update_context(self, context_id: str,
                             user_input: str, model_output: str,
                             metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Update context with new conversation turn."""
        context = await self.get_context(context_id)
        if not context:
            logger.warning(f"Context {context_id} not found")
            return False

        # Update context
        context.turn_count += 1
        if "conversation" not in context.context_data:
            context.context_data["conversation"] = []

        context.context_data["conversation"].append({
            "turn": context.turn_count,
            "timestamp": time.time(),
            "user_input": user_input,
            "model_output": model_output
        })

        # Update metadata if provided
        if metadata:
            context.metadata.update(metadata)

        # Save updated context
        return await self.save_context(context)

    async def delete_context(self, context_id: str) -> bool:
        """Delete a conversation context."""
        try:
            key = self._get_key(context_id)
            self.redis_client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Error deleting context {context_id}: {e}")
            return False

    async def list_contexts(self, model_name: Optional[str] = None) -> List[str]:
        """List all context IDs, optionally filtered by model."""
        try:
            # Get all keys with prefix
            pattern = f"{self.key_prefix}*"
            keys = self.redis_client.keys(pattern)

            # Remove prefix
            context_ids = [key[len(self.key_prefix):] for key in keys]

            # Filter by model if specified
            if model_name:
                filtered_ids = []
                for context_id in context_ids:
                    context = await self.get_context(context_id)
                    if context and context.model_name == model_name:
                        filtered_ids.append(context_id)
                return filtered_ids

            return context_ids
        except Exception as e:
            logger.error(f"Error listing contexts: {e}")
            return []

    async def cleanup_expired_contexts(self) -> int:
        """Cleanup function to explicitly remove expired contexts."""
        # Redis handles TTL automatically, but this can be used for metadata cleanup
        return 0

# Usage example in the main handler
async def process_mcp_request(request: Dict[str, Any], context_manager: ModelContextManager) -> Dict[str, Any]:
    """Process a request with Model Context Protocol."""
    context_id = request.get("context_id")
    model_name = request.get("model_name")
    input_text = request.get("inputs", {}).get("text", "")

    # Create new context if not provided
    if not context_id:
        context_id = await context_manager.create_context(model_name)
        logger.info(f"Created new context: {context_id}")

    # Retrieve context
    context = await context_manager.get_context(context_id)
    if not context:
        # Handle missing context
        logger.warning(f"Context not found: {context_id}")
        context_id = await context_manager.create_context(model_name)
        context = await context_manager.get_context(context_id)

    # Process the request with context
    result = {
        "context_id": context_id,
        "model_name": model_name,
        "is_new_context": context.turn_count == 0,
        "turn": context.turn_count + 1
    }

    # In a real implementation, you would:
    # 1. Format the input with the context
    # 2. Send to Triton
    # 3. Process the result
    # 4. Update the context with the new turn

    # This is a placeholder
    model_output = f"Response to '{input_text}' (turn {context.turn_count + 1})"
    await context_manager.update_context(context_id, input_text, model_output)

    result["output"] = model_output
    return result

# For testing purposes
async def main():
    config = {
        "context_ttl_seconds": 3600,
        "context_store": {
            "host": "localhost",
            "port": 6379,
            "database": 0,
            "key_prefix": "mcp:"
        },
        "max_context_size": 16384
    }

    context_manager = ModelContextManager(config)

    # Test creating a context
    context_id = await context_manager.create_context("llama2-7b-chat-q4")
    print(f"Created context: {context_id}")

    # Test updating context
    success = await context_manager.update_context(
        context_id,
        "Hello, how are you?",
        "I'm doing well, thank you for asking!"
    )
    print(f"Updated context: {success}")

    # Test retrieving context
    context = await context_manager.get_context(context_id)
    print(f"Retrieved context: {context}")

    # Test deleting context
    success = await context_manager.delete_context(context_id)
    print(f"Deleted context: {success}")

if __name__ == "__main__":
    asyncio.run(main())
