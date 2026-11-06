"""Detecção opcional de presença por webcam (rosto), para o modo totem.

A câmera só é aberta quando o modo totem é iniciado com ``--camera``. Nenhum
quadro é gravado ou enviado: o processo lê a imagem, conta rostos e descarta o
resto. Se o OpenCV ou a webcam não estiverem disponíveis, o totem continua
funcionando normalmente (só não cumprimenta sozinho).
"""

from __future__ import annotations

import threading
import time


class Presence:
    def __init__(self, index: int = 0):
        self.index = index
        self.available = False
        self.error = ""
        self._faces = 0
        self._last_face = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lia-camera")
        self._thread.start()

    @property
    def faces(self) -> int:
        return self._faces

    def seconds_since_face(self) -> float:
        if self._last_face <= 0:
            return 1e9
        return time.time() - self._last_face

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---------------------------------------------------------------- thread
    def _loop(self) -> None:
        try:
            import cv2
        except Exception as exc:
            self.error = f"opencv indisponível ({exc.__class__.__name__})"
            print(f"[lia] câmera desativada: {self.error}")
            return

        cap = None
        try:
            backend = cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0
            cap = cv2.VideoCapture(self.index, backend)
            if not cap.isOpened() and backend:
                cap.release()
                cap = cv2.VideoCapture(self.index)
            if not cap.isOpened():
                self.error = "webcam não encontrada"
                print("[lia] câmera desativada: webcam não encontrada")
                return

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 15)

            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(cascade_path)
            if cascade.empty():
                self.error = "classificador de rosto não carregou"
                print(f"[lia] câmera desativada: {self.error}")
                return

            self.available = True
            print("[lia] câmera ativa (detecção de presença, sem gravação).")
            misses = 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    misses += 1
                    if misses > 30:
                        self.error = "a webcam parou de responder"
                        break
                    time.sleep(0.1)
                    continue
                misses = 0
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                faces = cascade.detectMultiScale(
                    gray, scaleFactor=1.15, minNeighbors=5, minSize=(90, 90)
                )
                if len(faces):
                    self._faces = len(faces)
                    self._last_face = time.time()
                else:
                    self._faces = 0
                # 8 análises por segundo são suficientes e deixam a CPU livre.
                self._stop.wait(0.12)
        except Exception as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
            print(f"[lia] câmera desativada: {self.error}")
        finally:
            self._faces = 0
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
