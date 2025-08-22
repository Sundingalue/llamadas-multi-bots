# main.py — FastAPI + Twilio Connect/Stream + OpenAI Realtime (voz bidireccional)
# Requisitos:
# - Twilio con Bidirectional Media Streams habilitado (both_tracks)
# - OPENAI_API_KEY en variables de entorno

import os, json, time, pathlib, threading, asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
import websocket as wsclient  # websocket-client

load_dotenv()
app = FastAPI()
BASE_DIR = pathlib.Path(__file__).resolve().parent
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Ruta pública del servicio (ajusta si usas otro dominio)
PUBLIC_WS_BASE   = os.environ.get("PUBLIC_WS_BASE",   "wss://llamadas-multi-bots.onrender.com")
PUBLIC_HTTP_BASE = os.environ.get("PUBLIC_HTTP_BASE", "https://llamadas-multi-bots.onrender.com")

# Enrutamiento por número -> bot
NUMBER_TO_BOT = {
    "+13469882323": "inhoustontexas",  # tu número → bot
}
DEFAULT_BOT = "inhoustontexas"

# ---------- utilidades de bot ----------
def load_bot_config(bot_name: str) -> dict:
    cfg_path = BASE_DIR / "bots" / f"{bot_name}.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[BOT] ⚠️ No se pudo leer {cfg_path}: {e}")
        return {}

def _pick(cfg: dict, keys):
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def normalize_voice(v: str) -> str:
    if not v:
        return "alloy"
    vlow = v.lower().strip()
    supported = {"alloy", "verse", "aria", "sage"}
    if vlow in supported:
        return vlow
    # normalizamos voces no soportadas (ej. "nova", "Polly.Mia") a "alloy"
    return "alloy"

def get_voice(cfg: dict) -> str:
    raw = _pick(cfg, ["voice", "voz"]) or os.environ.get("OPENAI_VOICE") or "alloy"
    v = normalize_voice(raw)
    if v != raw:
        print(f"[BOT] ⚠️ Voz '{raw}' no soportada; usando '{v}'.")
    return v

def get_model(cfg: dict) -> str:
    return _pick(cfg, ["realtime_model", "model"]) or os.environ.get("OPENAI_REALTIME_MODEL") or "gpt-4o-realtime-preview"

def get_instructions(cfg: dict) -> str:
    return _pick(cfg, ["system_prompt", "instructions", "persona", "prompt"]) or \
           "Eres un asistente de voz en tiempo real. Responde en español, breve y amable."

# ---------- Cliente WS OpenAI ----------
def openai_ws_connect(model: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta: realtime=v1"
    ]
    return wsclient.create_connection(url, header=headers, suppress_origin=True)

def openai_send(ws_ai, obj: dict):
    ws_ai.send(json.dumps(obj))

# ---------- Health ----------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "✅ RT core activo (llamadas-multi-bots)."

# ---------- TwiML /voice ----------
@app.api_route("/voice", methods=["POST", "GET"])
async def voice(request: Request):
    form = {}
    try:
        form = dict(await request.form())
    except Exception:
        pass

    called = (form.get("Called") or request.query_params.get("Called") or "").strip()
    if not called:
        called = (form.get("To") or request.query_params.get("To") or "").strip()

    bot_name = NUMBER_TO_BOT.get(called, DEFAULT_BOT)
    ws_url = f"{PUBLIC_WS_BASE}/media-stream?bot={bot_name}"

    vr = VoiceResponse()
    vr.say("Conectando con el asistente en tiempo real.", language="es-ES", voice="Polly.Mia")

    connect = Connect()
    # *** IMP: require habilitar Bidirectional Media Streams en Twilio ***
    connect.stream(
        url=ws_url,
        track="both_tracks",
        status_callback=f"{PUBLIC_HTTP_BASE}/twilio/stream-status",
        status_callback_method="POST",
        status_callback_event="start stop mark clear"
    )
    vr.append(connect)

    xml = str(vr)
    print(f"[VOICE-CONNECT] Called={called or 'N/A'} Bot={bot_name}\n{xml}")
    return Response(content=xml, media_type="text/xml")

# ---------- Callback estado Twilio ----------
@app.post("/twilio/stream-status")
async def twilio_stream_status(request: Request):
    try:
        body = (await request.body()).decode("utf-8", "ignore")
        print(f"[TWILIO-CB] {body}")
    except Exception as e:
        print(f"[TWILIO-CB] error parseando: {e}")
    return Response(status_code=200)

# ---------- WebSocket Twilio (bidireccional) ----------
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept(subprotocol="audio")  # Twilio requiere subprotocolo 'audio'

    try:
        print(f"[WS-HANDSHAKE] headers OK")
    except Exception:
        pass

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

                t = payload.get("type")
                if t == "response.audio.delta":
                    # OpenAI envía 'delta' (base64 PCM16). Twilio espera 'media.payload'
                    b64audio = payload.get("delta") or payload.get("audio")
                    if b64audio and stream_sid:
                        emit_to_twilio({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": b64audio},
                            "track": "outbound"  # clave para reproducción en la llamada
                        })
                elif t == "response.completed":
                    if stream_sid:
                        emit_to_twilio({
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "ai_response_done"}
                        })
                elif t == "error":
                    print(f"[AI] ❌ error payload: {json.dumps(payload, ensure_ascii=False)}")
                else:
                    # logs útiles (transcript, done, etc.)
                    if t.startswith("response."):
                        print(f"[AI] ← {t}: {json.dumps(payload, ensure_ascii=False)[:300]}")
        except Exception as e:
            print(f"[AI] ❌ Error leyendo WS Realtime: {e}")

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
            except Exception:
                continue

            etype = data.get("event")

            if etype == "start":
                stream_sid = data.get("start", {}).get("streamSid")
                print(f"[WS] ▶️ start streamSid={stream_sid}")

                try:
                    ws_ai = openai_ws_connect(MODEL)
                    print("[AI] ✅ Conectado a OpenAI Realtime.")

                    # configuración mínima válida
                    openai_send(ws_ai, {
                        "type": "session.update",
                        "session": {
                            "voice": VOICE,
                            "instructions": INSTRUCTIONS
                        }
                    })

                    # Pedimos salida combinada audio+texto (válida)
                    openai_send(ws_ai, {
                        "type": "response.create",
                        "response": {
                            "instructions": "Saluda brevemente según tu personalidad y ofrece ayuda.",
                            "modalities": ["audio", "text"]
                        }
                    })

                    threading.Thread(target=read_ai_forever, daemon=True).start()

                except Exception as e:
                    print(f"[AI] ❌ No se pudo conectar a OpenAI Realtime: {e}")

            elif etype == "media":
                # Aquí podríamos enviar el audio entrante a OpenAI si lo necesitas
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
