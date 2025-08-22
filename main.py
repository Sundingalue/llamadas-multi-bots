# main.py — FastAPI + Twilio Connect/Stream + OpenAI Realtime (voz)
# - Twilio: /voice (TwiML con <Connect><Stream>)
# - WS: /media-stream (acepta 'audio', carga bot por query ?bot=...)
# - Enrutamiento por número: NUMBER_TO_BOT
# - Audio de salida: g711_ulaw (8000 Hz)

import os, json, time, pathlib, threading, asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
import websocket as wsclient  # websocket-client (cliente WS hacia OpenAI)

load_dotenv()
app = FastAPI()
BASE_DIR = pathlib.Path(__file__).resolve().parent
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ====== Enrutamiento por número (ajusta aquí) ======
NUMBER_TO_BOT = {
    "+13469882323": "inhoustontexas",   # <- este número usa el bot de In Houston Texas
    # "+1OTRO_NUMERO": "otro-bot",
}
DEFAULT_BOT = "inhoustontexas"

# URLs públicas (puedes dejarlas así o sobreescribir con env)
PUBLIC_WS_BASE = os.environ.get("PUBLIC_WS_BASE", "wss://llamadas-multi-bots.onrender.com")
PUBLIC_HTTP_BASE = os.environ.get("PUBLIC_HTTP_BASE", "https://llamadas-multi-bots.onrender.com")

# ========= Bot config (JSON) =========
def load_bot_config(bot_name: str) -> dict:
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

# ========= OpenAI Realtime (cliente WS) =========
def openai_ws_connect(model: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta: realtime=v1"
    ]
    return wsclient.create_connection(url, header=headers, suppress_origin=True)

def openai_send(ws_ai, obj: dict):
    ws_ai.send(json.dumps(obj))

# ========= Health =========
@app.get("/", response_class=PlainTextResponse)
def root():
    return "✅ RT core activo (llamadas-multi-bots)."

# ========= TwiML /voice (usa <Connect><Stream>) =========
@app.api_route("/voice", methods=["POST", "GET"])
async def voice(request: Request):
    """
    Recibe la llamada de Twilio, detecta el número destino (Called),
    resuelve el bot y construye el WS con ?bot=<name>.
    """
    # Twilio envía POST form-urlencoded
    form = {}
    try:
        form = dict(await request.form())
    except Exception:
        pass

    called = (form.get("Called") or request.query_params.get("Called") or "").strip()
    if not called:
        # Twilio también usa 'To' en algunos flujos
        called = (form.get("To") or request.query_params.get("To") or "").strip()

    bot_name = NUMBER_TO_BOT.get(called, DEFAULT_BOT)
    ws_url = f"{PUBLIC_WS_BASE}/media-stream?bot={bot_name}"

    vr = VoiceResponse()
    vr.say("Conectando.", language="es-ES", voice="Polly.Mia")

    connect = Connect()
    # IMPORTANTE: sin 'track' en <Connect><Stream> para evitar "Invalid Track configuration"
    connect.stream(
        url=ws_url,
        status_callback=f"{PUBLIC_HTTP_BASE}/twilio/stream-status",
        status_callback_method="POST",
        status_callback_event="start stop mark clear"
    )
    vr.append(connect)

    xml = str(vr)
    print(f"[VOICE-CONNECT] Called={called or 'N/A'} Bot={bot_name}\n{xml}")
    return Response(content=xml, media_type="text/xml")

# ========= Callback de estado del Stream (diagnóstico) =========
@app.post("/twilio/stream-status")
async def twilio_stream_status(request: Request):
    try:
        body = (await request.body()).decode("utf-8", "ignore")
        print(f"[TWILIO-CB] {body}")
    except Exception as e:
        print(f"[TWILIO-CB] error parseando: {e}")
    return Response(status_code=200)

# ========= WebSocket Twilio =========
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    # Twilio suele enviar subprotocolo 'audio'
    await websocket.accept(subprotocol="audio")

    # Log de handshake
    try:
        proto = websocket.headers.get("sec-websocket-protocol")
        ua = websocket.headers.get("user-agent")
        host = websocket.headers.get("host")
        print(f"[WS-HANDSHAKE] path=/media-stream host={host} proto={proto} ua={ua}")
    except Exception:
        pass

    # Determinar el bot de la query (?bot=...)
    bot_name = websocket.query_params.get("bot") or DEFAULT_BOT
    bot_cfg = load_bot_config(bot_name)
    VOICE = get_voice(bot_cfg)
    MODEL = get_model(bot_cfg)
    INSTRUCTIONS = get_instructions(bot_cfg)
    print(f"[BOT] bot={bot_name} voz={VOICE} model={MODEL}")

    stream_sid = None
    ws_ai = None
    started_at = time.time()
    stop_event = threading.Event()

    # Cola para enviar mensajes hacia Twilio desde un hilo
    outq: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    async def sender_task():
        while True:
            obj = await outq.get()
            try:
                await websocket.send_text(json.dumps(obj))
            except Exception as e:
                print(f"[WS] ❌ Error enviando a Twilio: {e}")
                break

    sender = asyncio.create_task(sender_task())

    def emit_to_twilio(obj: dict):
        """Thread-safe: encolar envío hacia Twilio."""
        try:
            loop.call_soon_threadsafe(outq.put_nowait, obj)
        except Exception as e:
            print(f"[WS] ❌ No se pudo encolar mensaje a Twilio: {e}")

    def read_ai_forever():
        """Lee eventos de OpenAI y reenvía audio al WS de Twilio (outbound)."""
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
                        emit_to_twilio({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": b64audio},
                            "track": "outbound"
                        })
                elif mtype == "response.completed":
                    if stream_sid:
                        emit_to_twilio({
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "ai_response_done"}
                        })
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

                    threading.Thread(target=read_ai_forever, daemon=True).start()

                except Exception as e:
                    print(f"[AI] ❌ No se pudo conectar a OpenAI Realtime: {e}")

            elif etype == "media":
                # Próximo paso: enviar audio entrante a OpenAI (ASR/voice-in).
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
                try:
                    ws_ai.close()
                except:
                    pass
            sender.cancel()
        finally:
            print("[WS] ✅ Media stream finalizado.")
