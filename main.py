# main.py
# -*- coding: utf-8 -*-

import os
import json
import base64
import asyncio
import websockets
from typing import Dict, Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

# =========================
# CONFIG BÁSICA
# =========================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
VOICE = os.getenv("OPENAI_VOICE", "alloy")

APP_URL = os.getenv("APP_URL")  # p.ej. https://llamadas-multi-bots.onrender.com
if not APP_URL:
    # Render la setea en RENDER_EXTERNAL_URL; si no, usa dummy que Twilio no usará.
    APP_URL = os.getenv("RENDER_EXTERNAL_URL", "https://example.invalid")

# Bots simples en memoria (puedes mover a /bots/*.json si quieres)
BOTS: Dict[str, Dict[str, Any]] = {
    "inhoustontx": {
        "name": "Sara",
        "business_name": "In Houston Texas",
        "instructions": (
            "Eres Sara, del departamento de ventas de 'In Houston, Texas'. "
            "Preséntate cordial, profesional, cercana y persuasiva, con lenguaje "
            "humano y natural (español latino). Frases cortas, sin repetirte. "
            "Guía siempre hacia una cita con el Sr. Sundin Galue. Usa un tono "
            "cálido y convincente. Si el usuario dice hola, saluda: "
            "‘Hola, Soy Sara, representante de ventas de In Houston Texas. "
            "¿Con quién tengo el gusto?’"
        ),
    }
}

app = FastAPI()

# =========================
# ENDPOINTS
# =========================

@app.get("/")
async def root():
    return {"ok": True, "service": "llamadas-multi-bots", "model": MODEL, "voice": VOICE}


@app.post("/voice")
async def voice(request: Request):
    """
    Twilio (Voice Webhook): responde TwiML para abrir el Media Stream
    hacia nuestro WebSocket /media-stream.
    Usa both_tracks para audio ida/vuelta (requiere habilitación en Twilio).
    """
    params = dict(request.query_params)
    bot = params.get("bot", "inhoustontx")
    stream_url = f"{APP_URL.replace('http://', 'https://')}/media-stream?bot={bot}"

    # Si aún no te habilitan both_tracks, cambia a inbound_track para probar:
    track = "both_tracks"  # o "inbound_track"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="es-ES" voice="Polly.Mia">Conectando con el asistente en tiempo real.</Say>
  <Connect>
    <Stream
      url="{stream_url}"
      track="{track}"
      statusCallbackEvent="start stop mark clear"
      statusCallbackMethod="POST"
      statusCallback="{APP_URL}/twilio/stream-status?bot={bot}"/>
  </Connect>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/stream-status")
async def stream_status(request: Request):
    """
    Solo para depurar: Twilio nos manda eventos del stream.
    """
    body = await request.body()
    try:
        text = body.decode("utf-8", errors="ignore")
    except Exception:
        text = str(body)
    print(f"[TWILIO-CB] {text}")
    return PlainTextResponse("OK")


# =========================
# BRIDGE: TWILIO <-> OPENAI
# =========================

async def openai_connect(bot_key: str):
    """
    Abre WebSocket con OpenAI Realtime.
    Devuelve la conexión ws lista para usar.
    """
    url = f"wss://api.openai.com/v1/realtime?model={MODEL}"
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]
    ws = await websockets.connect(url, extra_headers=headers, max_size=16 * 1024 * 1024)
    # Config de sesión (modalidades, voz y formatos de audio)
    bot = BOTS.get(bot_key, BOTS["inhoustontx"])
    session_update = {
        "type": "session.update",
        "session": {
            "voice": VOICE,
            "modalities": ["text", "audio"],
            # Enviar y recibir μ-law 8k (compatible con Twilio)
            "input_audio_format": {
                "type": "g711_ulaw",
                "sample_rate": 8000
            },
            "audio": {
                "voice": VOICE
            },
            "audio_out": {
                "format": "mulaw",
                "sample_rate": 8000
            },
            # Instrucciones (personalidad)
            "instructions": bot["instructions"]
        }
    }
    await ws.send(json.dumps(session_update))
    return ws


async def pump_twilio_to_openai(twilio_ws: WebSocket, openai_ws, stream_sid: str):
    """
    Lee mensajes de Twilio y envía audio (ulaw base64) a OpenAI.
    Cuando Twilio manda 'mark' (fin de turno), pedimos respuesta a OpenAI.
    """
    while True:
        msg = await twilio_ws.receive_text()
        data = json.loads(msg)

        event = data.get("event")
        if event == "start":
            print(f"[WS] ▶️ start streamSid={stream_sid}")
        elif event == "media":
            # Audio μ-law base64 desde Twilio
            ulaw_b64 = data["media"]["payload"]
            await openai_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": ulaw_b64
            }))
        elif event == "mark":
            # Twilio marcó fin de utterance (si usas marks)
            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {"modalities": ["text", "audio"]}
            }))
        elif event == "stop":
            print(f"[WS] ⏹ stop streamSid={stream_sid}")
            # Cierra ordenadamente
            await openai_ws.close()
            await twilio_ws.close()
            break
        else:
            # Otros eventos (clear, etc.)
            pass


async def pump_openai_to_twilio(openai_ws, twilio_ws: WebSocket, stream_sid: str):
    """
    Lee eventos de OpenAI y reenvía audio Δ a Twilio (μ-law base64).
    """
    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                # Algunos frames podrían venir como binarios (rarísimo hoy), ignoramos
                continue

            t = evt.get("type")
            if t == "response.audio.delta":
                mulaw_b64 = evt["delta"]
                await twilio_ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": mulaw_b64}
                }))
            elif t == "response.audio.done":
                # Fin del audio de esta respuesta
                pass
            elif t == "response.done":
                pass
            elif t == "error":
                print(f"[AI] ❌ error payload: {evt}")
            elif t == "session.updated":
                pass
            elif t == "session.created":
                pass
            else:
                # Para depurar otros tipos:
                # print(f"[AI] ← {t}: {evt}")
                pass

    except websockets.ConnectionClosed:
        # OpenAI cerró
        try:
            await twilio_ws.close()
        except Exception:
            pass


@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    """
    WebSocket que recibe Twilio Media Streams (μ-law 8k, base64)
    y puentea a OpenAI Realtime.
    """
    await twilio_ws.accept()

    # El primer mensaje de Twilio suele ser "start", lo leemos para obtener el streamSid
    start_msg_raw = await twilio_ws.receive_text()
    start_msg = json.loads(start_msg_raw)
    if start_msg.get("event") != "start":
        # Si no es "start", igual intenta extraer streamSid:
        stream_sid = start_msg.get("streamSid", "unknown")
    else:
        stream_sid = start_msg["start"]["streamSid"]

    # Param bot para personalidad
    bot = (twilio_ws.query_params.get("bot") or "inhoustontx").strip().lower()

    print(f"[WS-HANDSHAKE] path=/media-stream streamSid={stream_sid} bot={bot}")

    # Conexión a OpenAI
    if not OPENAI_API_KEY:
        await twilio_ws.send_text(json.dumps({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": "no_openai_key"}
        }))
        await twilio_ws.close()
        return

    try:
        openai_ws = await openai_connect(bot)
    except Exception as e:
        print(f"[BOT] error conectando a OpenAI: {e}")
        await twilio_ws.close()
        return

    # Lanzamos el bombeo en paralelo
    task1 = asyncio.create_task(pump_twilio_to_openai(twilio_ws, openai_ws, stream_sid))
    task2 = asyncio.create_task(pump_openai_to_twilio(openai_ws, twilio_ws, stream_sid))

    try:
        await asyncio.gather(task1, task2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] error: {e}")
    finally:
        try:
            await openai_ws.close()
        except Exception:
            pass
        try:
            await twilio_ws.close()
        except Exception:
            pass
