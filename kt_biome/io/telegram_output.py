"""Telegram output module.

Sends assistant text back to the originating Telegram chat. The target
chat is discovered from the ``metadata`` of the most recent user-input
event this output module has seen (see :meth:`TelegramOutput.observe_input`).

Usage in ``config.yaml``:

    output:
      type: custom
      module: kt_biome.io.telegram_output
      class_name: TelegramOutput
      options:
        token: "${TELEGRAM_BOT_TOKEN}"
        max_message_chars: 4000
        stream_edit: false
        parse_mode: MarkdownV2   # MarkdownV2 | HTML | None

Requires: ``pip install python-telegram-bot>=21``. The dependency is
OPTIONAL and imported lazily in ``start()``.

Streaming (edit-based): we ship final-only sends in this pass. The
``stream_edit`` flag is accepted for forward compatibility but
intentionally degrades to final-only delivery — edit-based streaming
needs a 500ms debounce loop with ``editMessageText`` that we will add
in a follow-up. See the final report for rationale.
"""

import asyncio
import re
from typing import Any

from kohakuterrarium.modules.output.base import BaseOutputModule
from kohakuterrarium.utils.logging import get_logger

from kt_biome.io.telegram_input import _check_sdk, expand_env_var

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# MarkdownV2 escape helper
# ---------------------------------------------------------------------------

# Telegram MarkdownV2 spec — every one of these must be escaped with '\'
# when it appears in non-code content. Code fences / inline code have
# their own rules (only '`' and '\' need escaping inside them).
_MD_V2_SPECIALS = set(r"_*[]()~`>#+-=|{}.!")


def escape_markdown_v2(text: str) -> str:
    """Escape a chunk of plain text for Telegram MarkdownV2.

    Leaves content inside triple-backtick code fences and inline
    backtick spans untouched — those have separate escape rules.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Triple-backtick code fence
        if text.startswith("```", i):
            j = text.find("```", i + 3)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i : j + 3])
            i = j + 3
            continue
        # Inline code
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j == -1:
                out.append(_escape_plain(text[i:]))
                break
            out.append(text[i : j + 1])
            i = j + 1
            continue
        out.append(_escape_plain(text[i]))
        i += 1
    return "".join(out)


def _escape_plain(segment: str) -> str:
    return "".join("\\" + ch if ch in _MD_V2_SPECIALS else ch for ch in segment)


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```[^\n`]*")


def split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    """Split ``text`` into chunks each <= ``limit`` characters.

    Preserves triple-backtick code-fence boundaries: if a hard cut
    would land *inside* a fence, we explicitly close the fence in the
    current chunk and reopen it (with the original language tag) in
    the next. Every emitted chunk therefore contains a balanced number
    of ``` markers.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        window = remaining[:limit]
        # Find where a balanced break can happen.
        fence_open = _find_open_fence(window)
        if fence_open is not None:
            # We're inside a fence at the cut point. Close it, reopen
            # in the next chunk with the same language tag.
            reserve = len("\n```")
            cut = _safe_cut(window, limit - reserve)
            chunk = remaining[:cut].rstrip()
            chunks.append(chunk + "\n```")
            remaining = fence_open + "\n" + remaining[cut:].lstrip("\n")
            continue

        cut = _safe_cut(window, limit)
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")

    return [c for c in chunks if c]


def _find_open_fence(window: str) -> str | None:
    """Return the text of the *last opened* fence in ``window`` if the
    window ends inside a fence, else None.

    Scans left-to-right, toggling state at every ``` marker.
    """
    state_open = False
    current_open: str | None = None
    for m in _FENCE_RE.finditer(window):
        if state_open:
            state_open = False
            current_open = None
        else:
            state_open = True
            current_open = m.group(0)
    return current_open if state_open else None


def _safe_cut(window: str, hard_limit: int) -> int:
    """Find the best place within ``window[:hard_limit]`` to cut.

    Prefers the last newline, falls back to the last space, then
    finally hard-cuts at ``hard_limit``.
    """
    if hard_limit <= 0:
        return len(window)
    slice_ = window[:hard_limit]
    nl = slice_.rfind("\n")
    if nl >= hard_limit // 2:
        return nl + 1
    sp = slice_.rfind(" ")
    if sp >= hard_limit // 2:
        return sp + 1
    return hard_limit


# ---------------------------------------------------------------------------
# Output module
# ---------------------------------------------------------------------------


class TelegramOutput(BaseOutputModule):
    """Final-only Telegram output module."""

    def __init__(self, options: dict[str, Any] | None = None):
        super().__init__()
        opts = options or {}
        self._token_raw: str = str(opts.get("token", ""))
        self._max_chars: int = int(opts.get("max_message_chars", 4000))
        self._stream_edit: bool = bool(opts.get("stream_edit", False))
        parse_mode = opts.get("parse_mode", "MarkdownV2")
        if isinstance(parse_mode, str) and parse_mode.lower() == "none":
            parse_mode = None
        self._parse_mode: str | None = parse_mode

        self._bot: Any = None
        self._resolved_token: str | None = None
        self._buffer: str = ""
        self._target_chat_id: int | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _on_start(self) -> None:
        if self._bot is not None:
            return
        _check_sdk()
        self._resolved_token = expand_env_var(self._token_raw)
        if not self._resolved_token:
            raise ValueError(
                "TelegramOutput: 'token' is empty. "
                "Set it to your bot token or ${ENV_VAR_NAME}."
            )
        from telegram import Bot

        self._bot = Bot(token=self._resolved_token)
        logger.info("Telegram output started")

    async def _on_stop(self) -> None:
        self._bot = None
        self._buffer = ""
        logger.info("Telegram output stopped")

    # ------------------------------------------------------------------
    # Chat-id tracking
    # ------------------------------------------------------------------

    def observe_input(self, event: Any) -> None:
        """Hook called by the framework (or by the router) with the
        most recent user-input event so the output knows where to reply.

        Falls back gracefully if ``event`` is not shaped like we expect.
        """
        try:
            metadata = (event.context or {}).get("metadata") or {}
            chat_id = metadata.get("chat_id")
            if chat_id is not None:
                self._target_chat_id = int(chat_id)
        except Exception:
            pass

    async def on_user_input(self, text: str) -> None:  # noqa: D401
        """BaseOutputModule hook — cannot carry metadata, so this is a
        no-op. The agent wiring should call :meth:`observe_input` when
        it knows the full event. See :meth:`set_target_chat_id` for a
        direct setter."""

    def set_target_chat_id(self, chat_id: int) -> None:
        """Override the reply chat id explicitly."""
        self._target_chat_id = int(chat_id)

    # ------------------------------------------------------------------
    # OutputModule protocol
    # ------------------------------------------------------------------

    async def write(self, content: str) -> None:
        self._buffer += content
        if len(self._buffer) > self._max_chars * 2:
            await self.flush()

    async def write_stream(self, chunk: str) -> None:
        # Streaming (edit-based) is disabled in this pass. Buffer and
        # send on flush. See module docstring for the rationale.
        self._buffer += chunk

    async def flush(self) -> None:
        text = self._buffer.strip()
        self._buffer = ""
        if not text:
            return
        await self._send(text)

    async def on_processing_end(self) -> None:
        await self.flush()

    # ------------------------------------------------------------------
    # Send pipeline
    # ------------------------------------------------------------------

    async def _send(self, text: str) -> None:
        if self._bot is None:
            logger.warning(
                "Telegram output send skipped — bot not started", text_len=len(text)
            )
            return
        if self._target_chat_id is None:
            logger.warning(
                "Telegram output has no target chat_id — dropping reply",
                text_len=len(text),
            )
            return

        chunks = split_for_telegram(text, self._max_chars)
        for chunk in chunks:
            payload = self._format_for_parse_mode(chunk)
            try:
                async with self._lock:
                    await self._bot.send_message(
                        chat_id=self._target_chat_id,
                        text=payload,
                        parse_mode=self._parse_mode,
                    )
            except Exception as exc:
                # Never crash the host — warn and drop this chunk.
                logger.warning(
                    "Telegram send_message failed",
                    error=str(exc),
                    chat_id=self._target_chat_id,
                    chunk_len=len(chunk),
                )

    def _format_for_parse_mode(self, chunk: str) -> str:
        if self._parse_mode and str(self._parse_mode).lower() == "markdownv2":
            return escape_markdown_v2(chunk)
        return chunk


# ---------------------------------------------------------------------------
# Env-var helper kept here as a public export for the manifest loader.
# ---------------------------------------------------------------------------

__all__ = [
    "TelegramOutput",
    "escape_markdown_v2",
    "split_for_telegram",
    "expand_env_var",
]
