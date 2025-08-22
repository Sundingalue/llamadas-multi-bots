# main.py — RT calls WS bridge (Paso 5)
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Start
from dotenv import load_dotenv
from simple_websocket import Server, ConnectionClosed
import os, json, base64, time

load_dotenv()
app = Flask(__name__)

@app.route("/", methods=["GET"])
def root():
    return "✅ RT core activo (llamadas-multi-bots).", 200

@app.route("/voice", methods=["POST", "GET"])
def voice():
    """
    Twilio Voice Webhook:
    - Devuelve TwiML que abre un Media Stream hacia nuestro WebSocket (/media-stream).
    - En el próximo paso conectaremos este stream a OpenAI Realtime (voz Nova).
    """
    # Construir URL pública del WS: usa var de entorno PUBLIC_WS_URL si existe;
    # si no, dedúcela a partir del request.
    base_ws = os.environ.get("PUBLIC_WS_URL")
    if not base_ws:
        base = request.url_root.replace("https", "wss").replace("http", "ws")
        base_ws = (base.rstrip("/") + "/media-stream")

    vr = VoiceResponse()
    # Mensaje breve, solo como feedback inicial (Twilio <Say>).
    vr.say("Conectando con el asistente en tiempo real.", language="es-ES", voice="Polly.Mia")

    start = Start()
    # both_tracks: envía (caller->server) y recibe (server->Twilio). Por ahora solo recibimos.
    start.stream(url=base_ws, track="both_tracks")
    vr.append(start)
    return Response(str(vr), mimetype="text/xml")

@app.route("/media-stream", methods=["GET"])
def media_stream():
    """
    Endpoint WebSocket para Twilio Media Streams.
    - Recibe mensajes JSON con event: start | media | stop.
    - Por ahora solo los registramos (no respondemos audio aún).
    - Próximo paso: puentear a OpenAI Realtime y devolver audio (voz Nova).
    """
    # Intentar negociar WebSocket; si no es WS, responder 426.
    try:
        ws = Server(request.environ)
    except Exception:
        return Response("Upgrade Required: use WebSocket here.", status=426)

    call_sid = None
    packets = 0
    started_at = time.time()
    print("[WS] 🚀 Media stream conectado.")

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break

            # Twilio envía JSON por mensaje
            try:
                data = json.loads(msg)
            except Exception:
                print("[WS] ⚠️ Mensaje no-JSON (ignorado).")
                continue

            etype = data.get("event")

            if etype == "start":
                call_sid = data.get("start", {}).get("callSid")
                stream_sid = data.get("start", {}).get("streamSid")
                sample_rate = data.get("start", {}).get("sampleRate")
                print(f"[WS] ▶️ start callSid={call_sid} streamSid={stream_sid} sr={sample_rate}")

            elif etype == "media":
                packets += 1
                # payload = data["media"]["payload"]  # base64 de audio PCM μ-law/PCM16
                # Por ahora solo contamos paquetes para verificar flujo.
                if packets % 50 == 0:
                    print(f"[WS] 📦 paquetes recibidos: {packets}")

            elif etype == "stop":
                dur = time.time() - started_at
                print(f"[WS] ⏹ stop callSid={call_sid} duración={dur:.1f}s total_paquetes={packets}")
                break

            else:
                # Otros eventos raros: marks, clear, etc.
                pass

    except ConnectionClosed:
        print("[WS] 🔌 Conexión WS cerrada por el cliente.")
    except Exception as e:
        print(f"[WS] ❌ Error inesperado: {e}")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        print("[WS] ✅ Media stream finalizado.")
    # No devolvemos HTTP porque esto es un WS; retornamos cadena vacía para cerrar correctamente.
    return ""

if __name__ == "__main__":
    # Útil para correr local si lo necesitas (no obligatorio en Render)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
