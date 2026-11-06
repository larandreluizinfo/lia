"""Aplicação principal: escuta -> transcreve -> pensa -> fala -> anima."""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
import traceback

import pygame

from .avatar import Avatar
from .audio import Clip, Mic, Player
from .config import Settings, all_providers
from .llm import LLM, LLMError
from .stt import STT
from .texto import split_sentences
from .tts import TTS, VOZES, clean_for_speech

_BRACKETED = re.compile(r"^[\[\(][^\]\)]*[\]\)]$")


class LiaApp:
    def __init__(self, settings: Settings, text_mode: bool = False):
        self.settings = settings
        self.text_mode = text_mode

        pygame.display.init()
        pygame.font.init()
        pygame.display.set_caption("Lia — conversa por voz")
        flags = pygame.RESIZABLE
        if settings.fullscreen:
            flags |= pygame.FULLSCREEN
            size = (0, 0)
        else:
            size = (settings.width, settings.height)
        self.screen = pygame.display.set_mode(size, flags)
        self.fullscreen = settings.fullscreen
        self.clock = pygame.time.Clock()
        self.avatar = Avatar(*self.screen.get_size(), scale=settings.scale)
        self.t0 = time.time()

        self.state = "boot"
        self.note = "iniciando..."
        self.user_text = ""
        self.ai_text = ""
        self.flash_text = ""
        self.flash_until = 0.0
        self.error = ""

        self.auto_listen = settings.auto_listen and not text_mode
        self.mic: Mic | None = None
        self.player: Player | None = None
        self.stt = STT(
            settings.whisper_model,
            settings.whisper_device,
            settings.whisper_compute,
            settings.language,
        )
        self.tts = TTS(settings.tts_voice, settings.tts_speed, settings.tts_lang_code)
        self.llm = LLM(settings)

        self.cancel = threading.Event()
        self.turn_active = False
        self.ready = False
        self.quitting = False
        self.next_listen = 0.0

    # ------------------------------------------------------------- utilidades
    def _set_state(self, state: str) -> None:
        if self.state != "error" or state == "error":
            self.state = state

    def _flash(self, message: str, seconds: float = 3.0) -> None:
        self.flash_text = message
        self.flash_until = time.time() + seconds

    def _current_note(self) -> str:
        if time.time() < self.flash_until:
            return self.flash_text
        return self.note

    # ---------------------------------------------------------------- startup
    def _load_models(self) -> None:
        try:
            self.note = "abrindo áudio..."
            self.player = Player()

            self.note = "carregando voz Kokoro... (primeira vez baixa ~350 MB)"
            self.tts.load()

            self.note = "carregando Whisper... (primeira vez baixa o modelo)"
            self.stt.load()

            if not self.text_mode:
                self.note = "abrindo microfone..."
                try:
                    self.mic = Mic(gain=self.settings.mic_gain)
                except Exception as exc:
                    self.text_mode = True
                    print(f"[lia] microfone indisponível: {exc}")
                    self._flash("Microfone indisponível — use o modo texto (--texto)", 8)

            self.ready = True
            self.note = ""
            self._set_state("idle")
            print("[lia] pronta.")
            if self.text_mode:
                threading.Thread(target=self._stdin_loop, daemon=True).start()
        except Exception as exc:
            self.state = "error"
            self.error = str(exc)
            self.note = f"Falha ao carregar: {exc}"
            traceback.print_exc()

    # ------------------------------------------------------------------ loops
    def run(self) -> None:
        threading.Thread(target=self._load_models, daemon=True).start()
        try:
            while not self.quitting:
                self.clock.tick(60)
                self._handle_events()
                now = time.time()

                if (
                    self.ready
                    and self.auto_listen
                    and not self.turn_active
                    and self.state == "idle"
                    and self.mic is not None
                    and now >= self.next_listen
                ):
                    self._start_voice_turn()

                level = 0.0
                if self.state == "listening" and self.mic is not None:
                    level = min(1.0, self.mic.level * 7.0) ** 0.8
                elif self.state == "speaking" and self.player is not None:
                    level = self.player.level

                self.avatar.draw(
                    self.screen,
                    state=self.state,
                    level=level,
                    user_text=self.user_text,
                    ai_text=self.ai_text,
                    note=self._current_note(),
                    model_label=self.llm.active_label,
                    t=now - self.t0,
                )
                pygame.display.flip()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self.quitting = True
        self.cancel.set()
        if self.mic is not None:
            self.mic.request_stop()
        for resource in (self.player, self.mic):
            try:
                if resource is not None:
                    resource.close()
            except Exception:
                pass
        pygame.quit()

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.quitting = True
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    if self.state in ("thinking", "speaking", "listening") and self.turn_active:
                        self.interrupt()
                    else:
                        self.quitting = True
                elif event.key == pygame.K_SPACE:
                    if self.state == "listening" and self.mic is not None:
                        self.mic.request_stop()
                    elif self.state in ("thinking", "speaking"):
                        self.interrupt()
                    elif self.state == "idle" and self.ready and not self.turn_active:
                        self.next_listen = 0.0
                        self._start_voice_turn()
                elif event.key == pygame.K_m:
                    if self.text_mode:
                        self._flash("modo texto: digite no terminal")
                    else:
                        self.auto_listen = not self.auto_listen
                        self._flash(
                            "escuta contínua ligada" if self.auto_listen else "escuta contínua desligada (espaço para falar)"
                        )
                        if not self.auto_listen and self.state == "listening" and self.mic is not None:
                            self.mic.request_stop()
                elif event.key == pygame.K_f:
                    self._toggle_fullscreen()
                elif event.key == pygame.K_c:
                    self.user_text = ""
                    self.ai_text = ""

    def _toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(
                (self.settings.width, self.settings.height), pygame.RESIZABLE
            )

    # -------------------------------------------------------------- conversa
    def _start_voice_turn(self) -> None:
        if self.turn_active or not self.ready or self.mic is None:
            return
        self.turn_active = True
        threading.Thread(target=self._voice_turn, daemon=True).start()

    def _voice_turn(self) -> None:
        try:
            self.cancel.clear()
            self._set_state("listening")
            audio = self.mic.record_utterance(
                timeout=45.0,
                max_duration=self.settings.max_utterance,
                silence_seconds=self.settings.silence_seconds,
            )
            if self.cancel.is_set() or audio is None:
                return

            self._set_state("thinking")
            text = self.stt.transcribe(audio)
            if self.cancel.is_set():
                return
            text = (text or "").strip()

            if len(text) < 2 or _BRACKETED.match(text):
                self._flash("Não entendi, pode repetir?")
                return

            self.user_text = text
            print(f"[você] {text}")
            self._respond(text)
        except Exception as exc:
            self.note = f"Erro no turno: {exc}"
            print(f"[lia] erro: {exc}")
            traceback.print_exc()
        finally:
            self.next_listen = time.time() + 0.45
            self.turn_active = False
            if self.state != "error":
                self._set_state("idle")

    def _text_turn(self, text: str) -> None:
        try:
            self.cancel.clear()
            self.user_text = text
            print(f"[você] {text}")
            self._respond(text)
        except Exception as exc:
            self.note = f"Erro no turno: {exc}"
            traceback.print_exc()
        finally:
            self.turn_active = False
            if self.state != "error":
                self._set_state("idle")

    def _respond(self, user_text: str) -> None:
        self._set_state("thinking")
        self.ai_text = ""
        if self.note.startswith(("LLM:", "Voz:", "Erro")):
            self.note = ""
        buffer = ""
        full = ""

        try:
            for token in self.llm.stream(user_text, cancel=self.cancel):
                if self.cancel.is_set():
                    break
                full += token
                buffer += token
                self.ai_text = full
                ready, buffer = split_sentences(buffer)
                for sentence in ready:
                    if self.cancel.is_set():
                        break
                    self._speak(sentence)
            if buffer.strip() and not self.cancel.is_set():
                self._speak(buffer)
        except LLMError as exc:
            print(f"[lia] LLM falhou: {exc}")
            self.note = f"LLM: {exc}"
            if not full.strip() and not self.cancel.is_set():
                self._speak("Desculpe, tive um problema para pensar agora. Pode repetir?")
        except Exception as exc:
            self.note = f"Erro: {exc}"
            traceback.print_exc()

        if full.strip():
            print(f"[lia] {full.strip()}")
            if not self.cancel.is_set():
                self.llm.commit(user_text, full)

        # espera o áudio terminar de tocar
        while self.player is not None and self.player.busy and not self.cancel.is_set():
            time.sleep(0.05)

        if self.cancel.is_set() and self.player is not None:
            self.player.stop()

    def _speak(self, sentence: str) -> None:
        clean = clean_for_speech(sentence)
        if len(clean) < 2 or self.player is None:
            return
        try:
            audio = self.tts.synthesize(clean)
        except Exception as exc:
            self.note = f"Voz: {exc}"
            traceback.print_exc()
            return
        if self.cancel.is_set():
            return
        if audio.size:
            self.player.enqueue(Clip(audio, sentence))
            self._set_state("speaking")

    def interrupt(self) -> None:
        self.cancel.set()
        if self.mic is not None:
            self.mic.request_stop()
        if self.player is not None:
            self.player.stop()
        self._set_state("idle")
        self._flash("interrompido", 1.6)

    # ----------------------------------------------------------- modo texto
    def _stdin_loop(self) -> None:
        print("\n[lia] modo texto: digite sua mensagem e pressione Enter (ou 'sair').")
        while not self.quitting:
            try:
                line = input("você> ")
            except (EOFError, OSError):
                return
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("sair", "exit", "quit"):
                self.quitting = True
                return
            if self.turn_active:
                self._flash("espere eu terminar de responder")
                continue
            self.turn_active = True
            threading.Thread(target=self._text_turn, args=(text,), daemon=True).start()


def build_settings(args) -> Settings:
    settings = Settings()
    if args.provider:
        settings.provider = args.provider
    if args.voz:
        settings.tts_voice = args.voz
    if args.escala:
        settings.scale = args.escala
    if args.fullscreen:
        settings.fullscreen = True
    if getattr(args, "janela", False):
        settings.fullscreen = False
    if args.sem_escuta:
        settings.auto_listen = False
    if getattr(args, "camera", False):
        settings.camera = True
    if getattr(args, "porta", None):
        settings.web_port = args.porta
    if getattr(args, "sem_web", False):
        settings.web = False
    if getattr(args, "sem_audio", False):
        settings.audio = False
    if getattr(args, "perguntas", None):
        from pathlib import Path

        settings.questions_file = Path(args.perguntas)
    if getattr(args, "microfone", False):
        settings.mode = "microfone"
    elif getattr(args, "texto", False):
        settings.mode = "texto"
    elif getattr(args, "totem", False):
        settings.mode = "totem"
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lia — recepção com IA para feiras e eventos")
    parser.add_argument("--totem", action="store_true", help="modo feira, sem microfone (padrão)")
    parser.add_argument("--microfone", action="store_true", help="modo voz original, com microfone e Whisper")
    parser.add_argument("--texto", action="store_true", help="conversar digitando no terminal (sem microfone)")
    parser.add_argument("--camera", action="store_true", help="cumprimentar quando detectar um rosto (webcam)")
    parser.add_argument("--porta", type=int, help="porta da página do celular (padrão: 8765)")
    parser.add_argument("--sem-web", action="store_true", help="desligar a página do celular e o QR code")
    parser.add_argument("--sem-audio", action="store_true", help="totem sem voz (só texto; não baixa o Kokoro)")
    parser.add_argument("--perguntas", help="arquivo JSON com as perguntas do evento")
    parser.add_argument("--janela", action="store_true", help="abrir em janela mesmo se LIA_FULLSCREEN=1")
    parser.add_argument("--provider", help="pollinations | ollama | groq | gemini | custom")
    parser.add_argument("--voz", help="voz do Kokoro (ex.: pf_dora, pm_alex, pm_santa)")
    parser.add_argument("--escala", type=int, choices=(1, 2, 3), help="qualidade do desenho (2 = padrão)")
    parser.add_argument("--fullscreen", action="store_true", help="iniciar em tela cheia")
    parser.add_argument("--sem-escuta", action="store_true", help="só fala ao apertar espaço")
    parser.add_argument("--vozes", action="store_true", help="listar as vozes disponíveis e sair")
    args = parser.parse_args(argv)

    if args.microfone and args.texto:
        parser.error("escolha --microfone ou --texto, não os dois")

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.vozes:
        print("Vozes do Kokoro (português do Brasil):")
        for name, description in VOZES.items():
            print(f"  {name:12s} {description}")
        return 0

    if args.provider and args.provider not in all_providers():
        parser.error(f"provedor desconhecido: {args.provider}")

    settings = build_settings(args)
    if settings.mode == "totem":
        from .kiosk import main_totem

        return main_totem(settings)

    try:
        app = LiaApp(settings, text_mode=settings.mode == "texto")
    except Exception as exc:
        print(f"Não consegui iniciar a Lia: {exc}")
        return 1
    app.run()
    return 0
