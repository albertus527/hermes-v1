"""Lightweight message debounce/buffering for Website Builder R1.

No queue service — in-memory debounce with deterministic flush.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from app.channels.telegram import NormalizedMessage


@dataclass
class MessageBuffer:
    """Collects messages for a conversation and flushes after a debounce window."""

    debounce_seconds: float = 2.0
    on_flush: Optional[Callable[[str, List[NormalizedMessage]], None]] = None
    _buffers: Dict[str, List[NormalizedMessage]] = field(default_factory=dict)
    _timers: Dict[str, threading.Timer] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, conversation_id: str, message: NormalizedMessage) -> None:
        """Add a message to the conversation buffer and reset the debounce timer."""
        with self._lock:
            if conversation_id not in self._buffers:
                self._buffers[conversation_id] = []
            self._buffers[conversation_id].append(message)

            # Cancel existing timer
            if conversation_id in self._timers:
                self._timers[conversation_id].cancel()

            # Start new timer
            timer = threading.Timer(
                self.debounce_seconds, self._flush, args=[conversation_id]
            )
            timer.daemon = True
            self._timers[conversation_id] = timer
            timer.start()

    def _flush(self, conversation_id: str) -> None:
        """Flush buffered messages for a conversation."""
        with self._lock:
            messages = self._buffers.pop(conversation_id, [])
            self._timers.pop(conversation_id, None)

        if messages and self.on_flush:
            self.on_flush(conversation_id, messages)

    def flush_now(self, conversation_id: str) -> List[NormalizedMessage]:
        """Immediately flush and return buffered messages."""
        with self._lock:
            if conversation_id in self._timers:
                self._timers[conversation_id].cancel()
                del self._timers[conversation_id]
            return self._buffers.pop(conversation_id, [])

    def pending_count(self, conversation_id: str) -> int:
        with self._lock:
            return len(self._buffers.get(conversation_id, []))
