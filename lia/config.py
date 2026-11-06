"""Configurações da Lia (lidas de variáveis de ambiente e do arquivo .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
OUT_DIR = ROOT / "out"
CACHE_DIR = ROOT / "cache"


def _load_dotenv(path: Path) -> None:
    """Leitor mínimo de .env (sem dependência externa)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    raw = env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "sim", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Provider:
    """Qualquer endpoint compatível com a API /chat/completions da OpenAI."""

    name: str
    label: str
    url: str
    model: str
    api_key: str = ""
    needs_key: bool = False
    extra_headers: dict = field(default_factory=dict)


def providers() -> dict[str, Provider]:
    pol_token = env("POLLINATIONS_TOKEN")
    return {
        # Gratuito, sem cadastro e sem chave. É o padrão.
        "pollinations": Provider(
            name="pollinations",
            label="Pollinations (grátis, sem chave)",
            url="https://text.pollinations.ai/openai",
            model=env("LIA_MODEL", "openai"),
            api_key=pol_token,
        ),
        # 100% local e ilimitado de verdade (precisa do Ollama rodando).
        "ollama": Provider(
            name="ollama",
            label="Ollama (local, ilimitado)",
            url=env("OLLAMA_URL", "http://localhost:11434/v1/chat/completions"),
            model=env("OLLAMA_MODEL", "qwen2.5:3b"),
            api_key="ollama",
        ),
        # Plano gratuito rápido, precisa de chave: https://console.groq.com
        "groq": Provider(
            name="groq",
            label="Groq (grátis com chave)",
            url="https://api.groq.com/openai/v1/chat/completions",
            model=env("GROQ_MODEL", "llama-3.3-70b-versatile"),
            api_key=env("GROQ_API_KEY"),
            needs_key=True,
        ),
        # Plano gratuito do Google AI Studio.
        "gemini": Provider(
            name="gemini",
            label="Google Gemini (grátis com chave)",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            model=env("GEMINI_MODEL", "gemini-2.5-flash"),
            api_key=env("GEMINI_API_KEY"),
            needs_key=True,
        ),
        # Qualquer servidor compatível com OpenAI (LM Studio, vLLM, etc).
        "custom": Provider(
            name="custom",
            label="Endpoint personalizado",
            url=env("LIA_BASE_URL", "http://localhost:1234/v1/chat/completions"),
            model=env("LIA_MODEL", "local-model"),
            api_key=env("LIA_API_KEY", "none"),
        ),
    }


@dataclass
class Settings:
    # --- LLM ---
    provider: str = env("LIA_PROVIDER", "pollinations")
    # Lista separada por vírgula, tentada em ordem se o principal falhar.
    fallback: str = env("LIA_FALLBACK", "ollama")
    temperature: float = env_float("LIA_TEMPERATURE", 0.7)
    max_tokens: int = env_int("LIA_MAX_TOKENS", 320)
    history_turns: int = env_int("LIA_HISTORY", 8)
    system_prompt: str = env(
        "LIA_PERSONA",
        (
            "Você é a Lia, uma companhia virtual simpática, curiosa e bem-humorada. "
            "Você conversa por voz com pessoas em português do Brasil. "
            "Regras: responda em no máximo 3 frases curtas; fale de forma natural, "
            "como numa conversa; nunca use markdown, listas, emojis, asteriscos ou "
            "parênteses com ações, porque sua resposta será lida em voz alta; "
            "se não souber algo, diga que não sabe em vez de inventar."
        ),
    )

    # --- Voz (Whisper) ---
    whisper_model: str = env("LIA_WHISPER", "small")
    whisper_device: str = env("LIA_WHISPER_DEVICE", "cpu")
    whisper_compute: str = env("LIA_WHISPER_COMPUTE", "int8")
    language: str = env("LIA_LANG", "pt")

    # --- Fala (Kokoro) ---
    tts_voice: str = env("LIA_VOZ", "pf_dora")
    tts_speed: float = env_float("LIA_VELOCIDADE", 1.0)
    tts_lang_code: str = env("LIA_TTS_LANG", "p")  # p = português do Brasil

    # --- Interface ---
    width: int = env_int("LIA_LARGURA", 1280)
    height: int = env_int("LIA_ALTURA", 800)
    scale: int = env_int("LIA_ESCALA", 2)  # superamostragem do desenho (2 = suave)
    fullscreen: bool = env_bool("LIA_FULLSCREEN", False)
    auto_listen: bool = env_bool("LIA_AUTO_ESCUTA", True)

    # --- Totem de feira (modo padrão, sem microfone) ---
    mode: str = env("LIA_MODO", "totem")  # totem | microfone | texto
    questions_file: Path = Path(env("LIA_PERGUNTAS", str(ROOT / "perguntas.json")))
    web: bool = env_bool("LIA_WEB", True)
    web_port: int = env_int("LIA_WEB_PORT", 8765)
    web_url: str = env("LIA_WEB_URL", "")  # opcional: URL pública do celular
    inactivity: float = env_float("LIA_INATIVIDADE", 90.0)
    camera: bool = env_bool("LIA_CAMERA", False)
    camera_index: int = env_int("LIA_CAMERA_INDICE", 0)
    auto_greet: bool = env_bool("LIA_SAUDACAO", True)
    audio: bool = env_bool("LIA_AUDIO", True)

    # --- Microfone (detecção de fala) ---
    mic_gain: float = env_float("LIA_MIC_GAIN", 1.0)
    silence_seconds: float = env_float("LIA_SILENCIO", 0.8)
    max_utterance: float = env_float("LIA_MAX_FALA", 20.0)


def all_providers() -> dict[str, Provider]:
    return providers()
