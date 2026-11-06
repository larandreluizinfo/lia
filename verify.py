"""Teste de fumaça do totem: LLM, voz, interface, QR e página do celular.

Uso:
    python verify.py                 # valida o modo feira (sem microfone)
    python verify.py --microfone     # inclui Whisper (baixa ~460 MB)

Salva amostras em out/ (voz em .wav e quadros em .png).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.request

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np

from lia.config import OUT_DIR, Settings, all_providers
from lia.llm import check_provider
from lia.texto import split_sentences
from lia.tts import TTS
from lia.webserver import WebBridge


def log(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or len(audio) == 0:
        return audio.astype(np.float32)
    count = max(1, int(round(len(audio) * target_rate / source_rate)))
    x_old = np.linspace(0.0, 1.0, len(audio), dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, count, dtype=np.float64)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def test_frases() -> None:
    ready, rest = split_sentences("Olá, tudo bem? Eu sou a Lia! E aí")
    print(f"frases: {ready} | resto: {rest!r}")
    if ready != ["Olá, tudo bem? Eu sou a Lia!"] or not rest.startswith("E aí"):
        raise RuntimeError("divisão de frases inesperada")
    ready, rest = split_sentences("Esta é uma frase longa o suficiente para ser falada sozinha. Outra frase também longa para o teste. ")
    if len(ready) != 2 or rest:
        raise RuntimeError("divisão de frases longas inesperada")
    print("frases longas: ok")


def test_llm(settings: Settings) -> str:
    started = time.time()
    name = settings.provider
    if name not in all_providers():
        raise SystemExit(f"provedor desconhecido: {name}")
    print(f"provedor: {name} ({all_providers()[name].model})")
    text = check_provider(name, settings)
    print(f"resposta: {text[:200]!r}")
    print(f"tempo: {time.time() - started:.2f}s")
    if not text.strip():
        raise RuntimeError("LLM devolveu texto vazio")
    return text


def test_tts(settings: Settings) -> np.ndarray:
    tts = TTS(settings.tts_voice, settings.tts_speed, settings.tts_lang_code)
    tts.load()
    text = "Olá! Eu sou a Lia. Estou testando minha voz em português do Brasil."
    started = time.time()
    audio = tts.synthesize(text)
    elapsed = time.time() - started
    duration = len(audio) / tts.SAMPLE_RATE
    print(f"voz: {settings.tts_voice} | {duration:.2f}s de áudio em {elapsed:.2f}s")
    if duration < 0.5:
        raise RuntimeError("áudio muito curto — TTS falhou")

    import soundfile as sf

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "amostra_voz.wav"
    sf.write(str(path), audio, tts.SAMPLE_RATE)
    print(f"salvo: {path}")
    return audio


def test_saida_de_audio() -> None:
    from lia.audio import Player

    player = Player()
    player.close()
    print("dispositivo de saída aberto e fechado.")


def test_avatar() -> None:
    import pygame

    from lia.avatar import Avatar

    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((1000, 720))
    avatar = Avatar(1000, 720, scale=2)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    frames = [
        ("idle", 0.0, "", "", ""),
        ("thinking", 0.0, "Me conta uma curiosidade sobre o espaço.", "", ""),
        (
            "speaking",
            0.75,
            "Oi! Como você está hoje?",
            "Oi! Eu estou muito bem, obrigada por perguntar. E você, como está?",
            "",
        ),
    ]
    for state, level, user, ai, note in frames:
        screen.fill((0, 0, 0))
        avatar.draw(
            screen,
            state=state,
            level=level,
            user_text=user,
            ai_text=ai,
            note=note,
            model_label="pollinations · openai",
            t=3.2,
        )
        path = OUT_DIR / f"avatar_{state}.png"
        pygame.image.save(screen, str(path))
        print(f"salvo: {path}")
    pygame.quit()


def test_totem(settings: Settings) -> None:
    import pygame

    from lia.kiosk import KioskApp

    totem_settings = Settings(
        width=1280,
        height=800,
        fullscreen=False,
        audio=False,
        web=False,
        camera=False,
        auto_greet=False,
        questions_file=settings.questions_file,
    )
    app = KioskApp(totem_settings)
    app._load()
    if app.fatal:
        app._shutdown()
        raise RuntimeError(app.fatal)
    app.t = 4.0
    app._draw(time.time())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    atrativo = OUT_DIR / "totem_atrativo.png"
    pygame.image.save(app.screen, str(atrativo))
    print(f"salvo: {atrativo}")

    app._ask_preset(0)
    app.reveal = float(len(app.answer))
    app.t = 6.0
    app._draw(time.time())
    conversa = OUT_DIR / "totem_conversa.png"
    pygame.image.save(app.screen, str(conversa))
    print(f"salvo: {conversa}")
    if not app.question or len(app.answer) < 20:
        app._shutdown()
        raise RuntimeError("pergunta pronta não carregou")
    app._shutdown()


def test_web(settings: Settings) -> None:
    from lia.kiosk import load_questions

    bank = load_questions(settings.questions_file)
    bridge = WebBridge(
        port=0,
        event=bank.evento,
        welcome=bank.boas_vindas,
        questions=[q.titulo for q in bank.perguntas],
    )
    if not bridge.available:
        raise RuntimeError(bridge.error or "servidor web não abriu")
    base = f"http://127.0.0.1:{bridge.port}"
    try:
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        if "const CONFIG" not in page or "Digite sua pergunta" not in page:
            raise RuntimeError("página do celular incompleta")
        print(f"página: {len(page)} bytes | URL: {bridge.url}")

        estado = json.loads(
            urllib.request.urlopen(base + "/api/estado", timeout=5).read()
        )
        if len(estado.get("perguntas", [])) != len(bank.perguntas):
            raise RuntimeError("perguntas não chegaram ao celular")
        print(f"estado: {estado['estado']} | {len(estado['perguntas'])} perguntas")

        request = urllib.request.Request(
            base + "/api/mensagem",
            data=json.dumps({"texto": "  teste   do totem  "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resposta = json.loads(urllib.request.urlopen(request, timeout=5).read())
        if not resposta.get("ok"):
            raise RuntimeError("POST /api/mensagem recusado")
        fila = bridge.take_messages()
        if fila != ["teste do totem"]:
            raise RuntimeError(f"mensagem inesperada na fila: {fila}")
        print(f"mensagem recebida: {fila[0]!r}")
    finally:
        bridge.close()


def test_perguntas(settings: Settings) -> None:
    from lia.kiosk import load_questions

    bank = load_questions(settings.questions_file)
    print(f"evento: {bank.evento}")
    print(f"boas-vindas: {bank.boas_vindas[:90]}...")
    print(f"perguntas: {len(bank.perguntas)}")
    for question in bank.perguntas:
        print(f"  - {question.titulo}")
    if len(bank.perguntas) < 1 or not bank.boas_vindas:
        raise RuntimeError("perguntas.json está vazio")


def test_stt(settings: Settings, audio: np.ndarray) -> None:
    from lia.stt import STT

    stt = STT(
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute,
        settings.language,
    )
    stt.load()
    audio_16k = resample(audio, 24000, 16000)
    started = time.time()
    text = stt.transcribe(audio_16k)
    print(f"transcrição: {text!r}")
    print(f"tempo: {time.time() - started:.2f}s")
    if not text.strip():
        raise RuntimeError("Whisper não transcreveu nada")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verifica a instalação da Lia")
    parser.add_argument(
        "--microfone",
        action="store_true",
        help="também testa o Whisper (modo voz opcional, baixa ~460 MB)",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    print("Lia — verificação do ambiente")
    print(
        f"python {sys.version.split()[0]} | provedor {settings.provider} | voz {settings.tts_voice}"
    )

    failures = 0
    audio = None

    log("perguntas do evento")
    try:
        test_perguntas(settings)
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")
        traceback.print_exc()

    log("frases para a voz")
    try:
        test_frases()
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")

    log("LLM")
    try:
        test_llm(settings)
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")
        traceback.print_exc()

    log("Kokoro-TTS")
    try:
        audio = test_tts(settings)
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")
        traceback.print_exc()

    log("alto-falante")
    try:
        test_saida_de_audio()
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")

    log("avatar")
    try:
        test_avatar()
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")
        traceback.print_exc()

    log("interface do totem")
    try:
        test_totem(settings)
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")
        traceback.print_exc()

    log("página do celular")
    try:
        test_web(settings)
        print("OK")
    except Exception as exc:
        failures += 1
        print(f"FALHOU: {exc}")
        traceback.print_exc()

    if args.microfone:
        log("Whisper (opcional)")
        if audio is None:
            failures += 1
            print("FALHOU: pulado (o TTS não gerou áudio)")
        else:
            try:
                test_stt(settings, audio)
                print("OK")
            except Exception as exc:
                failures += 1
                print(f"FALHOU: {exc}")
                traceback.print_exc()

    log("resultado")
    if failures:
        print(f"{failures} etapa(s) com problema.")
        return 1
    print("Tudo funcionando. Rode:  python run.py --fullscreen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
