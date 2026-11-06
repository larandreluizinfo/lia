"""Utilidades de texto compartilhadas entre o modo voz e o modo totem."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[.!?…]+[\"'\u201d\u2019)\]]?\s+")


def split_sentences(buffer: str, min_len: int = 18, max_len: int = 220) -> tuple[list[str], str]:
    """Separa frases prontas de um texto que ainda está chegando do LLM.

    Devolve (frases_prontas, resto). O resto fica no buffer para a próxima
    leva de tokens. Frases muito longas são cortadas em um espaço próximo de
    ``max_len`` para a voz não demorar a começar.
    """
    ready: list[str] = []
    while True:
        cut = 0
        for match in _SENTENCE_END.finditer(buffer):
            if match.end() >= min_len:
                cut = match.end()
                break
        if cut == 0 and len(buffer) > max_len:
            space = buffer.rfind(" ", 0, max_len)
            cut = space + 1 if space > min_len else max_len
        if cut <= 0:
            break
        piece = buffer[:cut].strip()
        if piece:
            ready.append(piece)
        buffer = buffer[cut:].lstrip()
    return ready, buffer
