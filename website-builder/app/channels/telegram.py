"""Telegram intake adapter for Website Builder R1.

Normalizes Telegram payloads into internal messages.
No live Telegram integration without credentials — deterministic fixtures only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NormalizedMessage:
    """Normalized internal message per canonical spec §4."""

    event_id: str
    channel: str = "telegram"
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    text: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    reply_to: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)


class TelegramNormalizer:
    """Normalize Telegram webhook payloads into NormalizedMessage."""

    @staticmethod
    def normalize(payload: Dict[str, Any]) -> Optional[NormalizedMessage]:
        """Convert a Telegram Update payload to a NormalizedMessage.

        Returns None if the payload does not contain a processable message.
        """
        message = payload.get("message") or payload.get("edited_message")
        if not message:
            return None

        event_id = str(payload.get("update_id", ""))
        if not event_id:
            return None

        from_user = message.get("from", {})
        chat = message.get("chat", {})

        text = message.get("text", "") or message.get("caption", "")

        attachments: List[Dict[str, Any]] = []
        if "photo" in message:
            for photo in message["photo"]:
                attachments.append(
                    {
                        "type": "photo",
                        "file_id": photo.get("file_id"),
                        "width": photo.get("width"),
                        "height": photo.get("height"),
                    }
                )
        if "document" in message:
            doc = message["document"]
            attachments.append(
                {
                    "type": "document",
                    "file_id": doc.get("file_id"),
                    "file_name": doc.get("file_name"),
                    "mime_type": doc.get("mime_type"),
                }
            )

        reply_to = None
        if "reply_to_message" in message:
            reply = message["reply_to_message"]
            reply_to = {
                "message_id": reply.get("message_id"),
                "text": reply.get("text", "") or reply.get("caption", ""),
                "from_user_id": reply.get("from", {}).get("id"),
            }

        return NormalizedMessage(
            event_id=event_id,
            channel="telegram",
            user_id=str(from_user.get("id", "")) or None,
            conversation_id=str(chat.get("id", "")) or None,
            text=text,
            attachments=attachments,
            reply_to=reply_to,
            timestamp=float(message.get("date", time.time())),
        )
