# main.py — Twilio <-> OpenAI Realtime
# PASO 10-B: Cargar voz, instrucciones y modelo desde bots/inhoustontexas.json
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Start
from dotenv import load_dotenv
from simple_websocket import Server, ConnectionClosed
import websocket  # websocket-client
import os, json, time, pathlib

load_dotenv()
app = Flask(__name__)

BASE_DIR = pathlib.Path(__file__).resolve().parent
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# -------------------------------
# Utilidades para el bot (JSON)
# -------------------------------
def load_bot_config(bot_name: str = "inhoustontexas") -> dict:
    """
    Lee bots/<bot_name>.json y devuelve un dict.
    Tolerante a errores, con defaults seguros.
    """
    cfg_path = BASE_DIR / "bots" / f"{bot_name}.json"
    cfg = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        print(f"[BOT] ✅ Cargado {cfg_path}")
    except Exception as e:
        print(f"[BOT] ⚠️ No se pudo leer {cfg_path}: {e}")
    return cfg or {}

def choose_text(cfg: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def build_instructions(cfg: dict) -> str:
    # Orden de preferencia de campos típicos
    candidates = [
        "instructions", "system", "system_prompt", "persona", "prompt", "context"
    ]
    chunks = []
    for k in candidates:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            chunks.append(v.strip())
    if chunks:
        return "\n".join(chunks)
    # Fallback
    name = cfg.get("name", "Asistente")
    return f"Eres {name}, un asistente de voz en tiempo real. Responde en español, breve, amable y útil."

def get_voice(cfg: dict) -> str:
    return (
        choose_text(cfg, ["voice", "voz"])
        or os.environ.get("OPENAI_VOICE")
        or "nova"
    )

def get_model(cfg: dict) -> str:
    return (
        choose_text(cfg, ["realtime_model", "model"])
        or os.environ.get("OPENAI_REALTIME_MODEL")
        or "gpt-4o-realtime-preview"
    )

# -------------------------------
# OpenAI Realtime helpers
# -------------------------------
def openai_ws_connect(model: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta: realtime=v1"
    ]
    ws = websocket.create_connection(url, header=headers, suppress_origin=True)
    return ws

def openai_send(ws_ai, obj: dict):
    ws_ai.send(json.dumps(obj))

# -------------------------------
# Rutas HTTP (TwiML y health)
# -------------------------------
@app.route("/", methods=["GET"])
def root():
    return "✅ RT core activo (llamadas-multi-bots).", 200

@app.route("/voice", methods=["POST", "GET"])
def voice():
    """
    Twilio Voice Webhook:
    - Devuelve TwiML que abre un Media Stream hacia nuestro WebSocket (/media-stream).
    - El contenido real (voz/instrucciones/modelo) viene del bot JSON.
    """
    # Construir URL pública del WS
    base_ws = os.environ.get("PUBLIC_WS_URL")
    if not base_ws:
        base = request.url_root.replace("https", "wss").replace("http", "ws")
        base_ws = (base.rstrip("/") + "/media-stream")

    vr = VoiceResponse()
    # Mensaje breve de espera (Twilio <Say>); OpenAI hablará por WS
    vr.say("Conectando con el asistente en tiempo real.", language="es-ES", voice="Polly.Mia")

    start = Start()
    # both_tracks: bidireccional para poder enviar audio a Twilio
    start.stream(url=base_ws, track="both_tracks")
    vr.append(start)
    return Response(str(vr), mimetype="text/xml")

# -------------------------------
# WebSocket Twilio Media Streams
# -------------------------------
@app.route("/media-stream", methods=["GET"])
def media_stream():
    """
    WS de Twilio. En cada conexión:
    - Carga bots/inhoustontexas.json
    - Se conecta a OpenAI Realtime con modelo/voz/instrucciones del JSON
    - Envía un saludo inicial (voz Nova u otra definida en JSON) en g711_ulaw/8k
    """
    # Negociar WS; si no es WebSocket, devolver 426
    try:
        ws_twilio = Server(request.environ)
    except Exception:
        return Response("Upgrade Required: use WebSocket here.", status=426)

    # Cargar bot (cerebro)
    bot_cfg = load_bot_config("inhoustontexas")
    VOICE_NAME = get_voice(bot_cfg)            # p.ej., "nova" si está en JSON
    REALTIME_MODEL = get_model(bot_cfg)        # p.ej., "gpt-4o-realtime-preview"
    INSTRUCTIONS = build_instructions(bot_cfg) # personalidad, reglas, etc.

    print(f"[BOT] voz={VOICE_NAME} model={REALTIME_MODEL}")

    stream_sid = None
    ws_ai = None
    started_at = time.time()

    try:
        while True:
            msg = ws_twilio.receive()
            if msg is None:
                break

            # Twilio envía JSON
            try:
                data = json.loads(msg)
            except Exception:
                print("[WS] ⚠️ Mensaje no JSON desde Twilio (ignorado)")
                continue

            etype = data.get("event")

            if etype == "start":
                stream_sid = data.get("start", {}).get("streamSid")
                sample_rate = data.get("start", {}).get("sampleRate")
                print(f"[WS] ▶️ start streamSid={stream_sid} sr={sample_rate}")

                # Conectar a OpenAI y configurar sesión con info del bot
                try:
                    ws_ai = openai_ws_connect(REALTIME_MODEL)
                    print("[AI] ✅ Conectado a OpenAI Realtime.")

                    # Configurar sesión: voz + formato telefónico + instrucciones
                    openai_send(ws_ai, {
                        "type": "session.update",
                        "session": {
                            "voice": VOICE_NAME,
                            "audio_format": "g711_ulaw",
                            "sample_rate": 8000,
                            "instructions": INSTRUCTIONS
                        }
                    })

                    # Pedir un saludo inicial (solo para validar flujo)
                    openai_send(ws_ai, {
                        "type": "response.create",
                        "response": {
                            "instructions": "Saluda brevemente según tu personalidad y ofrece ayuda.",
                            "modalities": ["audio"],
                            "audio": {
                                "voice": VOICE_NAME,
                                "format": "g711_ulaw",
                                "sample_rate": 8000
                            }
                        }
                    })

                    # Drenar respuestas de OpenAI y reenviar audio a Twilio
                    def _drain_ai():
                        try:
                            while True:
                                raw = ws_ai.recv()
                                if not raw:
                                    break
                                try:
                                    payload = json.loads(raw)
                                except Exception:
                                    continue

                                mtype = payload.get("type")

                                if mtype == "output_audio.delta":
                                    b64audio = payload.get("audio") or payload.get("delta")
                                    if b64audio and stream_sid:
                                        ws_twilio.send(json.dumps({
                                            "event": "media",
                                            "streamSid": stream_sid,
                                            "media": {"payload": b64audio},
                                            "track": "outbound"
                                        }))
                                elif mtype == "response.completed":
                                    if stream_sid:
                                        ws_twilio.send(json.dumps({
                                            "event": "mark",
                                            "streamSid": stream_sid,
                                            "mark": {"name": "ai_response_done"}
                                        }))
                                else:
                                    # Logs útiles (omitimos eventos de silencio/detección)
                                    if mtype not in ("input_audio_buffer.speech_started",
                                                     "input_audio_buffer.speech_stopped"):
                                        print(f"[AI] ← {mtype}")
                        except Exception as e:
                            print(f"[AI] ❌ Error leyendo WS Realtime: {e}")

                    eventlet.spawn_n(_drain_ai)

                except Exception as e:
                    print(f"[AI] ❌ No se pudo conectar a OpenAI Realtime: {e}")

            elif etype == "media":
                # (Siguiente paso) aquí enviaremos el audio entrante a OpenAI para ASR.
                pass

            elif etype == "stop":
                dur = time.time() - started_at
                print(f"[WS] ⏹ stop streamSid={stream_sid} duración={dur:.1f}s")
                break

            else:
                pass

    except ConnectionClosed:
        print("[WS] 🔌 WS Twilio cerrado por el cliente.")
    except Exception as e:
        print(f"[WS] ❌ Error WS Twilio: {e}")
    finally:
        try:
            if ws_ai:
                try:
                    ws_ai.close()
                except Exception:
                    pass
            ws_twilio.close()
        except Exception:
            pass
        print("[WS] ✅ Media stream finalizado.")
    return ""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
