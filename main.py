# main.py — Twilio <-> OpenAI Realtime (voz) con handshake WS correcto
# - WS Twilio en /media-stream con subprotocolo 'audio' (eventlet.websocket)
# - TwiML /voice con <Start><Stream> y statusCallback
# - Bot/persona desde bots/inhoustontexas.json
# - Salida de audio OpenAI en g711_ulaw/8000 hacia la llamada

import eventlet
eventlet.monkey_patch()

from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Start
from dotenv import load_dotenv
from eventlet import websocket  # WebSocket server (handshake con subprotocolos)
import websocket as wsclient     # websocket-client (cliente WS hacia OpenAI)
import os, json, time, pathlib

load_dotenv()
app = Flask(__name__)

BASE_DIR = pathlib.Path(__file__).resolve().parent
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# =========================
# Utilidades del bot (JSON)
# =========================
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
    return _pick(cfg, ["system_prompt", "instructions", "persona", "prompt"]) or \
           "Eres un asistente de voz en tiempo real. Responde en español, breve y amable."

# =========================
# OpenAI Realtime (cliente)
# =========================
def openai_ws_connect(model: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta: realtime=v1"
    ]
    # Cliente WS hacia OpenAI
    ws = wsclient.create_connection(url, header=headers, suppress_origin=True)
    return ws

def openai_send(ws_ai, obj: dict):
    ws_ai.send(json.dumps(obj))

# =========================
# Rutas HTTP (health + TwiML)
# =========================
@app.route("/", methods=["GET"])
def root():
    return "✅ RT core activo (llamadas-multi-bots).", 200

@app.route("/voice", methods=["POST", "GET"])
def voice():
    """
    Devuelve TwiML que:
    - Dice un breve mensaje (<Say>)
    - Abre Stream WS a /media-stream (track=both_tracks)
    - Envía statusCallback a /twilio/stream-status (start/stop/mark/clear)
    """
    # URL WS pública
    base_ws = os.environ.get("PUBLIC_WS_URL")
    if not base_ws:
        base = request.url_root.replace("https", "wss").replace("http", "ws").rstrip("/")
        base_ws = f"{base}/media-stream"

    # URL HTTP pública para callbacks
    public_http = request.url_root.rstrip("/")

    vr = VoiceResponse()
    vr.say("Conectando con el asistente en tiempo real.", language="es-ES", voice="Polly.Mia")

    start = Start()
    start.stream(
        url=base_ws,
        track="both_tracks",
        status_callback=f"{public_http}/twilio/stream-status",
        status_callback_method="POST",
        status_callback_event="start stop mark clear"
    )
    vr.append(start)
    return Response(str(vr), mimetype="text/xml")

@app.route("/twilio/stream-status", methods=["POST"])
def twilio_stream_status():
    """ Recibe callbacks HTTP del <Stream> (diagnóstico de inicio/fin). """
    try:
        form = request.form.to_dict()
        print(f"[TWILIO-CB] {json.dumps(form)}")
    except Exception as e:
        print(f"[TWILIO-CB] error parseando: {e}")
    return ("", 200)

# =========================
# WebSocket Twilio (server)
# =========================
def twilio_media_ws(ws):
    """
    Handler del WebSocket de Twilio.
    - Se acepta con subprotocolo 'audio' (ver wsgi_app).
    - Al 'start' conectamos a OpenAI Realtime y enviamos saludo en g711_ulaw/8000.
    - (Entrada de audio del usuario se activará en el siguiente paso).
    """
    bot_cfg = load_bot_config("inhoustontexas")
    VOICE = get_voice(bot_cfg)
    MODEL = get_model(bot_cfg)
    INSTRUCTIONS = get_instructions(bot_cfg)
    print(f"[BOT] voz={VOICE} model={MODEL}")

    stream_sid = None
    ws_ai = None
    started_at = time.time()

    # Lee mensajes de OpenAI y reenvía audio a Twilio
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
                ua = data.get("start", {}).get("userAgent")
                print(f"[WS] ▶️ start streamSid={stream_sid} sr={sr} ua={ua}")

                try:
                    ws_ai = openai_ws_connect(MODEL)
                    print("[AI] ✅ Conectado a OpenAI Realtime.")

                    # Configurar sesión: voz + formato telefónico + instrucciones del bot
                    openai_send(ws_ai, {
                        "type": "session.update",
                        "session": {
                            "voice": VOICE,
                            "audio_format": "g711_ulaw",
                            "sample_rate": 8000,
                            "instructions": INSTRUCTIONS
                        }
                    })

                    # Saludo inicial para validar flujo
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
                # Próximo paso: enviar audio entrante a OpenAI para ASR.
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
                try:
                    ws_ai.close()
                except:
                    pass
            ws.close()
        except:
            pass
        print("[WS] ✅ Media stream finalizado.")

# =========================
# Dispatcher WSGI
# =========================
def wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path == "/media-stream":
        # Log del handshake para diagnóstico
        proto_hdr = environ.get("HTTP_SEC_WEBSOCKET_PROTOCOL")
        ua_hdr = environ.get("HTTP_USER_AGENT")
        host = environ.get("HTTP_HOST")
        print(f"[WS-HANDSHAKE] path={path} host={host} proto={proto_hdr} ua={ua_hdr}")

        # Aceptar subprotocolo 'audio' que envía Twilio
        return websocket.WebSocketWSGI(twilio_media_ws, protocols=["audio"])(environ, start_response)

    # Resto de rutas -> Flask
    return app.wsgi_app(environ, start_response)

if __name__ == "__main__":
    # Útil para pruebas locales (no usado en Render)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
