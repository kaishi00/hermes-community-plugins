"""
Native Vision Plugin for Hermes Agent
======================================
Bypasses the auxiliary vision model and passes images directly to
vision-capable main LLMs via runtime monkey-patching.

ZERO core file modifications — all logic lives in this plugin.
"""

from __future__ import annotations

import base64
import functools
import inspect
import io
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.plugins.native_vision")

# ---------------------------------------------------------------------------
# Default configuration — hardcoded because Hermes' PluginManifest dataclass
# does not expose a ``config`` field from plugin.yaml.  These values are
# merged with whatever ``ctx.manifest.config`` may (or may not) provide.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "native_vision_enabled": True,
    "max_image_dimension": 1024,
    "max_total_image_tokens": 100000,
    "vision_models": [
        "gpt-4o",
        "claude-sonnet-4",
        "claude-opus-4",
        "glm-5v-turbo",
        "gemini-2.0-flash",
        "kimi-k2",
        "qwen3.6-plus",
        "qwen-vl",
    ],
}

# ---------------------------------------------------------------------------
# Marker used to shuttle image paths through the text pipeline
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r"\[NATIVE_VISION_IMAGES:([^\]]+)\]")

# ---------------------------------------------------------------------------
# Known-good baseline signatures for defensive version gating.
# These are *parameter names only* (excluding self) compared via
# ``inspect.signature()``.  We intentionally store them as tuples so
# a future parameter rename is caught as a mismatch.
# ---------------------------------------------------------------------------
_EXPECTED_SIGS = {
    "GatewayRunner._enrich_message_with_vision": ("user_text", "image_paths"),
    "HermesCLI._preprocess_images_with_vision": ("text", "images", "announce"),
    "AIAgent._prepare_anthropic_messages_for_api": ("api_messages",),
    "AIAgent._preprocess_anthropic_content": ("content", "role"),
}

# ---------------------------------------------------------------------------
# Try importing Pillow for image resizing
# ---------------------------------------------------------------------------
_HAS_PIL = False
try:
    from PIL import Image as PILImage

    _HAS_PIL = True
except ImportError:
    PILImage = None  # type: ignore[assignment,misc]


# =========================================================================
# Helpers
# =========================================================================

def _get_config_value(ctx, key: str, default: Any = None) -> Any:
    """Pull a value from the plugin manifest's ``config`` section.

    Falls back to ``DEFAULT_CONFIG`` because Hermes' ``PluginManifest``
    dataclass does not currently expose a ``config`` field parsed from
    ``plugin.yaml``.
    """
    cfg = getattr(ctx.manifest, "config", None) or {}
    if key in cfg:
        return cfg[key]
    return DEFAULT_CONFIG.get(key, default)


def _model_matches(agent_or_runner, vision_models: List[str]) -> bool:
    """Check whether the active model is in the vision allowlist.

    Tries multiple attribute names since GatewayRunner and AIAgent
    store the model differently.
    """
    model_name = getattr(agent_or_runner, "model", None)

    # GatewayRunner doesn't have self.model; resolve from its config object
    if not model_name:
        config = getattr(agent_or_runner, "config", None)
        if config is not None:
            # Config may be a dict or an object
            if isinstance(config, dict):
                model_cfg = config.get("model", {})
            else:
                model_cfg = getattr(config, "model", {})
            if isinstance(model_cfg, str):
                model_name = model_cfg
            elif isinstance(model_cfg, dict):
                model_name = model_cfg.get("default") or model_cfg.get("model") or ""

    # Last resort: read from the active profile's config.yaml on disk.
    # GatewayConfig (used by GatewayRunner) does not contain the model name,
    # so we must read the profile config directly.
    if not model_name:
        try:
            from hermes_constants import get_config_path
            import yaml
            with open(get_config_path(), "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, str):
                model_name = model_cfg
            elif isinstance(model_cfg, dict):
                model_name = model_cfg.get("default") or model_cfg.get("model") or ""
        except Exception:
            pass

    if not model_name:
        return False
    model_lower = model_name.lower().strip()
    for allowed in vision_models:
        if allowed.lower() in model_lower:
            return True
    return False


def _check_signature(cls, method_name: str, expected_params: tuple) -> bool:
    """Verify that *cls.method_name* has the expected parameter names.

    Returns ``True`` on match, ``False`` on mismatch (and logs a warning).
    """
    try:
        method = getattr(cls, method_name, None)
        if method is None:
            logger.warning(
                "[NATIVE-VISION] Method %s.%s not found — skipping patch.",
                cls.__name__, method_name,
            )
            return False
        sig = inspect.signature(method)
        actual = tuple(
            p.name for p in sig.parameters.values() if p.name != "self"
        )
        if actual == expected_params:
            return True
        logger.warning(
            "[NATIVE-VISION] Signature mismatch on %s.%s: expected %s, got %s. "
            "Aborting patch for this method.",
            cls.__name__, method_name,
            expected_params, actual,
        )
        return False
    except Exception as exc:
        logger.warning(
            "[NATIVE-VISION] Failed to inspect %s.%s: %s. Skipping patch.",
            cls.__name__, method_name, exc,
        )
        return False


def _resize_image(image_bytes: bytes, max_dim: int) -> bytes:
    """Resize an image so that no side exceeds *max_dim*, maintaining AR."""
    if not _HAS_PIL:
        logger.warning(
            "[NATIVE-VISION] Pillow not installed — skipping image resize. "
            "Images will be sent at original resolution."
        )
        return image_bytes
    try:
        img = PILImage.open(io.BytesIO(image_bytes))
        img.thumbnail((max_dim, max_dim), PILImage.LANCZOS)
        buf = io.BytesIO()
        fmt = img.format or "PNG"
        img.save(buf, format=fmt)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("[NATIVE-VISION] Failed to resize image: %s", exc)
        return image_bytes


def _image_to_data_url(file_path: str, max_dim: int) -> Optional[str]:
    """Read an image file, optionally resize, and return a base64 data URL."""
    try:
        raw = Path(file_path).read_bytes()
    except Exception as exc:
        logger.error(
            "[NATIVE-VISION] Failed to read image %s: %s — stripping marker.", file_path, exc,
        )
        return None

    if max_dim and max_dim > 0:
        raw = _resize_image(raw, max_dim)

    mime = mimetypes.guess_type(file_path)[0] or "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _estimate_image_tokens(data_url: str) -> int:
    """Rough token estimate: base64 payload size / 4 ≈ raw bytes / 3.5.

    This is intentionally conservative — actual tokenisation varies
    per provider but this keeps us within safe bounds.
    """
    # Extract base64 part after the comma
    b64_part = data_url.split(",", 1)[-1] if "," in data_url else ""
    byte_estimate = len(b64_part) * 3 // 4
    # Rough heuristic: ~3.5 bytes per token for images
    return max(1, byte_estimate // 4)


def _process_native_vision_images(
    user_message: str,
    max_dim: int,
    max_tokens: int,
    model_name: str,
) -> Any:
    """Detect the native-vision marker in *user_message* and convert to
    a multimodal content list suitable for OpenAI-compatible APIs.

    Returns
    -------
    str | list
        The clean text string if no marker found (pass-through), or a
        list of ``{"type": ..., ...}`` dicts if images were embedded.
    """
    match = _MARKER_RE.search(user_message)
    if not match:
        return user_message

    paths_str = match.group(1)
    image_paths = [p.strip() for p in paths_str.split("|") if p.strip()]
    if not image_paths:
        # Marker present but no paths — just strip it
        return _MARKER_RE.sub("", user_message).strip()

    clean_text = _MARKER_RE.sub("", user_message).strip()

    content_parts: List[Dict[str, Any]] = []
    if clean_text:
        content_parts.append({"type": "text", "text": clean_text})

    total_tokens = 0
    accepted_images = 0
    for path in image_paths:
        data_url = _image_to_data_url(path, max_dim)
        if data_url is None:
            continue
        tokens = _estimate_image_tokens(data_url)
        if total_tokens + tokens > max_tokens:
            logger.warning(
                "[NATIVE-VISION] Token budget exhausted at %d/%d tokens "
                "(image %s skipped). Consider increasing max_total_image_tokens.",
                total_tokens, max_tokens, path,
            )
            break
        total_tokens += tokens
        accepted_images += 1
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })

    logger.info(
        "[NATIVE-VISION] Bypassed auxiliary model for %d image(s). "
        "Model: %s. Paths: %s",
        accepted_images, model_name, image_paths,
    )

    # If no images survived processing, just return text
    if not accepted_images:
        return clean_text

    return content_parts


# =========================================================================
# Patch factories
# =========================================================================

def _make_gateway_vision_patch(config: dict):
    """Return a replacement for ``GatewayRunner._enrich_message_with_vision``
    that embeds the image paths as a marker instead of calling the
    auxiliary vision model.
    """
    vision_models: List[str] = config.get("vision_models", [])
    max_dim: int = config.get("max_image_dimension", 1024)

    async def _patched_enrich(self, user_text: str, image_paths: List[str]) -> str:
        if not _model_matches(self, vision_models):
            # Fall back to original behaviour
            return await self.__native_vision_original_enrich(user_text, image_paths)

        paths_joined = "|".join(str(p) for p in image_paths)
        marker = f"[NATIVE_VISION_IMAGES:{paths_joined}]"
        logger.info(
            "[NATIVE-VISION] Bypassed auxiliary model for %d image(s). "
            "Model: %s. Paths: %s",
            len(image_paths), getattr(self, "model", "<unknown>"), image_paths,
        )
        return f"{marker}\n{user_text}" if user_text.strip() else marker

    return _patched_enrich


def _make_cli_vision_patch(config: dict):
    """Return a replacement for ``HermesCLI._preprocess_images_with_vision``."""
    vision_models: List[str] = config.get("vision_models", [])

    def _patched_preprocess(
        self, text: str, images: list, *, announce: bool = True,
    ) -> str:
        if not _model_matches(self, vision_models):
            return self.__native_vision_original_cli_vision(
                text, images, announce=announce,
            )

        valid_paths = []
        for p in images:
            p_obj = Path(p) if not isinstance(p, Path) else p
            if p_obj.exists():
                valid_paths.append(str(p_obj))
        if not valid_paths:
            return text

        paths_joined = "|".join(valid_paths)
        marker = f"[NATIVE_VISION_IMAGES:{paths_joined}]"
        logger.info(
            "[NATIVE-VISION] Bypassed auxiliary model for %d image(s). "
            "Model: %s. Paths: %s",
            len(valid_paths), getattr(self, "model", "<unknown>"), valid_paths,
        )
        return f"{marker}\n{text}" if text.strip() else marker

    return _patched_preprocess


def _make_run_conversation_patch(config: dict):
    """Return a wrapper for ``AIAgent.run_conversation`` that intercepts
    the ``[NATIVE_VISION_IMAGES:...]`` marker and expands it into
    multimodal content.
    """
    max_dim: int = config.get("max_image_dimension", 1024)
    max_tokens: int = config.get("max_total_image_tokens", 8000)
    vision_models: List[str] = config.get("vision_models", [])

    def _patched_run_conversation(
        self,
        user_message: str,
        system_message: str = None,
        conversation_history: List[Dict[str, Any]] = None,
        task_id: str = None,
        stream_callback: Optional[callable] = None,
        persist_user_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Check if this model supports native vision
        if isinstance(user_message, str) and _MARKER_RE.search(user_message):
            if _model_matches(self, vision_models):
                model_name = getattr(self, "model", "<unknown>")
                processed = _process_native_vision_images(
                    user_message, max_dim, max_tokens, model_name,
                )
                if processed is not user_message:
                    user_message = processed
            else:
                # Model not in allowlist — strip marker, proceed text-only
                logger.info(
                    "[NATIVE-VISION] Model %s not in vision allowlist — "
                    "stripping marker, proceeding text-only.",
                    getattr(self, "model", "<unknown>"),
                )
                user_message = _MARKER_RE.sub("", user_message).strip()

        # Call original
        return self.__native_vision_original_run_conversation(
            user_message=user_message,
            system_message=system_message,
            conversation_history=conversation_history,
            task_id=task_id,
            stream_callback=stream_callback,
            persist_user_message=persist_user_message,
        )

    return _patched_run_conversation


def _make_prepare_anthropic_patch(config: dict):
    """Return a replacement for ``AIAgent._prepare_anthropic_messages_for_api``
    that passes through ``image_url`` blocks for vision-capable models.
    """
    vision_models: List[str] = config.get("vision_models", [])

    def _patched_prepare(self, api_messages: list) -> list:
        if _model_matches(self, vision_models):
            # Pass through multimodal content as-is for vision models.
            # The ``agent/anthropic_adapter.py`` handles OpenAI-style
            # ``image_url`` → Anthropic native conversion.
            return api_messages
        # Non-vision model: fall back to original behaviour (strip images).
        return self.__native_vision_original_prepare_anthro(api_messages)

    return _patched_prepare


def _make_preprocess_anthropic_content_patch(config: dict):
    """Return a replacement for ``AIAgent._preprocess_anthropic_content``
    that passes through ``image_url`` blocks for vision-capable models.
    """
    vision_models: List[str] = config.get("vision_models", [])

    def _patched_preprocess(self, content: Any, role: str) -> Any:
        if _model_matches(self, vision_models):
            # If content is not a list (i.e. plain string), just pass through
            if not isinstance(content, list):
                return content

            # Check if any part is an image_url block
            has_image = any(
                isinstance(part, dict) and part.get("type") in ("image_url", "input_image")
                for part in content
            )
            if has_image:
                # Pass through the multimodal content list as-is
                # The anthropic_adapter will handle the conversion
                return content

        # Non-vision model or no images — fall back to original behaviour
        return self.__native_vision_original_preprocess_anthro(content, role)

    return _patched_preprocess


# =========================================================================
# Registration
# =========================================================================

def register(ctx):
    """Plugin entry-point called by the Hermes plugin loader.

    Reads configuration, validates target method signatures, and applies
    monkey-patches to enable native vision.
    """
    enabled = _get_config_value(ctx, "native_vision_enabled", True)
    if not enabled:
        logger.info("[NATIVE-VISION] Plugin disabled via config — skipping all patches.")
        return

    config = {
        "native_vision_enabled": enabled,
        "max_image_dimension": int(_get_config_value(ctx, "max_image_dimension", 1024)),
        "max_total_image_tokens": int(_get_config_value(ctx, "max_total_image_tokens", 8000)),
        "vision_models": _get_config_value(ctx, "vision_models", []),
    }

    if not config["vision_models"]:
        logger.warning(
            "[NATIVE-VISION] No models listed in vision_models — "
            "plugin loaded but will not activate for any model."
        )

    logger.info(
        "[NATIVE-VISION] Registering plugin. Vision models: %s",
        config["vision_models"],
    )

    patches_applied = 0

    # ── 1. GatewayRunner._enrich_message_with_vision ──
    try:
        from gateway.run import GatewayRunner

        if _check_signature(
            GatewayRunner, "_enrich_message_with_vision",
            _EXPECTED_SIGS["GatewayRunner._enrich_message_with_vision"],
        ):
            original = GatewayRunner._enrich_message_with_vision
            replacement = _make_gateway_vision_patch(config)
            # Store reference to original on the class for fallback
            GatewayRunner.__native_vision_original_enrich = original
            GatewayRunner._enrich_message_with_vision = replacement
            patches_applied += 1
            logger.info("[NATIVE-VISION] Patched GatewayRunner._enrich_message_with_vision")
    except Exception as exc:
        logger.warning(
            "[NATIVE-VISION] Failed to patch GatewayRunner._enrich_message_with_vision: %s",
            exc,
        )

    # ── 2. HermesCLI._preprocess_images_with_vision ──
    try:
        from cli import HermesCLI

        if _check_signature(
            HermesCLI, "_preprocess_images_with_vision",
            _EXPECTED_SIGS["HermesCLI._preprocess_images_with_vision"],
        ):
            original = HermesCLI._preprocess_images_with_vision
            replacement = _make_cli_vision_patch(config)
            HermesCLI.__native_vision_original_cli_vision = original
            HermesCLI._preprocess_images_with_vision = replacement
            patches_applied += 1
            logger.info("[NATIVE-VISION] Patched HermesCLI._preprocess_images_with_vision")
    except Exception as exc:
        logger.warning(
            "[NATIVE-VISION] Failed to patch HermesCLI._preprocess_images_with_vision: %s",
            exc,
        )

    # ── 3. AIAgent.run_conversation ──
    try:
        from run_agent import AIAgent

        if _check_signature(
            AIAgent, "run_conversation",
            ("user_message", "system_message", "conversation_history",
             "task_id", "stream_callback", "persist_user_message"),
        ):
            original = AIAgent.run_conversation
            replacement = _make_run_conversation_patch(config)
            AIAgent.__native_vision_original_run_conversation = original
            AIAgent.run_conversation = replacement
            patches_applied += 1
            logger.info("[NATIVE-VISION] Patched AIAgent.run_conversation")
    except Exception as exc:
        logger.warning(
            "[NATIVE-VISION] Failed to patch AIAgent.run_conversation: %s", exc,
        )

    # ── 4a. AIAgent._prepare_anthropic_messages_for_api ──
    try:
        from run_agent import AIAgent

        if _check_signature(
            AIAgent, "_prepare_anthropic_messages_for_api",
            _EXPECTED_SIGS["AIAgent._prepare_anthropic_messages_for_api"],
        ):
            original = AIAgent._prepare_anthropic_messages_for_api
            replacement = _make_prepare_anthropic_patch(config)
            AIAgent.__native_vision_original_prepare_anthro = original
            AIAgent._prepare_anthropic_messages_for_api = replacement
            patches_applied += 1
            logger.info(
                "[NATIVE-VISION] Patched AIAgent._prepare_anthropic_messages_for_api"
            )
    except Exception as exc:
        logger.warning(
            "[NATIVE-VISION] Failed to patch AIAgent._prepare_anthropic_messages_for_api: %s",
            exc,
        )

    # ── 4b. AIAgent._preprocess_anthropic_content ──
    try:
        from run_agent import AIAgent

        if _check_signature(
            AIAgent, "_preprocess_anthropic_content",
            _EXPECTED_SIGS["AIAgent._preprocess_anthropic_content"],
        ):
            original = AIAgent._preprocess_anthropic_content
            replacement = _make_preprocess_anthropic_content_patch(config)
            AIAgent.__native_vision_original_preprocess_anthro = original
            AIAgent._preprocess_anthropic_content = replacement
            patches_applied += 1
            logger.info("[NATIVE-VISION] Patched AIAgent._preprocess_anthropic_content")
    except Exception as exc:
        logger.warning(
            "[NATIVE-VISION] Failed to patch AIAgent._preprocess_anthropic_content: %s",
            exc,
        )

    logger.info(
        "[NATIVE-VISION] Plugin loaded. %d/%d patches applied successfully.",
        patches_applied, 5,
    )
