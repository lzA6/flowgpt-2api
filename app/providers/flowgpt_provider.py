# app/providers/flowgpt_provider.py

import httpx
import json
import time
import logging
import uuid
import hashlib
import random
import string
from typing import Dict, Any, AsyncGenerator

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from app.core.config import settings
from app.providers.base_provider import BaseProvider
from app.utils.sse_utils import create_sse_data, create_chat_completion_chunk, DONE_CHUNK

logger = logging.getLogger(__name__)

class FlowGPTProvider(BaseProvider):
    BASE_URL = "https://prod-backend-k8s.flowgpt.com"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=settings.API_REQUEST_TIMEOUT)
        # --- ULTIMATE STATELESS PATCH: Robust state management for a single request stream ---
        self.buffer = ""
        self.prefix_cleaned = False
        self.checked_for_prefix = False

    def _generate_signature(self, timestamp: str, nonce: str) -> str:
        salt = settings.FLOWGPT_DEVICE_ID
        if not salt:
            raise ValueError("FLOWGPT_DEVICE_ID (salt) 未配置。")
        
        data_to_hash = f"{timestamp}{nonce}{salt}"
        signature = hashlib.md5(data_to_hash.encode()).hexdigest()
        return signature

    def _generate_nonce(self, length: int = 32) -> str:
        return ''.join(random.choices(string.hexdigits.lower(), k=length))

    async def _create_conversation_with_context(self, prompt_id: str, messages: list) -> str:
        logger.info("为新请求创建上游会话，并注入完整上下文。")
        url = f"{self.BASE_URL}/conversation/create"
        
        payload = {
            "promptId": prompt_id,
            "location": "CHAT_PAGE",
            "messages": messages
        }
        headers = self._prepare_headers()
        try:
            response = await self.client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            conversation_id = data.get("id")
            if not conversation_id:
                raise ValueError("'/conversation/create' 响应中缺少 'id' 字段。")
            
            logger.info(f"成功创建上游会话: {conversation_id}")
            return conversation_id
        except httpx.HTTPStatusError as e:
            logger.error(f"创建会话失败: {e.response.status_code} - {e.response.text}")
            raise HTTPException(status_code=e.response.status_code, detail=f"上游创建会话失败: {e.response.text}")
        except Exception as e:
            logger.error(f"创建会话时发生未知错误: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="创建上游会话时发生未知错误。")

    def _adaptive_clean_chunk(self, text_chunk: str) -> str:
        # --- ULTIMATE STATELESS PATCH: Robust buffered adaptive parser ---
        self.buffer += text_chunk
        
        separator = "Response:**"
        
        # If we haven't determined the format yet
        if not self.checked_for_prefix:
            # Check if the separator exists in the buffer
            if separator in self.buffer:
                parts = self.buffer.split(separator, 1)
                cleaned_content = parts[1].lstrip()
                self.buffer = "" # Clear buffer after processing
                self.prefix_cleaned = True
                self.checked_for_prefix = True
                return cleaned_content
            # If we've buffered enough characters and still no separator, assume pure text
            elif len(self.buffer) > 150: # Increased buffer size for more reliability
                self.prefix_cleaned = False # No prefix to clean
                self.checked_for_prefix = True
                # Release the entire buffer now
                temp_buffer = self.buffer
                self.buffer = ""
                return temp_buffer
        
        # If we have determined the format
        if self.checked_for_prefix:
            # If it was a prefixed response, we've already sent the cleaned first part
            if self.prefix_cleaned:
                return text_chunk
            # If it's a pure text response, just pass it through
            else:
                # This part is now handled by the buffer release above
                pass

        # If we are still buffering and haven't decided, return nothing yet
        return ""

    async def chat_completion(self, request_data: Dict[str, Any]) -> StreamingResponse:
        # Reset state for each new stream
        self.prefix_cleaned = False
        self.checked_for_prefix = False
        self.buffer = ""

        model_alias = request_data.get("model", "default")
        prompt_id = settings.MODEL_MAP.get(model_alias, settings.DEFAULT_MODEL_ID)
        
        messages = request_data.get("messages", [])
        if not messages:
            raise HTTPException(status_code=400, detail="请求体中必须包含 'messages' 数组。")

        conversation_id = await self._create_conversation_with_context(prompt_id, messages)
        
        url = f"{self.BASE_URL}/v3/chat"
        user_question = messages[-1].get("content")

        payload = {
            "model": "FlowGPT-Ares",
            "nsfw": False,
            "question": user_question,
            "temperature": request_data.get("temperature", 0.7),
            "userId": "",
            "promptId": prompt_id,
            "conversationId": conversation_id,
            "documentIds": [],
            "generateImage": False,
            "generateAudio": False
        }
        
        async def stream_generator() -> AsyncGenerator[bytes, None]:
            request_id = f"chatcmpl-{uuid.uuid4()}"
            try:
                headers = self._prepare_headers(is_chat=True)
                async with self.client.stream("POST", url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                            text_chunk = data.get("data", "")
                            
                            cleaned_chunk = self._adaptive_clean_chunk(text_chunk)
                            if cleaned_chunk:
                                chunk = create_chat_completion_chunk(request_id, model_alias, cleaned_chunk)
                                yield create_sse_data(chunk)

                        except json.JSONDecodeError:
                            logger.warning(f"无法解析流中的JSON行: {line}")
                            continue
                
                # Final flush of the buffer in case the stream ends before a decision was made
                if self.buffer:
                    chunk = create_chat_completion_chunk(request_id, model_alias, self.buffer)
                    yield create_sse_data(chunk)

                final_chunk = create_chat_completion_chunk(request_id, model_alias, "", "stop")
                yield create_sse_data(final_chunk)
                yield DONE_CHUNK

            except Exception as e:
                logger.error(f"处理流时发生错误: {e}", exc_info=True)
                error_message = f"内部错误: {str(e)}"
                error_chunk = create_chat_completion_chunk(request_id, model_alias, error_message, "stop")
                yield create_sse_data(error_chunk)
                yield DONE_CHUNK

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    def _prepare_headers(self, is_chat: bool = False) -> Dict[str, str]:
        if not settings.FLOWGPT_BEARER_TOKEN:
            raise HTTPException(status_code=401, detail="FLOWGPT_BEARER_TOKEN 未配置。")

        token = settings.FLOWGPT_BEARER_TOKEN
        if not token.lower().startswith('bearer '):
            auth_header_value = f"Bearer {token}"
        else:
            auth_header_value = token

        headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Authorization": auth_header_value,
            "Content-Type": "application/json",
            "Origin": "https://flowgpt.com",
            "Referer": "https://flowgpt.com/",
            "x-flow-device-id": settings.FLOWGPT_DEVICE_ID,
            "x-flow-language": "en",
        }
        
        if is_chat:
            timestamp = str(int(time.time()))
            nonce = self._generate_nonce()
            signature = self._generate_signature(timestamp, nonce)
            
            headers.update({
                "x-nonce": nonce,
                "x-timestamp": timestamp,
                "x-signature": signature,
            })
        return headers

    async def get_models(self) -> JSONResponse:
        model_data = {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": int(time.time()), "owned_by": "lzA6"}
                for name in settings.MODEL_MAP.keys()
            ]
        }
        return JSONResponse(content=model_data)
