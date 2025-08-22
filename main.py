# main.py — RT calls skeleton (Paso 2)
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Start
from dotenv import load_dotenv
import os

load_dotenv()
app = Flask(__name__)

@app.route("/", methods=["GET"])
def root():
    return "✅ RT core activo (llamadas-multi-bots).", 200

@app.route("/voice", methods=["POST", "GET"])
def voice():
    """
    Twilio Voice Webhook:
    - Responde con TwiML y abre un Media Stream hacia nuestro WebSocket (/media-stream).
    - En el siguiente paso implementaremos el WS y el puente a OpenAI Realtime.
    """
    # Construimos URL pública del WS (en Render será wss://.../media-stream)
    base_ws = os.environ.get("PUBLIC_WS_URL")
    if not base_ws:
        base = request.url_root.replace("https", "wss").replace("http", "ws")
        base_ws = (base.rstrip("/") + "/media-stream")

    vr = VoiceResponse()
    vr.say("Conectando con el asistente en tiempo real.", language="es-ES", voice="Polly.Mia")

    start = Start()
    start.stream(url=base_ws, track="both_tracks")
    vr.append(start)
    return Response(str(vr), mimetype="text/xml")

@app.route("/media-stream", methods=["GET"])
def media_stream_placeholder():
    # En el próximo paso implementaremos el WebSocket.
    return Response("Use WebSocket (procederemos en el siguiente paso).", status=426)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
