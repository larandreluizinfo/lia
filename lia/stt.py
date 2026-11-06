"""Reconhecimento de fala com faster-whisper (CPU, int8)."""

from __future__ import annotations

import time

import numpy as np

from .config import MODELS_DIR


class STT:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "pt",
        on_status=None,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.on_status = on_status or (lambda msg: None)
        self._model = None
        self._load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        self.on_status(f"Carregando Whisper {self.model_size}... (primeira vez baixa o modelo)")
        started = time.time()
        from faster_whisper import WhisperModel

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
            download_root=str(MODELS_DIR / "whisper"),
        )
        self._load_seconds = time.time() - started

    def transcribe(self, audio: np.ndarray) -> str:
        """audio: float32 mono, 16 kHz."""
        if self._model is None:
            self.load()
        if audio is None or len(audio) < 1600:  # menos de 0,1 s
            return ""

        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak < 1e-4:
            return ""
        if peak > 0:
            audio = audio / max(peak, 0.25) * 0.95

        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
            initial_prompt="Conversa informal em português do Brasil.",
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
