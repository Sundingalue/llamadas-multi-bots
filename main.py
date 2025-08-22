# main.py — FastAPI + Twilio Media Streams + OpenAI Realtime (voz)
# - WS de Twilio en /media-stream aceptando subprotocolo 'audio'
# - TwiML en /voice con <Start><Stream> + status callback
# - Bot/persona desde bots/inhoustontexas.json
# - Salida de audio OpenAI en g711_ulaw (8000 Hz) hacia la llamada

import os, json, time, pathlib, threading, asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Start
import websocket as wsclient  # websocket-client (cliente WS hacia OpenAI)

load_dotenv()
app = FastAPI()
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
    return _pick(cfg, ["system_prompt","instructions","persona","prompt"]) or \
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
    ws = wsclient.create_connection(url, header=headers, suppress_origin=True)
    return ws

def openai_send(ws_ai, obj: dict):
    ws_ai.send(json.dumps(obj))

# =========================
# Health
# =========================
@app.get("/", response_class=PlainTextResponse)
def root():
    return "✅ RT core activo (llamadas-multi-bots)."

# =========================
# TwiML /voice
# =========================
@app.api_route("/voice", methods=["POST", "GET"])
def voice(request: Request):
    # URL WS pública
    base_ws = os.environ.get("PUBLIC_WS_URL")
    if not base_ws:
        base = str(request.url_for("root")).replace("https", "wss").replace("http", "ws").rstrip("/")
        base_ws = f"{base}/media-stream"

    # URL HTTP para callbacks
    public_http = str(request.url_for("root")).rstrip("/")

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
    xml = str(vr)
    return Response(content=xml, media_type="text/xml")

# =========================
# Callback de estado del Stream (diagnóstico)
# =========================
@app.post("/twilio/stream-status")
async def twilio_stream_status(request: Request):
    try:
        body = (await request.body()).decode("utf-8", "ignore")
        print(f"[TWILIO-CB] {body}")
    except Exception as e:
        print(f"[TWILIO-CB] error parseando: {e}")
    return Response(status_code=200)

# =========================
# WebSocket Twilio
# =========================
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    # Aceptamos el subprotocolo 'audio' requerido por Twilio
    await websocket.accept(subprotocol="audio")
    # Logs de handshake
    try:
        proto = websocket.headers.get("sec-websocket-protocol")
        ua = websocket.headers.get("user-agent")
        host = websocket.headers.get("host")
        print(f"[WS-HANDSHAKE] path=/media-stream host={host} proto={proto} ua={ua}")
    except Exception:
        pass

    # Cargar bot y preparar OpenAI
    bot_cfg = load_bot_config("inhoustontexas")
    VOICE = get_voice(bot_cfg)
    MODEL = get_model(bot_cfg)
    INSTRUCTIONS = get_instructions(bot_cfg)
    print(f"[BOT] voz={VOICE} model={MODEL}")

    stream_sid = None
    ws_ai = None
    started_at = time.time()
    stop_event = threading.Event()

    async def send_to_twilio(obj: dict):
        await websocket.send_text(json.dumps(obj))

    def read_ai_forever():
        try:
            while not stop_event.is_set():
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
                        obj = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": b64audio},
                            "track": "outbound"
                        }
                        asyncio.run(asyncio.create_task(send_to_twilio(obj)))
                elif mtype == "response.completed":
                    if stream_sid:
                        asyncio.run(asyncio.create_task(send_to_twilio({
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "ai_response_done"}
                        })))
                else:
                    if mtype not in ("input_audio_buffer.speech_started",
                                     "input_audio_buffer.speech_stopped"):
                        print(f"[AI] ← {mtype}")
        except Exception as e:
            print(f"[AI] ❌ Error leyendo WS Realtime: {e}")

    try:
        while True:
            msg = await websocket.receive_text()
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

                    # Configurar sesión con voz e instrucciones (g711_ulaw / 8k)
                    openai_send(ws_ai, {
                        "type": "session.update",
                        "session": {
                            "voice": VOICE,
                            "audio_format": "g711_ulaw",
                            "sample_rate": 8000,
                            "instructions": INSTRUCTIONS
                        }
                    })

                    # Saludo inicial
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

                    # Hilo que lee a OpenAI y reenvía audio a Twilio
                    threading.Thread(target=read_ai_forever, daemon=True).start()

                except Exception as e:
                    print(f"[AI] ❌ No se pudo conectar a OpenAI Realtime: {e}")

            elif etype == "media":
                # (Próximo paso): enviar audio entrante a OpenAI para ASR.
                pass

            elif etype == "stop":
                dur = time.time() - started_at
                print(f"[WS] ⏹ stop streamSid={stream_sid} duración={dur:.1f}s")
                break

    except WebSocketDisconnect:
        print("[WS] 🔌 WS Twilio desconectado.")
    except Exception as e:
        print(f"[WS] ❌ Error WS Twilio: {e}")
    finally:
        try:
            stop_event.set()
            if ws_ai:
                try: ws_ai.close()
                except: pass
        finally:
            print("[WS] ✅ Media stream finalizado.")
