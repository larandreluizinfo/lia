"""Personagem 2D desenhada com Pygame, com lipsync e micro-animações.

Tudo é desenhado em código (nenhum asset externo). O desenho é feito numa
superfície com superamostragem (2x por padrão) para bordas suaves.

Lipsync: o envelope de volume do áudio do Kokoro (100 amostras/s) abre e fecha
a boca. É o mesmo princípio usado por bibliotecas de visemes, mas sem precisar
de alinhamento forçado — o que importa é a energia da fala no instante tocado.
"""

from __future__ import annotations

import math
import random
from collections import deque

import pygame

# ----------------------------------------------------------------- Paleta
SKIN = (244, 205, 176)
SKIN_SHADOW = (222, 170, 141)
BLUSH = (240, 138, 150)
HAIR = (108, 73, 182)
HAIR_DARK = (74, 48, 132)
HAIR_LIGHT = (147, 112, 219)
HOODIE = (33, 55, 84)
HOODIE_LIGHT = (44, 74, 106)
MINT = (99, 226, 194)
EYE_IRIS = (58, 205, 180)
EYE_DARK = (21, 32, 41)
WHITE = (250, 250, 253)
LIP = (196, 118, 112)
MOUTH_DARK = (66, 24, 34)
TONGUE = (208, 96, 116)
BG_TOP = (20, 22, 46)
BG_BOTTOM = (7, 8, 16)
PANEL = (11, 13, 24)
TEXT_DIM = (150, 158, 186)

STATUS = {
    "boot": ("Carregando", (150, 158, 186)),
    "idle": ("Pode falar", MINT),
    "listening": ("Ouvindo...", (94, 200, 255)),
    "thinking": ("Pensando...", (255, 196, 92)),
    "speaking": ("Falando", (176, 142, 255)),
    "error": ("Erro", (255, 110, 110)),
}


def _mix(a, b, t: float):
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


class Avatar:
    def __init__(self, width: int, height: int, scale: int = 2, title: str = "Lia"):
        self.ss = max(1, int(scale))
        self._k = self.ss
        self.title = title
        self._fonts: dict = {}
        self._wrap_cache: dict = {}
        self._blink_next = 2.0
        self._blink_start = -1.0
        self._mouth = 0.0
        self._bars = deque([0.05] * 28, maxlen=28)
        # Quando o avatar é desenhado num canvas maior que a área visível
        # (modo totem), o totem informa para onde ele deve olhar.
        self.pointer: tuple[float, float] | None = None
        self._particles = [
            {
                "x": random.random(),
                "y": random.random(),
                "speed": random.uniform(0.004, 0.016),
                "r": random.uniform(0.8, 2.6),
                "a": random.uniform(0.15, 0.5),
            }
            for _ in range(42)
        ]
        self.width = 0
        self.height = 0
        self.resize(width, height)

    # ------------------------------------------------------------ utilidades
    def _font(self, size: int, bold: bool = False):
        key = (size, bold)
        if key not in self._fonts:
            try:
                self._fonts[key] = pygame.font.SysFont(
                    "Segoe UI,DejaVu Sans,Arial", size, bold=bold
                )
            except Exception:
                self._fonts[key] = pygame.font.Font(None, size)
        return self._fonts[key]

    @property
    def k(self) -> int:
        return self._k

    def _rect(self, x, y, w, h):
        return pygame.Rect(
            int(x * self.k), int(y * self.k), max(1, int(w * self.k)), max(1, int(h * self.k))
        )

    def _ellipse(self, color, cx, cy, rx, ry):
        pygame.draw.ellipse(self.canvas, color, self._rect(cx - rx, cy - ry, rx * 2, ry * 2))

    def _circle(self, color, cx, cy, r):
        pygame.draw.circle(self.canvas, color, (int(cx * self.k), int(cy * self.k)), max(1, int(r * self.k)))

    def _line(self, color, p1, p2, width):
        pygame.draw.line(
            self.canvas,
            color,
            (int(p1[0] * self.k), int(p1[1] * self.k)),
            (int(p2[0] * self.k), int(p2[1] * self.k)),
            max(1, int(width * self.k)),
        )

    def _round_line(self, color, p1, p2, width):
        self._line(color, p1, p2, width)
        self._circle(color, p1[0], p1[1], width / 2)
        self._circle(color, p2[0], p2[1], width / 2)

    # ------------------------------------------------------------- estrutura
    def resize(self, width: int, height: int) -> None:
        self.width = max(320, int(width))
        self.height = max(240, int(height))

        # Limita o tamanho da superfície de desenho (4K em tela cheia, por ex.).
        k = self.ss
        while k > 1 and self.width * self.height * k * k > 4_000_000:
            k -= 1
        self._k = k

        W, H = self.width, self.height
        r = min(W * 0.150, H * 0.190)
        self.ui = max(0.85, min(1.55, H / 720.0))
        self.cx = W * 0.5
        self.cy = H * 0.395
        self.r = r
        self.rx = r
        self.ry = r * 1.12
        self.head_top = self.cy - self.ry
        self.eye_y = self.cy - self.ry * 0.07

        self.canvas = pygame.Surface((self.width * self.k, self.height * self.k)).convert()
        self.bg = self._make_background()
        self.glow = self._make_glow()
        self._wrap_cache.clear()

    def _make_background(self) -> pygame.Surface:
        w, h = self.width * self.k, self.height * self.k
        surf = pygame.Surface((w, h)).convert()
        for y in range(h):
            t = y / max(1, h - 1)
            color = _mix(BG_TOP, BG_BOTTOM, t ** 0.85)
            pygame.draw.line(surf, color, (0, y), (w, y))
        return surf

    def _make_glow(self) -> pygame.Surface:
        radius = int(self.r * 2.6 * self.k)
        surf = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
        steps = 48
        for i in range(steps, 0, -1):
            t = i / steps
            alpha = int(20 * (1 - t) ** 1.6)
            pygame.draw.circle(
                surf,
                (86, 66, 190, alpha),
                (radius, radius),
                int(radius * t),
            )
        return surf

    # ------------------------------------------------------------------ texto
    def _wrap(self, text: str, size: int, max_width: float, max_lines: int, tail: bool = False):
        key = (text, size, int(max_width), max_lines, tail)
        cached = self._wrap_cache.get(key)
        if cached is not None:
            return cached
        if len(self._wrap_cache) > 400:
            self._wrap_cache.clear()

        font = self._font(size)
        words = text.replace("\n", " ").split()
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
        result = lines or []
        self._wrap_cache[key] = result
        return result

    # -------------------------------------------------------------- desenho
    def draw(
        self,
        screen: pygame.Surface,
        *,
        state: str,
        level: float,
        user_text: str = "",
        ai_text: str = "",
        note: str = "",
        model_label: str = "",
        t: float = 0.0,
        chrome: bool = True,
    ) -> None:
        """Desenha a personagem.

        ``chrome=False`` desenha só o personagem (sem pílula de status,
        legendas, atalhos), para o modo totem montar a própria interface.
        """
        if screen.get_size() != (self.width, self.height):
            self.resize(*screen.get_size())

        self._draw_scene(state, level, t)
        pygame.transform.smoothscale(self.canvas, screen.get_size(), screen)
        if chrome:
            self._draw_overlay(screen, state, level, user_text, ai_text, note, model_label, t)

    # ---------------------------------------------------------- cena/corpo
    def _draw_scene(self, state: str, level: float, t: float) -> None:
        self.canvas.blit(self.bg, (0, 0))

        # brilho pulsante atrás da cabeça
        pulse = 1.0 + math.sin(t * 1.1) * 0.03 + level * 0.05
        gw = int(self.glow.get_width() * pulse)
        gh = int(self.glow.get_height() * pulse)
        glow = pygame.transform.smoothscale(self.glow, (gw, gh))
        self.canvas.blit(
            glow,
            (int(self.cx * self.k - gw / 2), int((self.cy - self.ry * 0.1) * self.k - gh / 2)),
        )

        self._draw_particles(t)

        bob = math.sin(t * 1.55) * 3.0 + math.sin(t * 0.73) * 1.6
        if state == "speaking":
            bob += math.sin(t * 7.0) * 1.4 * level
        sway = math.sin(t * 0.62) * 4.0

        cx = self.cx + sway * 0.35
        cy = self.cy + bob
        rx, ry = self.rx, self.ry

        self._draw_body(cx, cy, rx, ry, t, level, state)

        # pescoço
        pygame.draw.rect(
            self.canvas,
            SKIN_SHADOW,
            self._rect(cx - rx * 0.20, cy + ry * 0.55, rx * 0.40, ry * 0.80),
            border_radius=int(12 * self.k),
        )
        self._ellipse(_mix(SKIN_SHADOW, SKIN, 0.45), cx, cy + ry * 0.60, rx * 0.20, ry * 0.07)

        # cabelo de trás
        self._ellipse(HAIR_DARK, cx, cy - ry * 0.03, rx * 1.14, ry * 1.16)
        self._ellipse(HAIR, cx, cy - ry * 0.10, rx * 1.10, ry * 1.05)

        # rosto
        self._ellipse(SKIN, cx, cy + ry * 0.03, rx * 0.97, ry * 0.99)
        # sombra suave do cabelo na testa
        self._ellipse(_mix(SKIN, SKIN_SHADOW, 0.35), cx, cy - ry * 0.78, rx * 0.95, ry * 0.22)

        self._draw_hair_front(cx, cy, rx, ry, bob)
        self._draw_eyes(cx, cy, rx, ry, state, t)
        self._draw_brows(cx, cy, rx, ry, state, level)
        self._draw_nose(cx, cy, rx, ry)
        self._draw_blush(cx, cy, rx, ry)
        self._draw_mouth(cx, cy, rx, ry, state, level, t)

    def _draw_body(self, cx, cy, rx, ry, t, level, state) -> None:
        W, H = self.width, self.height
        breathe = math.sin(t * 1.25) * 2.5
        # ombros
        self._ellipse(HOODIE, cx, H * 1.00 + breathe, W * 0.33, H * 0.27)
        # gola alta do capuz
        self._ellipse(HOODIE_LIGHT, cx, H * 0.705 + breathe, rx * 0.88, ry * 0.30)
        self._ellipse(HOODIE, cx, H * 0.722 + breathe, rx * 0.62, ry * 0.18)
        # cordões do capuz
        for side in (-1, 1):
            top = (cx + side * rx * 0.15, H * 0.725 + breathe)
            bot = (cx + side * rx * 0.19, H * 0.835 + breathe)
            self._round_line(_mix(MINT, WHITE, 0.35), top, bot, 3.2)
            self._circle(_mix(MINT, WHITE, 0.55), bot[0], bot[1], 4.2)

        # micro-badge no peito
        badge_y = H * 0.975
        self._circle(_mix(HOODIE, MINT, 0.25), cx, badge_y, rx * 0.10)
        self._circle(MINT, cx, badge_y, rx * 0.05)

    def _draw_hair_front(self, cx, cy, rx, ry, bob) -> None:
        # franja principal (elipse achatada na testa)
        top = self.head_top - ry * 0.02
        bottom = cy - ry * 0.42
        cy_b = (top + bottom) / 2
        ry_b = (bottom - top) / 2
        self._ellipse(HAIR, cx, cy_b, rx * 1.02, ry_b)

        # pontas da franja
        for i, (ox, oy, w, h) in enumerate(((-0.52, -0.02, 0.20, 0.09), (-0.10, 0.03, 0.16, 0.07), (0.34, -0.03, 0.22, 0.10))):
            self._ellipse(HAIR, cx + rx * ox, cy - ry * 0.42 + ry * oy, rx * w, ry * h)

        # mechas laterais emoldurando o rosto
        for side in (-1, 1):
            self._ellipse(HAIR, cx + side * rx * 0.94, cy + ry * 0.10, rx * 0.16, ry * 0.52)

        # brilho do cabelo
        pygame.draw.arc(
            self.canvas,
            HAIR_LIGHT,
            self._rect(cx - rx * 0.72, cy - ry * 0.92, rx * 1.44, ry * 0.95),
            math.radians(112),
            math.radians(196),
            max(2, int(2.6 * self.k)),
        )

    def _draw_eyes(self, cx, cy, rx, ry, state, t) -> None:
        # piscada
        if self._blink_start < 0 and t >= self._blink_next:
            self._blink_start = t
        blink = 0.0
        if self._blink_start >= 0:
            p = (t - self._blink_start) / 0.135
            if p >= 1:
                self._blink_start = -1.0
                self._blink_next = t + random.uniform(2.2, 6.0)
            else:
                blink = math.sin(p * math.pi)

        mouse = self.pointer if self.pointer is not None else pygame.mouse.get_pos()
        look_x = max(-1.0, min(1.0, (mouse[0] - cx) / (self.width * 0.55)))
        look_y = max(-1.0, min(1.0, (mouse[1] - cy) / (self.height * 0.55)))
        if state == "thinking":
            look_x, look_y = -0.45, -0.7

        eye_y = self.eye_y + (cy - self.cy)
        eye_rx = rx * 0.185
        base_eye_ry = ry * 0.215
        eye_ry = base_eye_ry * (1.0 - 0.92 * blink)

        for side in (-1, 1):
            ex = cx + side * rx * 0.355
            if blink > 0.55:
                # olho fechado: curva delicada
                pygame.draw.arc(
                    self.canvas,
                    EYE_DARK,
                    self._rect(ex - eye_rx, eye_y - base_eye_ry * 0.8, eye_rx * 2, base_eye_ry * 1.6),
                    math.radians(200),
                    math.radians(340),
                    max(2, int(2.2 * self.k)),
                )
                continue

            self._ellipse(WHITE, ex, eye_y, eye_rx, eye_ry)
            iris_r = eye_rx * 0.62
            ix = ex + look_x * eye_rx * 0.34
            iy = eye_y + look_y * eye_ry * 0.30
            self._circle(EYE_IRIS, ix, iy, iris_r)
            self._circle(_mix(EYE_IRIS, EYE_DARK, 0.55), ix, iy + iris_r * 0.12, iris_r * 0.72)
            self._circle(EYE_DARK, ix, iy, iris_r * 0.46)
            self._circle(WHITE, ix - iris_r * 0.34, iy - iris_r * 0.36, iris_r * 0.28)
            self._circle(WHITE, ix + iris_r * 0.30, iy + iris_r * 0.26, iris_r * 0.12)

            # cílios
            pygame.draw.arc(
                self.canvas,
                HAIR_DARK,
                self._rect(ex - eye_rx * 1.05, eye_y - eye_ry * 1.22, eye_rx * 2.1, eye_ry * 1.1),
                math.radians(18),
                math.radians(162),
                max(2, int(1.8 * self.k)),
            )

        # sobrancelhas
    def _draw_brows(self, cx, cy, rx, ry, state, level) -> None:
        raise_px = 0.0
        if state == "listening":
            raise_px = 5.0
        elif state == "thinking":
            raise_px = 6.5
        elif state == "speaking":
            raise_px = 1.5 + level * 3.5

        brow_y = self.eye_y + (cy - self.cy) - ry * 0.40 - raise_px
        for side in (-1, 1):
            ex = cx + side * rx * 0.355
            inner = (ex - side * rx * 0.13, brow_y + 2.2)
            outer = (ex + side * rx * 0.14, brow_y - 1.6)
            self._round_line(HAIR_DARK, inner, outer, 6.2)

    def _draw_nose(self, cx, cy, rx, ry) -> None:
        ny = cy + ry * 0.20
        pygame.draw.arc(
            self.canvas,
            SKIN_SHADOW,
            self._rect(cx - rx * 0.085, ny, rx * 0.17, ry * 0.10),
            math.radians(20),
            math.radians(160),
            max(2, int(2.2 * self.k)),
        )
        self._circle(_mix(SKIN, WHITE, 0.18), cx - rx * 0.012, ny + ry * 0.028, rx * 0.022)

    def _draw_blush(self, cx, cy, rx, ry) -> None:
        for side in (-1, 1):
            bx = cx + side * rx * 0.50
            by = cy + ry * 0.27
            for i in range(4, 0, -1):
                t = i / 4
                self._ellipse(
                    _mix(SKIN, BLUSH, 0.10 + 0.09 * (4 - i)),
                    bx,
                    by,
                    rx * 0.16 * (0.55 + t * 0.5),
                    ry * 0.10 * (0.55 + t * 0.5),
                )

    def _draw_mouth(self, cx, cy, rx, ry, state, level, t) -> None:
        target = level if state == "speaking" else 0.0
        coeff = 0.55 if target > self._mouth else 0.24
        self._mouth += (target - self._mouth) * coeff
        openness = max(0.0, min(1.0, self._mouth))

        my = cy + ry * 0.50
        if openness < 0.045:
            # sorriso fechado
            w = rx * 0.30
            h = ry * 0.075
            points = []
            for i in range(9):
                p = i / 8
                x = cx - w + 2 * w * p
                y = my + math.sin(p * math.pi) * h * 0.75
                points.append((x, y))
            for a, b in zip(points, points[1:]):
                self._round_line(LIP, a, b, 3.6)
            return

        w = rx * (0.52 + 0.16 * openness) + math.sin(t * 15.0) * rx * 0.02 * openness
        h = ry * (0.03 + 0.36 * openness)

        sw = max(4, int(w * self.k))
        sh = max(4, int(h * self.k))
        mouth = pygame.Surface((sw, sh), pygame.SRCALPHA)
        pygame.draw.ellipse(mouth, MOUTH_DARK, (0, 0, sw, sh))
        if sh > 8:
            # dentes
            pygame.draw.ellipse(
                mouth,
                (250, 247, 242),
                (int(sw * 0.17), int(sh * 0.03), int(sw * 0.66), int(sh * 0.33)),
            )
        if sh > 26:
            # língua
            pygame.draw.ellipse(
                mouth,
                TONGUE,
                (int(sw * 0.27), int(sh * 0.55), int(sw * 0.46), int(sh * 0.44)),
            )
        pygame.draw.ellipse(mouth, LIP, (0, 0, sw, sh), max(2, int(2.0 * self.k)))
        self.canvas.blit(mouth, (int(cx * self.k - sw / 2), int(my * self.k - sh / 2)))

    def _draw_particles(self, t: float) -> None:
        W, H = self.width, self.height
        for particle in self._particles:
            particle["y"] -= particle["speed"] * 0.016
            if particle["y"] < -0.02:
                particle["y"] = 1.02
                particle["x"] = random.random()
            x = particle["x"] * W + math.sin(t * 0.6 + particle["y"] * 8) * 6
            y = particle["y"] * H
            color = _mix(BG_TOP, (168, 148, 255), particle["a"] * 0.55)
            self._circle(color, x, y, particle["r"])

    # ------------------------------------------------------------- interface
    def _panel(self, screen, rect, color, radius=14, border=None):
        surf = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
        pygame.draw.rect(surf, color, (0, 0, rect.width, rect.height), border_radius=radius)
        if border:
            pygame.draw.rect(surf, border, (0, 0, rect.width, rect.height), width=1, border_radius=radius)
        screen.blit(surf, rect.topleft)

    def _text(self, screen, text, x, y, size, color, bold=False, center=False):
        surf = self._font(size, bold).render(text, True, color)
        if center:
            x -= surf.get_width() / 2
        screen.blit(surf, (int(x), int(y)))
        return surf.get_width()

    def _draw_overlay(self, screen, state, level, user_text, ai_text, note, model_label, t) -> None:
        W, H = screen.get_size()
        ui = self.ui
        label, dot_color = STATUS.get(state, STATUS["idle"])

        # equalizador
        self._bars.append(level if state in ("listening", "speaking") else 0.03)
        bar_w = max(2, round(3 * ui))
        gap = max(2, round(3 * ui))
        n = len(self._bars)
        eq_w = n * (bar_w + gap) - gap
        eq_h = round(20 * ui)

        # pílula de status
        status_size = round(17 * ui)
        font_status = self._font(status_size, True)
        tw = font_status.size(label)[0]
        pill_h = round(44 * ui)
        pill = pygame.Rect(0, 0, int(70 * ui) + tw + eq_w, pill_h)
        pill.topleft = (round(24 * ui), round(22 * ui))
        self._panel(screen, pill, (*PANEL, 175), round(14 * ui), (255, 255, 255, 18))

        pulse = 0.5 + 0.5 * math.sin(t * 3.2)
        dot = (pill.x + round(20 * ui), pill.y + pill_h // 2)
        pygame.draw.circle(screen, dot_color, dot, round(5 * ui))
        pygame.draw.circle(
            screen,
            (*dot_color, 70),
            dot,
            round((5 + 4 * pulse) * ui),
            width=max(1, round(2 * ui)),
        )
        self._text(
            screen,
            label,
            pill.x + round(34 * ui),
            pill.y + (pill_h - status_size) // 2 - 1,
            status_size,
            (232, 236, 248),
            bold=True,
        )

        eq_x = pill.right - eq_w - round(12 * ui)
        self._panel(
            screen,
            pygame.Rect(eq_x - round(5 * ui), pill.y + round(12 * ui), eq_w + round(10 * ui), eq_h),
            (255, 255, 255, 10),
            max(3, round(6 * ui)),
        )
        for i, value in enumerate(self._bars):
            bh = max(3, int(3 + value * (eq_h - 4)))
            color = _mix(dot_color, (255, 255, 255), 0.25 * (i / n))
            rect = pygame.Rect(eq_x + i * (bar_w + gap), dot[1] - bh // 2, bar_w, bh)
            pygame.draw.rect(screen, color, rect, border_radius=max(1, bar_w // 2))

        # modelo / atalhos
        if model_label:
            surf = self._font(round(14 * ui)).render(model_label, True, TEXT_DIM)
            screen.blit(surf, (W - round(24 * ui) - surf.get_width(), round(26 * ui)))

        hint = "espaço falar · M microfone · F tela · esc sair"
        hsurf = self._font(round(13 * ui)).render(hint, True, (112, 120, 148))
        screen.blit(hsurf, (W - round(24 * ui) - hsurf.get_width(), H - round(26 * ui)))

        # nota (carregando / erro / dica)
        if note:
            color = (255, 130, 130) if state == "error" else (170, 178, 208)
            note_size = round(15 * ui)
            lines = self._wrap(note, note_size, W * 0.55, 2, tail=True)
            y = round(78 * ui)
            for line in lines:
                surf = self._font(note_size).render(line, True, color)
                screen.blit(surf, (round(26 * ui), y))
                y += round(20 * ui)

        # legendas
        if user_text or ai_text:
            user_size = round(17 * ui)
            ai_size = round(21 * ui)
            label_size = round(12 * ui)
            user_lh = round(21 * ui)
            ai_lh = round(27 * ui)
            pad_x = round(22 * ui)

            panel_w = min(W - round(80 * ui), round(780 * ui))
            wrap_w = panel_w - 2 * pad_x
            u_lines = self._wrap(user_text, user_size, wrap_w, 2) if user_text else []
            a_lines = self._wrap(ai_text, ai_size, wrap_w, 3, tail=True) if ai_text else []

            panel_h = round(16 * ui)
            if u_lines:
                panel_h += round(20 * ui) + user_lh * len(u_lines)
            if u_lines and a_lines:
                panel_h += round(8 * ui)
            if a_lines:
                panel_h += round(22 * ui) + ai_lh * len(a_lines)
            panel_h += round(14 * ui)

            panel = pygame.Rect(
                (W - panel_w) // 2, H - panel_h - round(48 * ui), panel_w, panel_h
            )
            self._panel(screen, panel, (*PANEL, 195), round(18 * ui), (255, 255, 255, 20))

            y = panel.y + round(16 * ui)
            if u_lines:
                self._text(screen, "VOCÊ", panel.x + pad_x, y, label_size, (128, 196, 186), bold=True)
                y += round(20 * ui)
                for line in u_lines:
                    self._text(screen, line, panel.x + pad_x, y, user_size, (196, 205, 228))
                    y += user_lh
                if a_lines:
                    y += round(8 * ui)
            if a_lines:
                self._text(screen, "LIA", panel.x + pad_x, y, label_size, (176, 142, 255), bold=True)
                y += round(22 * ui)
                for line in a_lines:
                    self._text(screen, line, panel.x + pad_x, y, ai_size, (246, 248, 255))
                    y += ai_lh

        # durante o boot, um spinner discreto
        if state == "boot":
            cx = W / 2
            cy = H * 0.72
            size = round(32 * ui)
            pygame.draw.arc(
                screen,
                MINT,
                pygame.Rect(cx - size / 2, cy - size / 2, size, size),
                t * 4.0,
                t * 4.0 + 2.0,
                width=max(2, round(3 * ui)),
            )
