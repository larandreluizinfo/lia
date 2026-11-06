"""Síntese de voz com Kokoro-TTS (24 kHz, local e gratuito)."""

from __future__ import annotations

import re
import time

import numpy as np

# Vozes do Kokoro v1.0 (multilíngue). As com prefixo "p" são português do Brasil.
VOZES = {
    "pf_dora": "Feminina · PT-BR (padrão)",
    "pm_alex": "Masculina · PT-BR",
    "pm_santa": "Masculina grave · PT-BR",
}

EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    "\U0000FE0F\U0000200D]+",
    flags=re.UNICODE,
)
MARKDOWN = re.compile(r"[*_`#>]+")


def clean_for_speech(text: str) -> str:
    """Remove o que atrapalha a leitura em voz alta."""
    text = MARKDOWN.sub("", text)
    text = EMOJI.sub("", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links markdown
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class TTS:
    SAMPLE_RATE = 24000

    def __init__(self, voice: str = "pf_dora", speed: float = 1.0, lang_code: str = "p", on_status=None):
        self.voice = voice
        self.speed = speed
        self.lang_code = lang_code
        self.on_status = on_status or (lambda msg: None)
        self._pipeline = None
        self._load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._pipeline is not None

    def load(self) -> None:
        if self._pipeline is not None:
            return
        self.on_status("Carregando voz Kokoro... (primeira vez baixa ~350 MB)")
        started = time.time()
        from kokoro import KPipeline

        try:
            self._pipeline = KPipeline(lang_code=self.lang_code, repo_id="hexgrad/Kokoro-82M")
        except TypeError:  # versões mais antigas não aceitam repo_id
            self._pipeline = KPipeline(lang_code=self.lang_code)
        self._load_seconds = time.time() - started

    def synthesize(self, text: str) -> np.ndarray:
        """Devolve áudio float32 mono a 24 kHz."""
        text = clean_for_speech(text)
        if not text:
            return np.zeros(0, dtype=np.float32)
        if self._pipeline is None:
            self.load()

        pieces: list[np.ndarray] = []
        for result in self._pipeline(text, voice=self.voice, speed=self.speed):
            audio = getattr(result, "audio", None)
            if audio is None:
                continue
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            piece = np.asarray(audio, dtype=np.float32).reshape(-1)
            if piece.size:
                pieces.append(piece)

        if not pieces:
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(pieces)
        # Fades curtos para evitar estalos.
        fade = min(int(0.006 * self.SAMPLE_RATE), len(audio) // 2)
        if fade > 0:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            audio[:fade] *= ramp
            audio[-fade:] *= ramp[::-1]
        peak = float(np.max(np.abs(audio)))
        if peak > 0.98:
            audio = audio / peak * 0.98
        return audio
