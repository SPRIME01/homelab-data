#!/usr/bin/env python3

import os
import sys
import json
import time
import uuid
import logging
import aioredis # Changed from redis
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict # Added asdict

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
        # self.redis_client is now initialized in an async method
        self.redis_client: Optional[aioredis.Redis] = None
        self.ttl_seconds = config.get("context_ttl_seconds", 3600)
        self.key_prefix = config.get("context_store", {}).get("key_prefix", "mcp:")
        self.max_context_size = config.get("max_context_size", 16384)
        self.redis_url = self._construct_redis_url(config.get("context_store", {}))

    def _construct_redis_url(self, redis_config: Dict[str, Any]) -> str:
        """Constructs Redis URL from configuration."""
        host = redis_config.get("host", "localhost")
        port = redis_config.get("port", 6379)
        password = redis_config.get("password")
        db = redis_config.get("database", 0)
        
        if password:
            return f"redis://:{password}@{host}:{port}/{db}"
        return f"redis://{host}:{port}/{db}"

    async def connect_redis(self) -> None:
        """Connect to Redis."""
        if self.redis_client is None:
            try:
                self.redis_client = await aioredis.from_url(self.redis_url, decode_responses=True)
                logger.info(f"Connected to Redis at {self.redis_url.split('@')[-1] if '@' in self.redis_url else self.redis_url}")
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
                # Decide if this should be a fatal error for the application using this manager
                raise

    async def close_redis(self) -> None:
        """Close Redis connection."""
        if self.redis_client:
            try:
                await self.redis_client.close()
                # await self.redis_client.connection_pool.disconnect() # For older aioredis versions or specific needs
                logger.info("Redis connection closed.")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")
            finally:
                self.redis_client = None


    def _get_key(self, context_id: str) -> str:
        """Get Redis key for a context ID."""
        return f"{self.key_prefix}{context_id}"

    async def create_context(self, model_name: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Create a new conversation context."""
        if not self.redis_client: await self.connect_redis()
        assert self.redis_client is not None # For type checker

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
        if not self.redis_client: await self.connect_redis()
        assert self.redis_client is not None 

        try:
            key = self._get_key(context_id)
            data = await self.redis_client.get(key)

            if not data:
                return None

            context_dict = json.loads(data) # data is already string due to decode_responses=True
            return ConversationContext(**context_dict) # Use dataclass unpacking
        except Exception as e:
            logger.error(f"Error retrieving context {context_id}: {e}")
            return None

    async def save_context(self, context: ConversationContext) -> bool:
        """Save conversation context."""
        if not self.redis_client: await self.connect_redis()
        assert self.redis_client is not None

        try:
            context.updated_at = time.time()
            # Use asdict for dataclass serialization
            serialized = json.dumps(asdict(context))

            if len(serialized) > self.max_context_size:
                logger.warning(f"Context {context.context_id} exceeds max size ({len(serialized)} > {self.max_context_size})")
                return False

            key = self._get_key(context.context_id)
            # setex equivalent in aioredis is set with ex parameter
            await self.redis_client.set(key, serialized, ex=self.ttl_seconds)
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
            logger.warning(f"Context {context_id} not found for update")
            return False

        context.turn_count += 1
        if "conversation" not in context.context_data:
            context.context_data["conversation"] = []

        context.context_data["conversation"].append({
            "turn": context.turn_count,
            "timestamp": time.time(),
            "user_input": user_input,
            "model_output": model_output
        })

        if metadata:
            context.metadata.update(metadata)
        return await self.save_context(context)

    async def delete_context(self, context_id: str) -> bool:
        """Delete a conversation context."""
        if not self.redis_client: await self.connect_redis()
        assert self.redis_client is not None

        try:
            key = self._get_key(context_id)
            await self.redis_client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Error deleting context {context_id}: {e}")
            return False

    async def list_contexts(self, model_name: Optional[str] = None) -> List[str]:
        """List all context IDs, optionally filtered by model."""
        if not self.redis_client: await self.connect_redis()
        assert self.redis_client is not None
        
        try:
            pattern = f"{self.key_prefix}*"
            # Note: KEYS can be slow in production on large Redis instances.
            # Consider using SCAN for iterative fetching if performance becomes an issue.
            keys = await self.redis_client.keys(pattern)
            
            context_ids = [key[len(self.key_prefix):] for key in keys]

            if model_name:
                filtered_ids = []
                # This can be inefficient as it fetches each context individually.
                # Consider storing model_name in a way that allows direct filtering via Redis if needed.
                for context_id_val in context_ids:
                    context = await self.get_context(context_id_val)
                    if context and context.model_name == model_name:
                        filtered_ids.append(context_id_val)
                return filtered_ids
            return context_ids
        except Exception as e:
            logger.error(f"Error listing contexts: {e}")
            return []

    async def cleanup_expired_contexts(self) -> int:
        """Cleanup function (Redis handles TTL automatically)."""
        # Redis handles TTL automatically for keys set with EX/PX.
        # This method could be used for more complex cleanup logic if needed in the future.
        logger.debug("Redis handles TTL automatically for context keys.")
        return 0

# Example usage (modified to be async)
async def process_mcp_request(request: Dict[str, Any], context_manager: ModelContextManager) -> Dict[str, Any]:
    """Process a request with Model Context Protocol."""
    if not context_manager.redis_client: # Ensure connected
        await context_manager.connect_redis()

    context_id = request.get("context_id")
    model_name = request.get("model_name", "default_model") # Ensure model_name has a default
    input_text = request.get("inputs", {}).get("text", "")

    if not context_id:
        context_id = await context_manager.create_context(model_name)
        logger.info(f"Created new context: {context_id}")

    context = await context_manager.get_context(context_id)
    if not context: # Should not happen if create_context worked or context_id was valid
        logger.error(f"Critical: Context {context_id} not found even after create attempt.")
        # Fallback: create a new one if it's really missing
        context_id = await context_manager.create_context(model_name)
        context = await context_manager.get_context(context_id)
        if not context: # If still not found, something is very wrong
             raise Exception(f"Failed to create or retrieve context {context_id}")


    result_payload = {
        "context_id": context.context_id, # Use context.context_id for consistency
        "model_name": context.model_name,
        "is_new_context": context.turn_count == 0,
        "turn": context.turn_count + 1 
    }

    model_output = f"Async response to '{input_text}' (turn {context.turn_count + 1})"
    await context_manager.update_context(context_id, input_text, model_output)

    result_payload["output"] = model_output
    return result_payload

async def main_test(): # Renamed to avoid conflict if this file is imported
    test_config = {
        "context_ttl_seconds": 3600,
        "context_store": {
            "host": os.environ.get("REDIS_HOST", "localhost"), # Use env var for testing flexibility
            "port": int(os.environ.get("REDIS_PORT", 6379)),
            "database": 0,
            "key_prefix": "mcp_test:"
        },
        "max_context_size": 16384
    }

    context_manager = ModelContextManager(test_config)
    await context_manager.connect_redis() # Connect explicitly for test

    try:
        context_id_test = await context_manager.create_context("llama2-7b-chat-q4_test")
        print(f"Created context: {context_id_test}")

        success_update = await context_manager.update_context(
            context_id_test, "Hello, async world?", "I'm doing well, asynchronously!"
        )
        print(f"Updated context: {success_update}")

        retrieved_context = await context_manager.get_context(context_id_test)
        print(f"Retrieved context: {retrieved_context}")

        listed_contexts = await context_manager.list_contexts(model_name="llama2-7b-chat-q4_test")
        print(f"Listed contexts for model: {listed_contexts}")


        # Example of process_mcp_request usage
        sample_request = {
            "model_name": "test_model_async",
            "inputs": {"text": "Tell me a joke async"}
        }
        processed_response = await process_mcp_request(sample_request, context_manager)
        print(f"Processed MCP request response: {processed_response}")
        
        if processed_response and "context_id" in processed_response:
             success_delete = await context_manager.delete_context(processed_response["context_id"])
             print(f"Deleted processed_response context: {success_delete}")


        success_delete_test = await context_manager.delete_context(context_id_test)
        print(f"Deleted test context: {success_delete_test}")

    except Exception as e:
        logger.error(f"Test run failed: {e}", exc_info=True)
    finally:
        await context_manager.close_redis() # Close connection after test

if __name__ == "__main__":
    # Note: The main function in inference-request-handler.py would be responsible
    # for creating ModelContextManager, connecting Redis, and closing it on shutdown.
    # This main_test() is for standalone testing of mcp-handler.
    
    # Check if REDIS_HOST is set, if not, skip test to avoid error in environments without Redis
    if os.environ.get("REDIS_HOST"):
        asyncio.run(main_test())
    else:
        print("Skipping mcp-handler.py standalone test: REDIS_HOST environment variable not set.")
        # Example of how it might be integrated into inference-request-handler.py's main
        # async def main_inference_handler():
        #     config_path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
        #     handler_config = InferenceHandler._load_config(config_path) # Assuming static method or instance
            
        #     mcp_manager = None
        #     if handler_config.get("advanced_features", {}).get("model_context_protocol", {}).get("enabled", False):
        #         mcp_config = handler_config.get("advanced_features", {}).get("model_context_protocol", {})
        #         mcp_manager = ModelContextManager(mcp_config)
        #         await mcp_manager.connect_redis() # Connect manager

        #     # ... rest of inference handler setup ...
        #     # handler = InferenceHandler(config_path, mcp_manager) # Pass manager
        #     # await handler.run()

        #     if mcp_manager:
        #         await mcp_manager.close_redis() # Close manager
        # asyncio.run(main_inference_handler())
```
