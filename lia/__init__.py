"""Lia - recepcionista virtual para feiras, com LLM gratuito, Kokoro-TTS e totem."""

import os

# Evita o banner do pygame e avisos barulhentos no console.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

__version__ = "2.0.0"
