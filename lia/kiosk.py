"""Modo totem de feira: recepção sem microfone.

O visitante interage de três formas:

1. toca em uma pergunta pronta (resposta instantânea, com voz em cache);
2. digita na tela ou em um teclado físico;
3. escaneia o QR code e conversa pelo próprio celular.

A personagem é desenhada em uma área própria e a interface é montada em volta
dela. O microfone e o Whisper não são carregados neste modo.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pygame

from .audio import Clip, Player
from .avatar import BG_BOTTOM, MINT, Avatar, _mix
from .config import CACHE_DIR, Settings
from .llm import LLM, LLMError
from .presence import Presence
from .texto import split_sentences
from .tts import TTS
from .webserver import WebBridge

# ------------------------------------------------------------------ paleta
TEXT = (242, 244, 252)
DIM = (154, 162, 190)
FAINT = (108, 116, 146)
LILAC = (176, 142, 255)
CARD = (18, 22, 38)
CARD_HOVER = (27, 33, 55)
RAIL = (10, 12, 22)
BUBBLE = (14, 17, 30)
LINE = (44, 51, 76)
AMBER = (255, 196, 92)
DANGER = (255, 122, 122)

STATUS_LABEL = {
    "carregando": "acendendo",
    "pronto": "pode perguntar",
    "pensando": "pensando",
    "falando": "falando",
    "erro": "instável",
}
STATUS_COLOR = {
    "carregando": FAINT,
    "pronto": MINT,
    "pensando": AMBER,
    "falando": LILAC,
    "erro": DANGER,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.lower().split())


# ------------------------------------------------------------- perguntas
@dataclass
class Question:
    titulo: str
    pergunta: str
    resposta: str


@dataclass
class QuestionBank:
    evento: str
    subtitulo: str
    boas_vindas: str
    convite: str
    perguntas: list[Question]


_FALLBACK = {
    "evento": "Recepção",
    "subtitulo": "Atendimento com inteligência artificial",
    "boas_vindas": "Olá! Eu sou a Lia, a recepcionista virtual do evento. Toque em uma pergunta para começar.",
    "convite": "Pode perguntar à vontade.",
    "perguntas": [
        {"titulo": "Quem é você?", "resposta": "Eu sou a Lia, uma recepcionista virtual. Posso responder perguntas sobre o evento e conversar com você pelo celular."},
        {"titulo": "Como funciona?", "resposta": "Eu uso inteligência artificial para entender a sua pergunta e gerar a resposta. Depois, transformo o texto em voz e animo a minha boca no ritmo do áudio."},
        {"titulo": "O que tem aqui?", "resposta": "Não tenho a programação completa, mas posso conversar sobre os expositores e as atividades. Pergunte sobre um tema específico ou procure a organização."},
        {"titulo": "Posso testar?", "resposta": "Claro! Toque em Digitar minha pergunta ou escaneie o código ao lado para usar o seu celular."},
        {"titulo": "Quem criou você?", "resposta": "Fui criada como um projeto aberto de recepção com inteligência artificial, usando ferramentas gratuitas e um computador comum."},
        {"titulo": "Fala outros idiomas?", "resposta": "Por enquanto eu falo português do Brasil. Posso tentar entender outra língua, mas a minha voz continua em português."},
    ],
}


def load_questions(path: Path) -> QuestionBank:
    data = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[lia] {path.name} não encontrado; usando perguntas padrão.")
    except Exception as exc:
        print(f"[lia] erro lendo {path.name}: {exc}; usando perguntas padrão.")

    if not isinstance(data, dict):
        data = _FALLBACK
    if not isinstance(data.get("perguntas"), list):
        data = _FALLBACK

    questions: list[Question] = []
    for item in data.get("perguntas", []):
        if not isinstance(item, dict):
            continue
        resposta = str(item.get("resposta") or "").strip()
        if not resposta:
            continue
        titulo = str(item.get("titulo") or item.get("pergunta") or "Pergunta").strip()
        pergunta = str(item.get("pergunta") or titulo).strip()
        questions.append(Question(titulo=titulo, pergunta=pergunta, resposta=resposta))
        if len(questions) == 6:
            break
    if not questions:
        return load_questions(Path("__nao_existe__"))

    return QuestionBank(
        evento=str(data.get("evento") or "Recepção").strip(),
        subtitulo=str(data.get("subtitulo") or "").strip(),
        boas_vindas=str(data.get("boas_vindas") or _FALLBACK["boas_vindas"]).strip(),
        convite=str(data.get("convite") or "").strip(),
        perguntas=questions,
    )


class KioskApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bank = load_questions(settings.questions_file)
        self.settings.system_prompt = self._totem_prompt(settings.system_prompt)

        pygame.display.init()
        pygame.font.init()
        pygame.display.set_caption("Lia — recepção")
        flags = pygame.RESIZABLE
        if settings.fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            self.fullscreen = True
        else:
            self.screen = pygame.display.set_mode(
                (settings.width, settings.height), flags
            )
            self.fullscreen = False
        self.clock = pygame.time.Clock()
        self.t0 = time.time()
        self.t = 0.0

        self._fonts: dict = {}
        self._wrap_cache: dict = {}
        self._hits: list = []
        self._layout_cache: dict | None = None
        self._layout_size = (0, 0)
        self._avatar: Avatar | None = None
        self._avatar_surf: pygame.Surface | None = None
        self._avatar_size = (0, 0)
        self._dim: pygame.Surface | None = None
        self._qr_cache: tuple | None = None
        self._qr_surf: pygame.Surface | None = None

        self.ready = False
        self.fatal = ""
        self.note = "acendendo as luzes..."
        self.warning = ""
        self._warning_shown = False
        self.status = "carregando"
        self.mode = "atrativo"  # atrativo | conversa
        self.question = ""
        self.answer = ""
        self.reveal = 1e9
        self.thinking = False
        self.preparing = False
        self.turn = 0
        self.cancel = threading.Event()
        self.quitting = False
        self.last_touch = time.time()
        self.keyboard_open = False
        self.typed = ""
        self._kb_rect = pygame.Rect(0, 0, 0, 0)
        self._pending: list[str] = []

        self.player: Player | None = None
        self.tts = TTS(settings.tts_voice, settings.tts_speed, settings.tts_lang_code)
        self.llm = LLM(settings)
        self.bridge: WebBridge | None = None
        self.presence: Presence | None = None

        self.voice_ok = False
        self.voice_error = ""
        self.tts_ok: bool | None = None
        self._voice_warned = False
        self._tts_lock = threading.Lock()
        self._audio_cache: dict[str, np.ndarray] = {}
        self._ready_at = 0.0
        self._auto_greet_done = False
        self._last_greet = 0.0
        self._presence_since: float | None = None
        self._presence_greeted = False
        self._presence_absent_since: float | None = None
        self._last_sync = 0.0
        self._flash_text = ""
        self._flash_until = 0.0

    # ------------------------------------------------------------- persona
    def _totem_prompt(self, base: str) -> str:
        evento = self.bank.evento
        extra = (
            f" Você está em um totem de recepção na {evento}. "
            "Quem fala com você está em pé, com outras pessoas esperando: responda "
            "em no máximo 2 frases curtas e diretas. Apresente-se como a Lia, "
            "recepcionista virtual do evento, e nunca diga que é um modelo de "
            "linguagem. Se não souber a programação, oriente a pessoa a procurar "
            "a organização. Nunca peça dados pessoais. Não use emojis, listas, "
            "markdown nem parênteses com ações."
        )
        return (base.strip() + extra).strip()

    # ---------------------------------------------------------- tipografia
    def _font(self, size: int, bold: bool = False):
        key = (int(size), bold)
        if key not in self._fonts:
            try:
                self._fonts[key] = pygame.font.SysFont(
                    "Segoe UI,DejaVu Sans,Arial", int(size), bold=bold
                )
            except Exception:
                self._fonts[key] = pygame.font.Font(None, int(size))
        return self._fonts[key]

    def _wrap(self, text: str, size: int, max_width: float, max_lines: int = 99, tail: bool = False):
        key = (text, int(size), int(max_width), max_lines, tail)
        cached = self._wrap_cache.get(key)
        if cached is not None:
            return cached
        if len(self._wrap_cache) > 600:
            self._wrap_cache.clear()
        font = self._font(size)
        words = str(text).replace("\n", " ").split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            if not current or font.size(candidate)[0] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) > max_lines:
            if tail:
                lines = lines[-max_lines:]
            else:
                lines = lines[:max_lines]
                last = lines[-1]
                while last and font.size(last + "…")[0] > max_width:
                    last = last[:-1]
                lines[-1] = last.rstrip() + "…"
        self._wrap_cache[key] = lines
        return lines

    def _text(self, text, x, y, size, color, bold=False, align="left", alpha=255):
        surf = self._font(size, bold).render(str(text), True, color)
        if alpha < 255:
            surf.set_alpha(alpha)
        if align == "center":
            x -= surf.get_width() / 2
        elif align == "right":
            x -= surf.get_width()
        self.screen.blit(surf, (int(x), int(y)))
        return surf

    def _panel(self, rect, color, radius=16, border=None):
        pygame.draw.rect(self.screen, color, rect, border_radius=radius)
        if border:
            pygame.draw.rect(self.screen, border, rect, width=1, border_radius=radius)

    def _button(self, rect, label, action, kind="ghost", size=14, enabled=True):
        hover = enabled and rect.collidepoint(pygame.mouse.get_pos())
        if kind == "solid":
            bg = _mix(MINT, (255, 255, 255), 0.14) if hover else MINT
            fg = (5, 31, 27)
            border = None
            if not enabled:
                bg = (35, 41, 58)
                fg = FAINT
        elif kind == "danger":
            bg = CARD_HOVER if hover else CARD
            fg = DANGER
            border = _mix(CARD, DANGER, 0.42)
        else:
            bg = CARD_HOVER if hover else BUBBLE
            fg = TEXT if enabled else FAINT
            border = _mix(LINE, MINT, 0.30) if hover else LINE
        pygame.draw.rect(self.screen, bg, rect, border_radius=rect.h // 2)
        if border:
            pygame.draw.rect(self.screen, border, rect, width=1, border_radius=rect.h // 2)
        self._text(label, rect.centerx, rect.centery - size * 0.62, size, fg, bold=True, align="center")
        if enabled and action is not None:
            self._hits.append((rect, action))

    # -------------------------------------------------------------- layout
    def _layout(self) -> dict:
        W, H = self.screen.get_size()
        if self._layout_cache is not None and self._layout_size == (W, H):
            return self._layout_cache

        u = _clamp(min(W, H) / 800.0, 0.70, 1.55)
        pad = round(26 * u)
        portrait = H > W * 1.12
        if portrait:
            hero_h = round(H * 0.47)
            hero = pygame.Rect(0, 0, W, hero_h)
            rail = pygame.Rect(0, hero_h, W, H - hero_h)
        else:
            rail_w = int(_clamp(W * 0.38, 340, 760))
            hero = pygame.Rect(0, 0, W - rail_w, H)
            rail = pygame.Rect(W - rail_w, 0, rail_w, H)

        inner_x = rail.x + pad
        inner_w = rail.w - 2 * pad
        rail_top = rail.y + round(22 * u)
        cards_top = rail_top + round(108 * u)
        footer_h = round(26 * u)
        qr_h = round(190 * u)
        qr_bottom = rail.bottom - round(pad * 0.8) - footer_h - round(8 * u)
        qr = pygame.Rect(inner_x, qr_bottom - qr_h, inner_w, qr_h)
        cards_bottom = qr.y - round(12 * u)
        cards_area_h = max(round(150 * u), cards_bottom - cards_top)

        gap = round(12 * u)
        card_w = (inner_w - gap) // 2
        card_h = max(round(70 * u), (cards_area_h - 2 * gap) // 3)
        cards: list[pygame.Rect] = []
        for index in range(len(self.bank.perguntas)):
            row, col = divmod(index, 2)
            cards.append(
                pygame.Rect(
                    inner_x + col * (card_w + gap),
                    cards_top + row * (card_h + gap),
                    card_w,
                    card_h,
                )
            )

        self._layout_cache = {
            "u": u,
            "pad": pad,
            "portrait": portrait,
            "hero": hero,
            "rail": rail,
            "railtop": rail_top,
            "innerx": inner_x,
            "innerw": inner_w,
            "cards": cards,
            "qr": qr,
            "footer_h": footer_h,
        }
        self._layout_size = (W, H)
        return self._layout_cache

    # --------------------------------------------------------------- startup
    def _load(self) -> None:
        try:
            self.note = "abrindo a página do celular..."
            if self.settings.web:
                try:
                    self.bridge = WebBridge(
                        port=self.settings.web_port,
                        event=self.bank.evento,
                        welcome=self.bank.boas_vindas,
                        questions=[q.titulo for q in self.bank.perguntas],
                        public_url=self.settings.web_url,
                    )
                except Exception as exc:
                    print(f"[lia] página do celular indisponível: {exc}")

            if self.settings.camera:
                self.presence = Presence(self.settings.camera_index)
                self.presence.start()

            if not self.settings.audio:
                print("[lia] áudio desativado: totem só com texto.")
                return

            self.note = "abrindo o áudio..."
            try:
                self.player = Player()
            except Exception as exc:
                self.voice_error = str(exc)
                self.warning = "Áudio indisponível — o totem continua funcionando por texto."
                print(f"[lia] áudio indisponível: {exc}")

            if self.player is not None:
                self.voice_ok = True
                texts = [self.bank.boas_vindas] + [q.resposta for q in self.bank.perguntas]
                cached = 0
                for text in texts:
                    audio = self._cached_audio(text)
                    if audio is not None and audio.size:
                        self._audio_cache[text] = audio
                        cached += 1
                if cached == 0:
                    # Primeira execução: prepara tudo antes de abrir, mostrando
                    # o progresso. Depois disso o totem abre em poucos segundos.
                    self._warm_voice(texts, blocking=True)
                else:
                    threading.Thread(
                        target=self._delayed_warm,
                        args=(texts,),
                        daemon=True,
                        name="lia-aquecer-voz",
                    ).start()
        except Exception as exc:
            self.fatal = f"{exc.__class__.__name__}: {exc}"
            self.note = f"falha ao iniciar: {exc}"
            print(f"[lia] falha ao iniciar o totem: {exc}")
            traceback.print_exc()
        finally:
            self.note = ""
            self.ready = True
            self._ready_at = time.time()
            print(
                "[lia] totem pronto."
                if not self.fatal
                else "[lia] totem iniciado com erros."
            )

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        threading.Thread(target=self._load, daemon=True, name="lia-load").start()
        try:
            while not self.quitting:
                dt = self.clock.tick(40) / 1000.0
                self._handle_events()
                now = time.time()
                self._update(now, dt)
                self._draw(now)
                pygame.display.flip()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self.quitting = True
        self.cancel.set()
        if self.player is not None:
            self.player.stop()
            self.player.close()
        if self.bridge is not None:
            self.bridge.close()
        if self.presence is not None:
            self.presence.close()
        pygame.quit()

    # --------------------------------------------------------------- eventos
    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.quitting = True
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._handle_click(event.pos)
            elif event.type == pygame.KEYDOWN:
                self._handle_key(event)

    def _handle_key(self, event) -> None:
        mods = event.mod
        ctrl = bool(mods & pygame.KMOD_CTRL)
        alt = bool(mods & pygame.KMOD_ALT)

        if ctrl and event.key == pygame.K_q:
            self.quitting = True
            return
        if event.key == pygame.K_F11:
            self._toggle_fullscreen()
            return
        if event.key == pygame.K_ESCAPE:
            if self.keyboard_open:
                self.keyboard_open = False
                self.typed = ""
            elif self.mode == "conversa":
                self._reset()
            return
        if event.key == pygame.K_BACKSPACE:
            if self.keyboard_open and self.typed:
                self.typed = self.typed[:-1]
                self.last_touch = time.time()
            return
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.keyboard_open:
                self._submit_typed()
            return
        if ctrl or alt:
            return

        char = event.unicode or ""
        if len(char) == 1 and char.isprintable():
            if not self.typed and char == " ":
                return
            self._type(char)

    def _handle_click(self, pos) -> None:
        self.last_touch = time.time()
        if self.keyboard_open:
            if pos[1] < self._kb_rect.y:
                self.keyboard_open = False
                self.typed = ""
                return
        for rect, action in reversed(self._hits):
            if rect.collidepoint(pos):
                self._do(action)
                return

    def _do(self, action) -> None:
        kind = action[0]
        if kind == "preset":
            self._ask_preset(action[1])
        elif kind == "open_keyboard":
            self.keyboard_open = True
            self.last_touch = time.time()
        elif kind == "close_keyboard":
            self.keyboard_open = False
            self.typed = ""
        elif kind == "key":
            self._type(action[1])
        elif kind == "backspace":
            self.typed = self.typed[:-1]
        elif kind == "space":
            if len(self.typed) < 180:
                self.typed += " "
        elif kind == "send":
            self._submit_typed()
        elif kind == "greet":
            self._greet()
        elif kind == "stop":
            self._stop()
        elif kind == "reset":
            self._reset()

    def _type(self, char: str) -> None:
        if not self.ready:
            return
        self.keyboard_open = True
        self.last_touch = time.time()
        if len(self.typed) < 180 and char.isprintable():
            self.typed += char

    def _submit_typed(self) -> None:
        text = self.typed.strip()
        self.typed = ""
        self.keyboard_open = False
        if len(text) < 2:
            self._flash("Escreva pelo menos duas letras.")
            return
        self._ask_text(text)

    def _toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(
                (self.settings.width, self.settings.height), pygame.RESIZABLE
            )
        self._layout_cache = None
        self._avatar = None
        self._avatar_surf = None
        self._dim = None

    # ------------------------------------------------------------- conversa
    def _begin_turn(self) -> tuple[int, threading.Event]:
        self.turn += 1
        self.cancel.set()
        self.cancel = threading.Event()
        if self.player is not None:
            self.player.stop()
        self.thinking = False
        self.preparing = False
        return self.turn, self.cancel

    def _ask_preset(self, index: int) -> None:
        if not self.ready or self.fatal or not (0 <= index < len(self.bank.perguntas)):
            return
        question = self.bank.perguntas[index]
        turn, _cancel = self._begin_turn()
        self.mode = "conversa"
        self.question = question.pergunta
        self.answer = question.resposta
        self.reveal = 0.0
        self.last_touch = time.time()
        self.keyboard_open = False
        self.typed = ""
        self.llm.commit(question.pergunta, question.resposta)
        self._play_text(question.resposta, turn)

    def _ask_text(self, text: str) -> None:
        text = " ".join(str(text).split()).strip()[:300]
        if not self.ready or self.fatal or len(text) < 2:
            return
        wanted = _normalize(text)
        for index, question in enumerate(self.bank.perguntas):
            if wanted in (_normalize(question.titulo), _normalize(question.pergunta)):
                self._ask_preset(index)
                return

        turn, cancel = self._begin_turn()
        self.mode = "conversa"
        self.question = text
        self.answer = ""
        self.reveal = 0.0
        self.thinking = True
        self.last_touch = time.time()
        self.keyboard_open = False
        self.typed = ""
        threading.Thread(
            target=self._free_turn, args=(text, turn, cancel), daemon=True, name="lia-turno"
        ).start()

    def _free_turn(self, text: str, turn: int, cancel: threading.Event) -> None:
        full = ""
        buffer = ""
        try:
            for token in self.llm.stream(text, cancel=cancel):
                if cancel.is_set() or self.turn != turn:
                    return
                full += token
                self.answer = full
                ready, buffer = split_sentences(buffer)
                for sentence in ready:
                    if cancel.is_set() or self.turn != turn:
                        return
                    self._speak_sentence(sentence, cancel)
            if buffer.strip() and not cancel.is_set() and self.turn == turn:
                self._speak_sentence(buffer, cancel)
        except LLMError as exc:
            print(f"[lia] LLM falhou: {exc}")
            if self.turn == turn and not cancel.is_set():
                apology = "Desculpe, não consegui pensar agora. Pode tentar de novo em instantes?"
                self.answer = apology
                self.reveal = 0.0
                self._speak_sentence(apology, cancel)
                self._flash("A internet ou o provedor de IA falhou. Tente outra pergunta.", 7)
        except Exception as exc:
            print(f"[lia] erro no turno: {exc}")
            traceback.print_exc()
            if self.turn == turn and not cancel.is_set():
                self.answer = "Tive um problema para responder. Pode tentar mais uma vez?"
                self.reveal = 0.0
        finally:
            if self.turn == turn:
                self.thinking = False
                if full.strip() and not cancel.is_set():
                    self.llm.commit(text, full.strip())

    def _speak_sentence(self, sentence: str, cancel: threading.Event) -> None:
        text = sentence.strip()
        if not text or not self.voice_ok or self.player is None:
            return
        audio = self._audio_cache.get(text)
        if audio is None:
            audio = self._cached_audio(text)
        if audio is None:
            if self.tts_ok is False:
                return
            try:
                audio = self._synth(text)
            except Exception as exc:
                if not self._voice_warned:
                    self._voice_warned = True
                    print(f"[lia] voz falhou: {exc}")
                return
        if audio is None or not audio.size or cancel.is_set():
            return
        self._audio_cache[text] = audio
        if not cancel.is_set():
            self.player.enqueue(Clip(audio, text))

    def _play_text(self, text: str, turn: int) -> None:
        if not self.voice_ok or self.player is None:
            self.reveal = float(len(text))
            return
        audio = self._audio_cache.get(text) or self._cached_audio(text)
        if audio is not None and audio.size:
            self._audio_cache[text] = audio
            self.player.enqueue(Clip(audio, text))
            return
        if self.tts_ok is False:
            self.reveal = float(len(text))
            return
        self.preparing = True
        threading.Thread(
            target=self._prepare_audio, args=(text, turn), daemon=True, name="lia-voz"
        ).start()

    def _prepare_audio(self, text: str, turn: int) -> None:
        audio = None
        try:
            audio = self._cached_audio(text) or self._synth(text)
        except Exception as exc:
            if not self._voice_warned:
                self._voice_warned = True
                print(f"[lia] voz falhou: {exc}")
        finally:
            if self.turn == turn:
                self.preparing = False
        if audio is not None and audio.size and self.turn == turn and not self.cancel.is_set():
            self._audio_cache[text] = audio
            self.player.enqueue(Clip(audio, text))

    def _greet(self) -> None:
        if not self.ready or self.fatal or self.mode != "atrativo":
            return
        turn, _cancel = self._begin_turn()
        self.answer = self.bank.boas_vindas
        self.question = ""
        self.reveal = 0.0
        self.last_touch = time.time()
        self._last_greet = time.time()
        self._play_text(self.answer, turn)

    def _stop(self) -> None:
        self.cancel.set()
        if self.player is not None:
            self.player.stop()
        self.thinking = False
        self.preparing = False
        self.reveal = float(len(self._display_answer()))
        if self.mode == "conversa" and not self.answer.strip():
            self.answer = "Tudo bem. Pode escolher outra pergunta."
        self.last_touch = time.time()

    def _reset(self) -> None:
        self.cancel.set()
        if self.player is not None:
            self.player.stop()
        self.turn += 1
        self.thinking = False
        self.preparing = False
        self.mode = "atrativo"
        self.question = ""
        self.answer = ""
        self.reveal = 1e9
        self.keyboard_open = False
        self.typed = ""
        self._pending.clear()
        self.llm.reset()
        self.last_touch = time.time()

    def _flash(self, message: str, seconds: float = 4.0) -> None:
        self._flash_text = message
        self._flash_until = time.time() + seconds

    # ------------------------------------------------------------ voz/cache
    def _delayed_warm(self, texts: list[str]) -> None:
        # Deixa o totem abrir e respirar antes de ocupar a CPU com o Kokoro.
        time.sleep(6.0)
        if not self.quitting:
            self._warm_voice(texts, blocking=False)

    def _warm_voice(self, texts: list[str], *, blocking: bool) -> None:
        if blocking:
            self.note = "carregando a voz Kokoro... (primeira vez baixa ~350 MB)"
        try:
            with self._tts_lock:
                self.tts.load()
            self.tts_ok = True
        except Exception as exc:
            self.tts_ok = False
            self.voice_error = str(exc)
            self.warning = "Não consegui carregar a voz. O totem continua por texto."
            print(f"[lia] voz indisponível: {exc}")
            traceback.print_exc()
            return

        total = len(texts)
        for index, text in enumerate(texts):
            if text in self._audio_cache:
                continue
            if blocking:
                self.note = f"preparando a voz {index + 1}/{total}..."
            try:
                audio = self._synth(text)
                if audio is not None and audio.size:
                    self._audio_cache[text] = audio
            except Exception as exc:
                self.warning = (
                    "Algumas vozes não puderam ser preparadas. "
                    "O totem continua funcionando."
                )
                print(f"[lia] falha preparando voz: {exc}")
                break

    def _cache_path(self, text: str) -> Path:
        raw = (
            f"{self.settings.tts_voice}|{self.settings.tts_speed:.3f}|{text}"
        ).encode("utf-8")
        return CACHE_DIR / "voz" / f"{hashlib.sha1(raw).hexdigest()}.wav"

    def _cached_audio(self, text: str) -> np.ndarray | None:
        path = self._cache_path(text)
        if not path.exists():
            return None
        try:
            import soundfile as sf

            data, rate = sf.read(str(path), dtype="float32", always_2d=False)
            if rate != TTS.SAMPLE_RATE or not len(data):
                return None
            if getattr(data, "ndim", 1) > 1:
                data = data.mean(axis=1)
            return np.ascontiguousarray(data, dtype=np.float32)
        except Exception:
            return None

    def _synth(self, text: str) -> np.ndarray:
        with self._tts_lock:
            if self.tts_ok is False:
                return np.zeros(0, dtype=np.float32)
            try:
                audio = self.tts.synthesize(text)
            except Exception:
                if not self.tts.loaded:
                    self.tts_ok = False
                raise
            self.tts_ok = True
        if audio is None or not audio.size:
            return np.zeros(0, dtype=np.float32)
        try:
            import soundfile as sf

            path = self._cache_path(text)
            path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(path), audio, TTS.SAMPLE_RATE, subtype="PCM_16")
        except Exception:
            pass
        return audio

    # --------------------------------------------------------------- update
    def _display_answer(self) -> str:
        if self.answer:
            return self.answer
        if self.mode == "conversa":
            return "Um instante..."
        return self.bank.boas_vindas

    def _compute_status(self) -> str:
        if not self.ready:
            return "carregando"
        if self.fatal:
            return "erro"
        if self.thinking or self.preparing:
            return "pensando"
        if self.player is not None and self.player.busy:
            return "falando"
        return "pronto"

    def _busy(self) -> bool:
        if self.thinking or self.preparing:
            return True
        if self.player is not None and self.player.busy:
            return True
        target = self._display_answer()
        return bool(target) and self.reveal < len(target) - 1

    def _update(self, now: float, dt: float) -> None:
        self.t = now - self.t0
        self.status = self._compute_status()

        target = self._display_answer()
        if self.reveal < len(target):
            self.reveal = min(len(target), self.reveal + dt * 85.0)

        if self.ready and self.warning and not self._warning_shown:
            self._warning_shown = True
            self._flash(self.warning, 9.0)

        if self.bridge is not None:
            queued = self.bridge.take_messages() if self.ready else []
            for message in queued:
                self.last_touch = now
                self._auto_greet_done = True
                self._ask_text(message)

        camera_ready = self.presence is not None and self.presence.available
        if (
            self.ready
            and self.voice_ok
            and self.settings.auto_greet
            and not self._auto_greet_done
            and not self.fatal
            and self.mode == "atrativo"
            and not self._busy()
            and not camera_ready
            and now - self._ready_at > 1.8
        ):
            self._auto_greet_done = True
            self._greet()

        if (
            self.presence is not None
            and self.presence.available
            and self.voice_ok
            and self.mode == "atrativo"
            and not self.keyboard_open
            and not self._busy()
        ):
            if self.presence.faces > 0:
                self._presence_absent_since = None
                if self._presence_since is None:
                    self._presence_since = now
                elif (
                    not self._presence_greeted
                    and now - self._presence_since > 1.2
                    and now - self._last_greet > 12.0
                ):
                    self._presence_greeted = True
                    self._presence_since = None
                    self._auto_greet_done = True
                    self._greet()
            else:
                self._presence_since = None
                if self._presence_absent_since is None:
                    self._presence_absent_since = now
                elif now - self._presence_absent_since > 3.0:
                    self._presence_greeted = False

        if (
            self.mode == "conversa"
            and not self._busy()
            and not self.keyboard_open
            and now - self.last_touch > self.settings.inactivity
        ):
            self._reset()

        if self.bridge is not None and now - self._last_sync > 0.4:
            self._last_sync = now
            self.bridge.update(
                estado=self.mode,
                status=self.status,
                pergunta=self.question,
                resposta=self._display_answer(),
            )

    # -------------------------------------------------------------- desenho
    def _draw(self, now: float) -> None:
        self._hits = []
        self.status = self._compute_status()
        self.screen.fill(BG_BOTTOM)
        layout = self._layout()
        self._draw_avatar(layout)
        self._draw_rail_bg(layout)
        self._draw_brand(layout)
        self._draw_rail(layout)
        self._draw_bubble(layout)
        if self.keyboard_open:
            self._draw_keyboard(layout)
        self._draw_toast(layout)

    def _draw_avatar(self, layout) -> None:
        hero: pygame.Rect = layout["hero"]
        vw = max(320, int(hero.w * 1.28))
        vh = max(240, int(hero.h * 1.0))
        if self._avatar is None or self._avatar_size != (vw, vh):
            self._avatar = Avatar(vw, vh, scale=self.settings.scale)
            self._avatar_surf = pygame.Surface((vw, vh)).convert()
            self._avatar_size = (vw, vh)
        assert self._avatar_surf is not None

        offx = hero.centerx - self._avatar.cx
        offy = hero.y + hero.h * 0.30 - self._avatar.cy
        mouse = pygame.mouse.get_pos()
        self._avatar.pointer = (mouse[0] - offx, mouse[1] - offy)

        if not self.ready:
            state = "boot"
        elif self.status == "falando":
            state = "speaking"
        elif self.status == "pensando":
            state = "thinking"
        elif self.status == "erro":
            state = "error"
        else:
            state = "idle"
        level = 0.0
        if state == "speaking" and self.player is not None:
            level = self.player.level
        self._avatar.draw(self._avatar_surf, state=state, level=level, t=self.t, chrome=False)
        self.screen.subsurface(hero).blit(self._avatar_surf, (offx, offy))

    def _draw_rail_bg(self, layout) -> None:
        rail: pygame.Rect = layout["rail"]
        pygame.draw.rect(self.screen, RAIL, rail)
        seam = (37, 43, 66)
        if layout["portrait"]:
            pygame.draw.line(self.screen, seam, (0, rail.y), (rail.w, rail.y), 2)
            pygame.draw.line(
                self.screen, MINT, (0, rail.y), (round(80 * layout["u"]), rail.y), 2
            )
        else:
            pygame.draw.line(self.screen, seam, (rail.x, 0), (rail.x, rail.h), 2)
            pygame.draw.line(
                self.screen, MINT, (rail.x, 0), (rail.x, round(70 * layout["u"])), 2
            )

    def _draw_brand(self, layout) -> None:
        hero: pygame.Rect = layout["hero"]
        u = layout["u"]
        pad = layout["pad"]
        x = pad + round(4 * u)
        y = round(18 * u)
        word_size = round(34 * u)
        self._text("LIA", x, y, word_size, TEXT, bold=True)
        width = self._font(word_size, True).size("LIA")[0]
        pulse = 0.5 + 0.5 * math.sin(self.t * 3.0)
        color = STATUS_COLOR.get("carregando" if not self.ready else self.status, FAINT)
        pygame.draw.circle(
            self.screen,
            color,
            (int(x + width + round(12 * u)), int(y + word_size * 0.38)),
            max(2, round(4.2 * u)),
        )
        self._text(
            "recepção virtual",
            x + 1,
            y + word_size + round(6 * u),
            round(11.5 * u),
            MINT,
            bold=True,
        )
        event_lines = self._wrap(
            self.bank.evento, round(13 * u), hero.w - 2 * pad, max_lines=1
        )
        if event_lines:
            self._text(
                event_lines[0],
                x,
                y + word_size + round(26 * u),
                round(13 * u),
                DIM,
            )

    def _draw_rail(self, layout) -> None:
        u = layout["u"]
        pad = layout["pad"]
        x = layout["innerx"]
        rail: pygame.Rect = layout["rail"]
        top = layout["railtop"]

        tag_lines = self._wrap(
            self.bank.evento.upper(), round(10.5 * u), layout["innerw"], max_lines=1
        )
        if tag_lines:
            self._text(tag_lines[0], x, top, round(10.5 * u), MINT, bold=True)
        self._text(
            "Pergunte à Lia", x, top + round(18 * u), round(29 * u), TEXT, bold=True
        )
        subtitle = (
            self.bank.subtitulo
            or "Toque em uma pergunta, digite ou use o celular."
        )
        sub_lines = self._wrap(subtitle, round(13 * u), layout["innerw"], max_lines=2)
        sy = top + round(58 * u)
        for line in sub_lines:
            self._text(line, x, sy, round(13 * u), DIM)
            sy += round(18 * u)

        if self.ready:
            self._draw_cards(layout)
        else:
            self._draw_progress(layout)
        self._draw_qr_card(layout)

        footer = "sem microfone · toque na tela ou use o celular"
        self._text(
            footer,
            rail.centerx,
            rail.bottom - layout["footer_h"] + round(2 * u),
            round(10.5 * u),
            FAINT,
            align="center",
        )

    def _draw_cards(self, layout) -> None:
        u = layout["u"]
        mouse = pygame.mouse.get_pos()
        for index, (rect, question) in enumerate(
            zip(layout["cards"], self.bank.perguntas)
        ):
            hover = (
                self.ready
                and not self.keyboard_open
                and rect.collidepoint(mouse)
            )
            self._panel(
                rect,
                CARD_HOVER if hover else CARD,
                radius=round(17 * u),
                border=_mix(LINE, MINT, 0.38) if hover else LINE,
            )
            number_size = round(10.5 * u)
            self._text(
                f"{index + 1:02d}",
                rect.right - round(14 * u),
                rect.y + round(10 * u),
                number_size,
                MINT if hover else FAINT,
                bold=True,
                align="right",
            )
            title_size = round(18.5 * u)
            lines = self._wrap(
                question.titulo, title_size, rect.w - round(26 * u), max_lines=2
            )
            ty = rect.y + round(13 * u)
            for line in lines:
                self._text(line, rect.x + round(13 * u), ty, title_size, TEXT, bold=True)
                ty += round(21.5 * u)
            if rect.h >= round(78 * u):
                teaser = self._wrap(
                    question.resposta,
                    round(10.5 * u),
                    rect.w - round(40 * u),
                    max_lines=1,
                )
                if teaser:
                    self._text(
                        teaser[0],
                        rect.x + round(13 * u),
                        rect.bottom - round(25 * u),
                        round(10.5 * u),
                        FAINT,
                    )
            if rect.h >= round(72 * u):
                self._text(
                    "›",
                    rect.right - round(15 * u),
                    rect.bottom - round(34 * u),
                    round(23 * u),
                    MINT if hover else FAINT,
                    bold=True,
                    align="right",
                )
            if self.ready:
                self._hits.append((rect, ("preset", index)))

    def _draw_progress(self, layout) -> None:
        u = layout["u"]
        area = pygame.Rect(
            layout["innerx"],
            layout["railtop"] + round(108 * u),
            layout["innerw"],
            layout["qr"].y - round(12 * u) - (layout["railtop"] + round(108 * u)),
        )
        self._panel(area, CARD, radius=round(18 * u), border=LINE)
        title = "Não consegui acender" if self.fatal else "Acendendo a Lia"
        color = DANGER if self.fatal else TEXT
        title_size = round(20 * u)
        self._text(
            title,
            area.x + round(18 * u),
            area.y + round(18 * u),
            title_size,
            color,
            bold=True,
        )
        if not self.fatal:
            cx = area.right - round(32 * u)
            cy = area.y + round(18 * u) + title_size * 0.55
            radius = round(10 * u)
            pygame.draw.arc(
                self.screen,
                MINT,
                pygame.Rect(cx - radius, cy - radius, radius * 2, radius * 2),
                self.t * 3.0,
                self.t * 3.0 + 2.2,
                width=max(2, round(2.5 * u)),
            )
        note = self.fatal or self.note or "preparando..."
        lines = self._wrap(note, round(12.5 * u), area.w - round(36 * u), max_lines=4)
        ny = area.y + round(52 * u)
        for line in lines:
            self._text(line, area.x + round(18 * u), ny, round(12.5 * u), DIM)
            ny += round(17 * u)
        if not self.fatal:
            tip = self._wrap(
                "Na primeira vez, a voz baixa cerca de 350 MB. Depois disso, tudo abre em segundos.",
                round(10.5 * u),
                area.w - round(36 * u),
                max_lines=3,
            )
            ty = area.bottom - round(14 * u) - len(tip) * round(14 * u)
            for line in tip:
                self._text(line, area.x + round(18 * u), ty, round(10.5 * u), FAINT)
                ty += round(14 * u)
        else:
            self._text(
                "Veja o terminal para mais detalhes.",
                area.x + round(18 * u),
                area.bottom - round(30 * u),
                round(10.5 * u),
                FAINT,
            )

    def _draw_qr_card(self, layout) -> None:
        u = layout["u"]
        rect: pygame.Rect = layout["qr"]
        pad = round(16 * u)
        self._panel(rect, CARD, radius=round(19 * u), border=LINE)

        size = int(min(rect.h - 2 * pad, 174 * u))
        qx = rect.x + pad
        qy = rect.centery - size // 2
        if self.bridge is not None and self.bridge.available:
            surf = self._qr_surface(size)
            if surf is not None:
                self.screen.blit(surf, (qx, qy))
        else:
            pygame.draw.rect(
                self.screen,
                (24, 29, 48),
                pygame.Rect(qx, qy, size, size),
                border_radius=round(8 * u),
            )
            self._text(
                "sem",
                qx + size // 2,
                qy + size // 2 - round(18 * u),
                round(12 * u),
                FAINT,
                align="center",
            )
            self._text(
                "celular",
                qx + size // 2,
                qy + size // 2 + round(2 * u),
                round(12 * u),
                FAINT,
                align="center",
            )

        tx = qx + size + round(17 * u)
        tw = rect.right - pad - tx
        if tw < round(120 * u):
            tw = rect.right - pad - tx

        title_size = round(16 * u)
        title = self._wrap(
            "Converse pelo celular", title_size, tw, max_lines=2
        )
        ty = rect.y + pad
        for line in title:
            self._text(line, tx, ty, title_size, TEXT, bold=True)
            ty += round(19 * u)

        hint = "Aponte a câmera para o código e digite sua pergunta."
        if not self.settings.web:
            hint = "A página do celular está desativada neste totem."
        elif self.bridge is None:
            hint = "A página do celular não conseguiu abrir."
        hint_lines = self._wrap(hint, round(10.5 * u), tw, max_lines=2)
        hy = ty + round(2 * u)
        for line in hint_lines:
            self._text(line, tx, hy, round(10.5 * u), DIM)
            hy += round(14 * u)

        if self.bridge is not None and self.bridge.available:
            url_lines = self._wrap(self.bridge.url, round(9.5 * u), tw, max_lines=1)
            if url_lines:
                self._text(url_lines[0], tx, hy + round(2 * u), round(9.5 * u), MINT)

        button_h = round(34 * u)
        button = pygame.Rect(
            tx, rect.bottom - pad - button_h, max(round(96 * u), tw), button_h
        )
        self._button(
            button,
            "Digitar pergunta",
            ("open_keyboard",),
            kind="solid",
            size=round(12.5 * u),
            enabled=self.ready,
        )

    def _qr_surface(self, size: int) -> pygame.Surface | None:
        if self.bridge is None or not self.bridge.available:
            return None
        key = (self.bridge.url, size)
        if self._qr_cache == key and self._qr_surf is not None:
            return self._qr_surf
        try:
            import qrcode

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=1,
                border=2,
            )
            qr.add_data(self.bridge.url)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except Exception as exc:
            print(f"[lia] QR code falhou: {exc}")
            return None

        modules = len(matrix)
        cell = max(1, size // modules)
        real = cell * modules
        surf = pygame.Surface((size, size)).convert()
        surf.fill((252, 253, 255))
        offset = (size - real) // 2
        for row, values in enumerate(matrix):
            for col, filled in enumerate(values):
                if filled:
                    pygame.draw.rect(
                        surf,
                        (9, 13, 25),
                        (offset + col * cell, offset + row * cell, cell, cell),
                    )
        self._qr_cache = key
        self._qr_surf = surf
        return surf

    def _draw_bubble(self, layout) -> None:
        hero: pygame.Rect = layout["hero"]
        u = layout["u"]
        pad = layout["pad"]
        inner_pad = round(20 * u)
        bubble_w = hero.w - 2 * pad
        text_w = bubble_w - 2 * inner_pad
        answer_size = round(20 * u)
        line_h = round(26 * u)
        question = self.mode == "conversa" and self.question
        display = self._display_answer()
        visible = display[: int(max(0, self.reveal))] if self.reveal < len(display) else display
        if self.mode == "conversa" and not self.answer:
            visible = display

        label_h = round(17 * u)
        q_h = round(20 * u) if question else 0
        button_h = round(34 * u)
        bottom_gap = round(11 * u)
        margin_bottom = round(24 * u)

        for max_lines in (4, 3, 2):
            lines = self._wrap(visible, answer_size, text_w, max_lines=max_lines)
            height = (
                inner_pad * 2
                + label_h
                + q_h
                + round(4 * u)
                + len(lines) * line_h
                + bottom_gap
                + button_h
            )
            if hero.bottom - margin_bottom - height >= hero.y + round(116 * u):
                break

        bubble = pygame.Rect(
            pad,
            hero.bottom - margin_bottom - height,
            bubble_w,
            height,
        )
        self._bubble_rect = bubble

        shadow = bubble.move(0, round(5 * u))
        pygame.draw.rect(
            self.screen, (4, 5, 10), shadow, border_radius=round(23 * u)
        )
        self._panel(bubble, BUBBLE, radius=round(23 * u), border=LINE)

        color = STATUS_COLOR.get(self.status, FAINT)
        label_y = bubble.y + inner_pad
        pygame.draw.circle(
            self.screen,
            color,
            (bubble.x + inner_pad + round(5 * u), label_y + round(6 * u)),
            max(2, round(3.6 * u)),
        )
        self._text(
            "LIA",
            bubble.x + inner_pad + round(14 * u),
            label_y,
            round(11 * u),
            MINT,
            bold=True,
        )
        if self.ready:
            self._text(
                STATUS_LABEL.get(self.status, "").upper(),
                bubble.right - inner_pad,
                label_y + round(1 * u),
                round(9.5 * u),
                FAINT,
                bold=True,
                align="right",
            )

        y = label_y + label_h
        if question:
            q_line = self._wrap(
                f"VOCÊ  ·  {self.question}",
                round(13 * u),
                text_w,
                max_lines=1,
            )
            if q_line:
                self._text(
                    q_line[0],
                    bubble.x + inner_pad,
                    y,
                    round(13 * u),
                    DIM,
                )
            y += q_h
        y += round(4 * u)

        for line in lines:
            self._text(line, bubble.x + inner_pad, y, answer_size, TEXT)
            y += line_h

        hint = ""
        if not self.ready:
            hint = "preparando o totem..."
        elif self.mode == "conversa":
            hint = "a Lia também responde em voz alta"
        else:
            hint = "toque em uma pergunta ao lado"

        if self._busy():
            label, action, kind = "Parar", ("stop",), "danger"
        elif self.mode == "conversa":
            label, action, kind = "Nova conversa", ("reset",), "ghost"
        else:
            label, action, kind = "Ouvir a Lia", ("greet",), "solid"

        button_w = max(
            round(100 * u),
            self._font(round(12.5 * u), True).size(label)[0] + round(30 * u),
        )
        button = pygame.Rect(
            bubble.right - inner_pad - button_w,
            bubble.bottom - inner_pad - button_h,
            button_w,
            button_h,
        )
        self._button(
            button,
            label,
            action,
            kind=kind,
            size=round(12.5 * u),
            enabled=self.ready and (self.voice_ok or action[0] != "greet"),
        )
        if hint:
            self._text(
                hint,
                bubble.x + inner_pad,
                button.centery - round(7 * u),
                round(10.5 * u),
                FAINT,
            )

    def _draw_toast(self, layout) -> None:
        if not self._flash_text or time.time() >= self._flash_until:
            return
        u = layout["u"]
        hero: pygame.Rect = layout["hero"]
        size = round(12 * u)
        lines = self._wrap(
            self._flash_text, size, hero.w - 2 * layout["pad"] - round(36 * u), max_lines=2
        )
        text_w = max(self._font(size).size(line)[0] for line in lines) if lines else 0
        w = min(hero.w - 2 * layout["pad"], text_w + round(38 * u))
        h = len(lines) * round(16 * u) + round(20 * u)
        x = hero.centerx - w // 2
        bubble = getattr(self, "_bubble_rect", None)
        if not self.keyboard_open and bubble is not None:
            y = max(hero.y + round(12 * u), bubble.y - h - round(9 * u))
        else:
            y = hero.y + round(14 * u)
        rect = pygame.Rect(x, y, w, h)
        self._panel(rect, (27, 18, 22), radius=round(12 * u), border=_mix(DANGER, LINE, 0.5))
        ty = rect.y + round(10 * u)
        for line in lines:
            self._text(line, rect.centerx, ty, size, (255, 196, 190), align="center")
            ty += round(16 * u)

    # ------------------------------------------------------------- teclado
    def _draw_keyboard(self, layout) -> None:
        W, H = self.screen.get_size()
        u = layout["u"]
        pad = round(18 * u)
        key_h = round(44 * u)
        gap = round(7 * u)
        preview_h = round(50 * u)
        title_size = round(19 * u)
        sheet_h = (
            pad * 2
            + round(27 * u)
            + round(8 * u)
            + preview_h
            + round(10 * u)
            + 6 * key_h
            + 5 * gap
            + round(6 * u)
        )
        sheet_h = min(sheet_h, int(H * 0.74))
        sheet = pygame.Rect(0, H - sheet_h, W, sheet_h)
        self._kb_rect = sheet

        if self._dim is None or self._dim.get_size() != (W, H):
            self._dim = pygame.Surface((W, H), pygame.SRCALPHA)
            self._dim.fill((3, 4, 10, 168))
        self.screen.blit(self._dim, (0, 0))
        self._panel(sheet, (12, 15, 26), radius=round(24 * u), border=LINE)
        pygame.draw.line(
            self.screen, MINT, (0, sheet.y + 1), (round(120 * u), sheet.y + 1), 2
        )

        self._text(
            "Digite sua pergunta",
            pad,
            sheet.y + pad,
            title_size,
            TEXT,
            bold=True,
        )
        close = pygame.Rect(
            W - pad - round(32 * u), sheet.y + pad - round(4 * u),
            round(32 * u), round(32 * u)
        )
        self._panel(close, (25, 29, 46), radius=round(10 * u))
        self._text(
            "×",
            close.centerx,
            close.centery - round(12 * u),
            round(24 * u),
            DIM,
            align="center",
        )
        self._hits.append((close, ("close_keyboard",)))

        preview = pygame.Rect(
            pad,
            sheet.y + pad + round(27 * u) + round(8 * u),
            W - 2 * pad,
            preview_h,
        )
        self._panel(preview, (8, 10, 19), radius=round(13 * u), border=LINE)
        text_size = round(16 * u)
        placeholder = not self.typed
        cursor = "" if placeholder else ("│" if int(self.t * 2.2) % 2 == 0 else " ")
        shown = "Toque nas teclas ou use o teclado do computador..." if placeholder else self.typed + cursor
        line = self._wrap(
            shown,
            text_size,
            preview.w - round(28 * u),
            max_lines=1,
            tail=not placeholder,
        )
        if line:
            self._text(
                line[0],
                preview.x + round(14 * u),
                preview.centery - round(10 * u),
                text_size,
                FAINT if placeholder else TEXT,
            )

        kw = min(W - 2 * pad, round(920 * u))
        kx = (W - kw) // 2
        ky = preview.bottom + round(10 * u)
        rows = [
            list("1234567890"),
            list("qwertyuiop"),
            list("asdfghjklç"),
            list("zxcvbnm-.") + ["?"],
            ["á", "ã", "â", "à", "é", "ê", "í", "ó", "ô", "õ", "ú"],
        ]
        mouse = pygame.mouse.get_pos()
        for row in rows:
            count = len(row)
            key_w = (kw - gap * (count - 1)) / count
            for col, char in enumerate(row):
                rect = pygame.Rect(
                    int(kx + col * (key_w + gap)),
                    ky,
                    int(key_w),
                    key_h,
                )
                hover = rect.collidepoint(mouse)
                self._panel(
                    rect,
                    (35, 42, 68) if hover else (26, 31, 51),
                    radius=round(9 * u),
                    border=_mix(LINE, MINT, 0.45) if hover else LINE,
                )
                label = char.upper() if char.isalpha() and len(char) == 1 and char.isascii() else char
                self._text(
                    label,
                    rect.centerx,
                    rect.centery - round(9 * u),
                    max(11, round(15 * u)),
                    TEXT if hover else (214, 220, 238),
                    bold=True,
                    align="center",
                )
                self._hits.append((rect, ("key", char)))
            ky += key_h + gap

        action_y = ky
        back_w = round(kw * 0.22)
        send_w = round(kw * 0.22)
        space_w = kw - back_w - send_w - 2 * gap
        back = pygame.Rect(kx, action_y, back_w, key_h)
        space = pygame.Rect(back.right + gap, action_y, space_w, key_h)
        send = pygame.Rect(space.right + gap, action_y, send_w, key_h)
        self._button(back, "APAGAR", ("backspace",), kind="ghost", size=round(12 * u))
        self._button(space, "espaço", ("space",), kind="ghost", size=round(12.5 * u))
        self._button(send, "ENVIAR", ("send",), kind="solid", size=round(12.5 * u))


def main_totem(settings: Settings) -> int:
    try:
        app = KioskApp(settings)
    except Exception as exc:
        print(f"Não consegui abrir o totem: {exc}")
        traceback.print_exc()
        return 1
    app.run()
    return 0



