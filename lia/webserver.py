"""Ponte web do totem: o visitante conversa pelo celular via QR code.

Sobe um servidor HTTP simples (biblioteca padrão) na rede local. A página é
toda embutida (sem CDN, sem internet) e conversa com o totem por dois
endpoints:

- ``POST /api/mensagem``  envia uma pergunta;
- ``GET  /api/estado``    devolve a pergunta, a resposta e o status atuais.
"""

from __future__ import annotations

import json
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def lan_ip() -> str:
    """Descobre o IP da máquina na rede local (sem enviar pacotes de verdade)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


class _Handler(BaseHTTPRequestHandler):
    bridge: "WebBridge" = None  # type: ignore[assignment]

    # Silencia o log de cada requisição no console do totem.
    def log_message(self, fmt, *args):  # noqa: N802
        return

    def _send(self, status: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, self.bridge.page(), "text/html")
        elif path == "/api/estado":
            body = json.dumps(self.bridge.snapshot(), ensure_ascii=False)
            self._send(200, body, "application/json")
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._send(404, "não encontrado", "text/plain")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/mensagem":
            self._send(404, "não encontrado", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            self._send(413, json.dumps({"erro": "mensagem inválida"}), "application/json")
            return
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, json.dumps({"erro": "json inválido"}), "application/json")
            return

        text = str(data.get("texto", "")) if isinstance(data, dict) else ""
        text = " ".join(text.split())[:300].strip()
        if len(text) < 2:
            self._send(400, json.dumps({"erro": "escreva uma pergunta"}), "application/json")
            return

        self.bridge.push(text)
        body = json.dumps({"ok": True, "texto": text}, ensure_ascii=False)
        self._send(200, body, "application/json")


class WebBridge:
    def __init__(
        self,
        port: int = 8765,
        *,
        event: str = "",
        welcome: str = "",
        questions: list[str] | None = None,
        public_url: str = "",
        port_tries: int = 6,
    ):
        self.port = int(port)
        self.event = event
        self.welcome = welcome
        self.questions = list(questions or [])
        self.available = False
        self.error = ""
        self.url = public_url.strip()

        self._lock = threading.Lock()
        self._messages: deque[str] = deque(maxlen=20)
        self._state = {
            "estado": "atrativo",
            "status": "carregando",
            "pergunta": "",
            "resposta": "",
        }
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        handler = type("LiaWebHandler", (_Handler,), {"bridge": self})
        last_error = ""
        for candidate in range(self.port, self.port + max(1, port_tries)):
            try:
                httpd = ThreadingHTTPServer(("0.0.0.0", candidate), handler)
                httpd.daemon_threads = True
                self._httpd = httpd
                self.port = int(httpd.server_address[1])
                break
            except OSError as exc:
                last_error = str(exc)
        if self._httpd is None:
            self.error = last_error or "porta indisponível"
            print(f"[lia] página do celular desativada: {self.error}")
            return

        if not self.url:
            host = lan_ip()
            self.url = f"http://{host}:{self.port}"
            if host == "127.0.0.1":
                print("[lia] sem rede local: a página do celular só abre neste computador.")
        self.available = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True
        )
        self._thread.start()
        print(f"[lia] página do celular: {self.url}")

    # ------------------------------------------------------------------ API
    def push(self, text: str) -> None:
        with self._lock:
            self._messages.append(text)

    def take_messages(self) -> list[str]:
        with self._lock:
            messages = list(self._messages)
            self._messages.clear()
            return messages

    def update(self, *, estado: str, status: str, pergunta: str, resposta: str) -> None:
        with self._lock:
            self._state = {
                "estado": estado,
                "status": status,
                "pergunta": pergunta[:300],
                "resposta": resposta[:1600],
            }

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
        state.update(
            {
                "evento": self.event,
                "boas_vindas": self.welcome,
                "perguntas": self.questions,
            }
        )
        return state

    def close(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    # ---------------------------------------------------------------- página
    def page(self) -> str:
        config = {
            "evento": self.event,
            "boas_vindas": self.welcome,
            "perguntas": self.questions,
        }
        blob = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
        return _PAGE.replace("__CONFIG__", blob)


_PAGE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0d18">
<title>Lia · recepção</title>
<style>
:root{--bg:#070810;--panel:#111524;--line:rgba(255,255,255,.08);--text:#f2f4fb;--dim:#9aa3c0;--mint:#63e2c2;--lilac:#b08eff}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{background:radial-gradient(120% 80% at 50% -10%,#1a1d3a 0%,#070810 55%);color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;flex-direction:column;min-height:100dvh}
header{padding:20px 20px 8px;display:flex;align-items:center;gap:11px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mint);box-shadow:0 0 0 4px rgba(99,226,194,.12)}
.brand{font-weight:700;letter-spacing:.02em;line-height:1.2}
.brand small{display:block;color:var(--dim);font-weight:400;font-size:12px;letter-spacing:0}
main{flex:1;padding:10px 16px 0;display:flex;flex-direction:column;gap:14px;overflow-y:auto}
.card{background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.015));border:1px solid var(--line);border-radius:20px;padding:18px}
.label{font-size:11px;letter-spacing:.14em;color:var(--mint);font-weight:700;text-transform:uppercase}
.question{color:var(--dim);font-size:14px;margin:9px 0 0;min-height:0}
.answer{font-size:19px;line-height:1.5;margin:11px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}
.status{font-size:12px;color:var(--dim);margin-top:16px;display:flex;align-items:center;gap:8px}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--lilac);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
.chips{display:flex;gap:8px;overflow-x:auto;padding:2px 2px 8px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chips button{white-space:nowrap;background:rgba(255,255,255,.06);border:1px solid var(--line);color:var(--text);padding:11px 15px;border-radius:999px;font-size:13px;font-family:inherit}
.chips button:active{background:rgba(99,226,194,.16);border-color:rgba(99,226,194,.4)}
footer{position:sticky;bottom:0;background:linear-gradient(180deg,transparent,#070810 32%);padding:12px 16px calc(14px + env(safe-area-inset-bottom))}
.composer{display:flex;gap:8px;align-items:flex-end;background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:7px 7px 7px 16px}
textarea{flex:1;resize:none;border:0;outline:0;background:transparent;color:var(--text);font:inherit;font-size:16px;max-height:120px;padding:11px 0}
textarea::placeholder{color:#6f7899}
.send{width:46px;height:46px;flex:0 0 46px;border-radius:50%;border:0;background:var(--mint);color:#062a25;font-size:21px;font-weight:800;display:grid;place-items:center;cursor:pointer}
.send:disabled{opacity:.35}
.hint{text-align:center;color:#6f7899;font-size:11px;margin-top:9px;line-height:1.4}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <div class="brand">Lia<small id="evento">recepção virtual</small></div>
</header>
<main>
  <section class="card" aria-live="polite">
    <div class="label" id="quem">Lia</div>
    <p class="question" id="pergunta" hidden></p>
    <p class="answer" id="resposta">Carregando...</p>
    <div class="status"><span class="pulse"></span><span id="status">acordando a Lia...</span></div>
  </section>
  <div class="chips" id="chips"></div>
</main>
<footer>
  <div class="composer">
    <textarea id="texto" rows="1" maxlength="300" placeholder="Digite sua pergunta..." enterkeyhint="send"></textarea>
    <button class="send" id="enviar" aria-label="Enviar pergunta">&#8593;</button>
  </div>
  <div class="hint">Sua pergunta aparece no totem e a Lia responde em voz alta.</div>
</footer>
<script>
const CONFIG = __CONFIG__;
const LABELS = {
  carregando: "acordando a Lia...",
  pronto: "pode perguntar",
  pensando: "pensando...",
  falando: "falando...",
  erro: "tive um problema, tente de novo"
};
const $ = (id) => document.getElementById(id);
$("evento").textContent = CONFIG.evento || "recepção virtual";
document.title = "Lia · " + (CONFIG.evento || "recepção");

const chips = $("chips");
for (const titulo of CONFIG.perguntas || []) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = titulo;
  b.addEventListener("click", () => {
    $("texto").value = titulo;
    send();
  });
  chips.appendChild(b);
}

let ultimoTexto = "";
function aplicar(s) {
  const emConversa = s.estado === "conversa" && s.pergunta;
  $("quem").textContent = emConversa ? "Você perguntou" : "Lia";
  $("pergunta").hidden = !emConversa;
  if (emConversa) $("pergunta").textContent = s.pergunta;
  const texto = (s.resposta && s.resposta.trim()) ? s.resposta : (CONFIG.boas_vindas || "Olá! Como posso ajudar?");
  if (texto !== ultimoTexto) {
    ultimoTexto = texto;
    $("resposta").textContent = texto;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  $("status").textContent = LABELS[s.status] || "pode perguntar";
}

async function tick() {
  try {
    const r = await fetch("api/estado", { cache: "no-store" });
    if (r.ok) aplicar(await r.json());
  } catch (e) {
    $("status").textContent = "sem conexão com o totem";
  }
}

async function send() {
  const texto = $("texto").value.trim();
  if (texto.length < 2) return;
  const botao = $("enviar");
  botao.disabled = true;
  try {
    const r = await fetch("api/mensagem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto })
    });
    if (r.ok) {
      $("texto").value = "";
      $("texto").style.height = "auto";
      $("status").textContent = "enviado! a Lia já vai responder";
    } else {
      $("status").textContent = "não consegui enviar. tente de novo.";
    }
  } catch (e) {
    $("status").textContent = "não consegui enviar. tente de novo.";
  } finally {
    setTimeout(() => { botao.disabled = false; }, 700);
  }
}

$("enviar").addEventListener("click", send);
$("texto").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("texto").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(120, e.target.scrollHeight) + "px";
});
tick();
setInterval(tick, 900);
</script>
</body>
</html>
"""
