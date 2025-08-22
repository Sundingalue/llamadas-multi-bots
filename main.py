# main.py — Twilio <-> OpenAI Realtime con handshake WS correcto (PASO 10-E)
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Start
from dotenv import load_dotenv
from eventlet import websocket  # <-- handshake WS con subprotocolos
import websocket as wsclient     # websocket-client (para OpenAI)
import os, json, time, pathlib

load_dotenv()
app = Flask(__name__)

BASE_DIR = pathlib.Path(__file__).resolve().parent
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ---------- Utilidades BOT ----------
def load_bot_config(bot_name: str = "inhoustontexas") -> dict:
    cfg_path = BASE_DIR / "bots" / f"{bot_name}.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        print(f"[BOT] ✅ Cargado {cfg_path}")
        return cfg
    except Exception as e:
        print(f"[BOT] ⚠️ No se pudo leer {cfg_path}: {e}")
        return {}

def _pick(cfg: dict, keys):
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def get_voice(cfg: dict) -> str:
    return _pick(cfg, ["voice", "voz"]) or os.environ.get("OPENAI_VOICE") or "nova"

def get_model(cfg: dict) -> str:
    return _pick(cfg, ["realtime_model", "model"]) or os.environ.get("OPENAI_REALTIME_MODEL") or "gpt-4o-realtime-preview"

def get_instructions(cfg: dict) -> str:
    return _pick(cfg, ["system_prompt","instructions","persona","prompt"]) or \
           "Eres un asistente de voz en tiempo real. Responde en español, breve y amable."

# ---------- OpenAI Realtime ----------
def openai_ws_connect(model: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta: realtime=v1"
    ]
    # Cliente WS hacia OpenAI
    return wsclient.create_connection(url, header=headers, suppress_origin=True)

def openai_send(ws_ai, obj: dict):
    ws_ai.send(json.dumps(obj))

# ---------- HTTP (TwiML/health) ----------
@app.route("/", methods=["GET"])
def root():
    return "✅ RT core activo (llamadas-multi-bots).", 200

@app.route("/voice", methods=["POST", "GET"])
def voice():
    # URL pública del WS (para Twilio Media Streams)
    base_ws = os.environ.get("PUBLIC_WS_URL")
    if not base_ws:
        base = request.url_root.replace("https", "wss").replace("http", "ws")
        base_ws = (base.rstrip("/") + "/media-stream")

    vr = VoiceResponse()
    vr.say("Conectando con el asistente en tiempo real.", language="es-ES", voice="Polly.Mia")
    start = Start()
    # MUY IMPORTANTE: Twilio abrirá un WebSocket a /media-stream
    start.stream(url=base_ws, track="both_tracks")
    vr.append(start)
    return Response(str(vr), mimetype="text/xml")

# ---------- WS handler (Twilio) ----------
def twilio_media_ws(ws):
    """
    Handler del WebSocket de Twilio.
    - Aquí sí negociamos subprotocolo 'audio' con eventlet.websocket.
    - Al 'start' conectamos a OpenAI Realtime y enviamos un saludo en g711_ulaw/8k.
    """
    bot_cfg = load_bot_config("inhoustontexas")
    VOICE = get_voice(bot_cfg)
    MODEL = get_model(bot_cfg)
    INSTRUCTIONS = get_instructions(bot_cfg)
    print(f"[BOT] voz={VOICE} model={MODEL}")

    stream_sid = None
    started_at = time.time()
    ws_ai = None

    # Lector de OpenAI -> reenviar audio a Twilio
    def drain_ai():
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
                        ws.send(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": b64audio},
                            "track": "outbound"
                        }))
                elif mtype == "response.completed":
                    if stream_sid:
                        ws.send(json.dumps({
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "ai_response_done"}
                        }))
                else:
                    if mtype not in ("input_audio_buffer.speech_started",
                                     "input_audio_buffer.speech_stopped"):
                        print(f"[AI] ← {mtype}")
        except Exception as e:
            print(f"[AI] ❌ Error leyendo WS Realtime: {e}")

    try:
        while True:
            msg = ws.wait()
            if msg is None:
                break

            try:
                data = json.loads(msg)
            except Exception:
                print("[WS] ⚠️ mensaje no JSON (ignorado)")
                continue

            etype = data.get("event")

            if etype == "start":
                stream_sid = data.get("start", {}).get("streamSid")
                sr = data.get("start", {}).get("sampleRate")
                print(f"[WS] ▶️ start streamSid={stream_sid} sr={sr}")

                try:
                    ws_ai = openai_ws_connect(MODEL)
                    print("[AI] ✅ Conectado a OpenAI Realtime.")

                    openai_send(ws_ai, {
                        "type": "session.update",
                        "session": {
                            "voice": VOICE,
                            "audio_format": "g711_ulaw",
                            "sample_rate": 8000,
                            "instructions": INSTRUCTIONS
                        }
                    })

                    openai_send(ws_ai, {
                        "type": "response.create",
                        "response": {
                            "instructions": "Saluda brevemente según tu personalidad y ofrece ayuda.",
                            "modalities": ["audio"],
                            "audio": {
                                "voice": VOICE,
                                "format": "g711_ulaw",
                                "sample_rate": 8000
                            }
                        }
                    })

                    eventlet.spawn_n(drain_ai)

                except Exception as e:
                    print(f"[AI] ❌ No se pudo conectar a OpenAI Realtime: {e}")

            elif etype == "media":
                # (Siguiente paso: enviaremos audio entrante a OpenAI)
                pass

            elif etype == "stop":
                dur = time.time() - started_at
                print(f"[WS] ⏹ stop streamSid={stream_sid} duración={dur:.1f}s")
                break

    except websocket.WebSocketError:
        print("[WS] 🔌 WS Twilio cerrado.")
    except Exception as e:
        print(f"[WS] ❌ Error WS Twilio: {e}")
    finally:
        try:
            if ws_ai:
                try: ws_ai.close()
                except: pass
            ws.close()
        except: pass
        print("[WS] ✅ Media stream finalizado.")

# ---------- Dispatcher WSGI ----------
# En lugar de exponer "app", exponemos un "wsgi_app" que enruta:
# - /media-stream -> handler WS con subprotocolo 'audio'
# - resto de rutas -> Flask
def wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path == "/media-stream":
        # ACEPTAR subprotocolo 'audio' (Twilio lo exige)
        return websocket.WebSocketWSGI(twilio_media_ws, protocols=["audio"])(environ, start_response)
    # Resto: Flask
    return app.wsgi_app(environ, start_response)
