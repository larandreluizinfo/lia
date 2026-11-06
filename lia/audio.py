"""Áudio: microfone com detecção de fala e tocador com envelope para lipsync."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

import numpy as np

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - depende do sistema
    sd = None
    _SD_ERROR = exc
else:
    _SD_ERROR = None


ENV_FPS = 100  # 10 ms por amostra de envelope


def compute_envelope(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Envelope de volume (0..1) com 100 amostras por segundo, para o lipsync.

    Usa uma escala absoluta de dB: partes baixas ficam quase fechadas e a fala
    abre bem a boca, sem depender do volume do arquivo inteiro.
    """
    if audio is None or len(audio) == 0:
        return np.zeros(1, dtype=np.float32)

    hop = max(1, sample_rate // ENV_FPS)
    if len(audio) < hop:
        audio = np.pad(audio, (0, hop - len(audio)))
    count = max(1, len(audio) // hop)
    trimmed = audio[: count * hop].reshape(count, hop).astype(np.float32)
    rms = np.sqrt(np.mean(trimmed * trimmed, axis=1) + 1e-12)
    db = 20.0 * np.log10(np.maximum(rms, 1e-7))

    floor_db, ceil_db = -52.0, -14.0
    env = np.clip((db - floor_db) / (ceil_db - floor_db), 0.0, 1.0)
    env = env ** 1.35

    # Ataque rápido e soltura suave, como uma boca de verdade.
    smooth = np.empty_like(env)
    current = 0.0
    for i, value in enumerate(env):
        coeff = 0.65 if value > current else 0.22
        current += (value - current) * coeff
        smooth[i] = current
    return smooth.astype(np.float32)


class Clip:
    __slots__ = ("audio", "text", "envelope")

    def __init__(self, audio: np.ndarray, text: str = ""):
        self.audio = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        self.text = text
        self.envelope = compute_envelope(self.audio, 24000)


class Player:
    """Toca clipes em sequência e informa o nível atual (0..1) para a boca."""

    def __init__(self, sample_rate: int = 24000, blocksize: int = 480):
        if sd is None:
            raise RuntimeError(f"sounddevice indisponível: {_SD_ERROR}")
        self.sample_rate = sample_rate
        self._queue: deque[Clip] = deque()
        self._clip: Clip | None = None
        self._pos = 0
        self._level = 0.0
        self._lock = threading.Lock()

        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            latency="low",
            callback=self._callback,
        )
        self._stream.start()

    # ------------------------------------------------------------------ API
    def enqueue(self, clip: Clip) -> None:
        # Uma pausinha no fim de cada frase deixa a fala mais natural.
        pad = np.zeros(int(0.09 * self.sample_rate), dtype=np.float32)
        clip.audio = np.concatenate([clip.audio, pad])
        clip.envelope = np.concatenate(
            [clip.envelope, np.zeros(int(0.09 * ENV_FPS), dtype=np.float32)]
        )
        with self._lock:
            self._queue.append(clip)

    def stop(self) -> None:
        with self._lock:
            self._queue.clear()
            self._clip = None
            self._pos = 0
            self._level = 0.0

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._clip is not None or self._queue)

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    # ------------------------------------------------------------- callback
    def _callback(self, outdata, frames, time_info, status):
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            offset = 0
            while offset < frames:
                if self._clip is None:
                    if not self._queue:
                        break
                    self._clip = self._queue.popleft()
                    self._pos = 0
                clip = self._clip
                remaining = len(clip.audio) - self._pos
                if remaining <= 0:
                    self._clip = None
                    continue
                take = min(frames - offset, remaining)
                out[offset : offset + take] = clip.audio[self._pos : self._pos + take]
                self._pos += take
                offset += take
                if self._pos >= len(clip.audio):
                    self._clip = None

            if self._clip is not None and len(self._clip.envelope):
                index = min(self._pos * ENV_FPS // self.sample_rate, len(self._clip.envelope) - 1)
                self._level = float(self._clip.envelope[index])
            else:
                self._level = 0.0

        outdata[:, 0] = out


class Mic:
    """Microfone contínuo com VAD simples por energia (sem dependências extras)."""

    def __init__(self, sample_rate: int = 16000, blocksize: int = 512, gain: float = 1.0):
        if sd is None:
            raise RuntimeError(f"sounddevice indisponível: {_SD_ERROR}")
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.gain = gain
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._stop_recording = threading.Event()
        self._level = 0.0
        self._level_lock = threading.Lock()
        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            latency="low",
            callback=self._callback,
        )
        self._stream.start()

    # ------------------------------------------------------------------ API
    @property
    def level(self) -> float:
        with self._level_lock:
            return self._level

    def request_stop(self) -> None:
        """Força o fim da gravação atual (ex.: apertar ESPAÇO)."""
        self._stop_recording.set()

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    # ------------------------------------------------------------- callback
    def _callback(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        if self.gain != 1.0:
            block *= self.gain
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(block)
            except queue.Empty:
                pass
        rms = float(np.sqrt(np.mean(block * block) + 1e-12))
        with self._level_lock:
            self._level = max(rms, self._level * 0.72)

    # ------------------------------------------------------------------ VAD
    def record_utterance(
        self,
        timeout: float = 60.0,
        max_duration: float = 20.0,
        silence_seconds: float = 0.8,
    ) -> np.ndarray | None:
        """Espera alguém falar e devolve o áudio float32 16 kHz da frase.

        Retorna None se ninguém falar dentro de `timeout`.
        """
        self.flush()
        self._stop_recording.clear()

        block_seconds = self.blocksize / self.sample_rate
        silence_blocks = max(3, int(silence_seconds / block_seconds))
        max_blocks = int(max_duration / block_seconds)
        deadline = time.monotonic() + timeout

        preroll: deque[np.ndarray] = deque(maxlen=8)
        captured: list[np.ndarray] = []
        noise = 0.004
        voiced_run = 0
        silence_run = 0
        speaking = False
        blocks = 0

        # Calibra o ruído de fundo com ~200 ms iniciais, se estiverem baixos.
        calibration: list[float] = []
        calibration_deadline = time.monotonic() + 0.4
        while len(calibration) < 6 and time.monotonic() < calibration_deadline:
            try:
                block = self._queue.get(timeout=0.08)
            except queue.Empty:
                break
            calibration.append(float(np.sqrt(np.mean(block * block) + 1e-12)))
            preroll.append(block)
            blocks += 1
        if calibration:
            noise = max(1e-5, min(0.01, min(calibration)))

        while True:
            if self._stop_recording.is_set():
                break
            if time.monotonic() > deadline:
                break

            try:
                block = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue

            rms = float(np.sqrt(np.mean(block * block) + 1e-12))
            if rms < noise:
                noise = 0.8 * noise + 0.2 * rms
            else:
                noise = 0.995 * noise + 0.005 * rms
            threshold = min(0.05, max(0.0065, noise * 2.8))

            if not speaking:
                preroll.append(block)
                blocks += 1
                if rms > threshold:
                    voiced_run += 1
                else:
                    voiced_run = 0
                if voiced_run >= 2:
                    speaking = True
                    captured.extend(preroll)
                    preroll.clear()
                    silence_run = 0
                elif blocks * block_seconds > timeout:
                    break
            else:
                captured.append(block)
                if rms < threshold * 0.75:
                    silence_run += 1
                else:
                    silence_run = 0
                if silence_run >= silence_blocks:
                    break
                if len(captured) >= max_blocks:
                    break

        if not speaking or not captured:
            return None

        # tira o silêncio extra do final (mantém 100 ms de respiro)
        while len(captured) > 8:
            tail = captured[-1]
            if float(np.sqrt(np.mean(tail * tail))) < 0.006:
                captured.pop()
            else:
                break

        audio = np.concatenate(captured).astype(np.float32)
        if len(audio) < int(0.2 * self.sample_rate):
            return None
        return audio
