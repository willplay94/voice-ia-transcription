"""
Síntesis de voz con ElevenLabs para el indicador.

Convierte texto en audio MP3 y lo devuelve por trozos, para que `hablar` lo
acumule en un tempfile y lo reproduzca con `paplay`. Igual que `corrector.py`,
habla REST con `urllib` (stdlib): no hay SDK de ElevenLabs instalable sin
`pip`, y no lo queremos.

La regla que gobierna el diseño es la misma que en el corrector: **nunca
lanzar hacia afuera**. Si la red falla, si la clave es mala, si el HTTP
devuelve error, se registra en la traza y se devuelve un generador vacío.
Quien llama (`hablar`) decide qué hacer, normalmente caer a `spd-say`.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# --- Configuración ------------------------------------------------------

CONFIG = Path.home() / ".config" / "indicador-voz" / "hablar.json"

# Claves en español, igual que el resto del proyecto. Se mapean a los campos
# que espera la API de ElevenLabs dentro de `sintetizar`.
POR_DEFECTO = {
    # Opt-in y no opt-out (hallazgo BAJO de la revisión del TTS): README y
    # CLAUDE.md documentan "activo: false por defecto", pero aquí había un
    # True. No es cosmético: lo que se lee en voz alta son respuestas del
    # agente que pueden arrastrar contenido de archivos o credenciales, y
    # salen del equipo (ElevenLabs + LLM resumidor). Hasta que el usuario no
    # active el TTS en su hablar.json, nada se envía. En la máquina de uso
    # diario el hablar.json ya lo pone en true, así que este cambio solo
    # afecta a instalaciones nuevas.
    "activo": False,
    "api_key": "",
    # Voz por defecto: Carlos Aguilar (voz de la librería). Ojo: las voces de
    # la librería requieren plan de pago; en Free devuelven HTTP 402 y el TTS
    # cae a spd-say. Con plan de pago funciona sin tocar nada. La voz
    # predefinida de Free es Adam (pNInz6obpgDQGcFmaJgB), que es la que usa
    # el hablar.json de la máquina de uso diario.
    "voz_id": "8MeTTgXVwMEhRVfblXOj",
    "modelo": "eleven_flash_v2_5",
    # MP3 a 44.1 kHz / 128 kbps. El plan Free de ElevenLabs NO devuelve PCM
    # (pcm_44100 da HTTP 403: "only available on the Pro tier"), así que se
    # pide MP3 y `paplay` (sin --raw) lo decodifica solo.
    "output_format": "mp3_44100_128",
    # Parámetros de voz. `estabilidad` y `similitud` son los dos que más
    # cambian el resultado; el resto se deja en valores neutros.
    "estabilidad": 0.5,
    "similitud": 0.75,
    "estilo": 0.0,
    "velocidad": 1.0,
    # Timeout por operación de socket, NUNCA total: un resumen largo puede
    # tardar un minuto en reproducirse, y un timeout total lo cortaría a
    # mitad. urllib aplica este valor a cada `read()`, no a toda la llamada.
    "timeout_s": 15,
    # Por encima de esto no se sintetiza: es un texto demasiado largo para
    # leerlo en voz alta y la espera molesta más de lo que aporta.
    "max_caracteres": 1000,
    # --- Resumen con LLM (Fase 2) ---
    # Si es False, todo el texto va directo al TTS sin resumir. Útil para
    # depurar o si el usuario prefiere oír la respuesta completa.
    "resumen_activo": True,
    # Tope de caracteres que se mandan al resumidor (cabeza + cola).
    "max_caracteres_entrada": 4000,
    # Tope del resumen resultante, lo que el TTS va a leer en voz alta.
    "max_caracteres_resumen": 350,
}

# El endpoint /stream devuelve el audio por trozos a medida que se genera.
# Con MP3 los trozos son frames concatenables: `hablar` los acumula en un
# tempfile y reproduce el archivo completo con `paplay`.
URL_BASE = "https://api.elevenlabs.io/v1/text-to-speech/{voz_id}/stream"


def cargar_config() -> dict:
    """Config fusionada sobre los valores por defecto, como `corrector.py`."""
    config = dict(POR_DEFECTO)
    try:
        config.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        # No se puede usar `print`: VoxType captura stdout. La traza es el
        # único canal visible.
        import estado as st
        st.traza(f"tts: config ilegible ({e}); se usan los valores por defecto")
    return config


def leer_clave(config: dict) -> str | None:
    """La clave de ElevenLabs: de la config, o del entorno."""
    clave = config.get("api_key") or os.environ.get("ELEVENLABS_API_KEY")
    if clave:
        return clave.strip()
    return None


def sintetizar(texto: str, config: dict | None = None):
    """Generador que devuelve los trozos de audio MP3 a medida que llegan.

    Nunca lanza: ante cualquier error registra en la traza y termina sin
    devolver nada. El llamador detecta que no llegó audio y cae a `spd-say`.
    """
    config = config or cargar_config()

    # Sin audio y con motivo en la traza (hallazgo BAJO): antes estos cuatro
    # retornos eran silenciosos y el único síntoma era "cayó a spd-say sin
    # saber por qué". Solo se registra el motivo y tamaños: NUNCA el texto ni
    # la clave, que no deben acabar en la traza.
    import estado as st

    if not config.get("activo", False):
        st.traza("tts: no sintetiza: TTS desactivado en config")
        return

    limpio = texto.strip()
    if not limpio:
        st.traza("tts: no sintetiza: texto vacío")
        return
    max_car = config.get("max_caracteres", POR_DEFECTO["max_caracteres"])
    if len(limpio) > max_car:
        st.traza(f"tts: no sintetiza: texto de {len(limpio)} car. supera el tope de {max_car}")
        return

    voz_id = config.get("voz_id")
    if not voz_id:
        st.traza("tts: no sintetiza: sin voz_id en config")
        return

    clave = leer_clave(config)
    if not clave:
        st.traza("tts: no sintetiza: sin clave de ElevenLabs (config ni ELEVENLABS_API_KEY)")
        return

    cuerpo = json.dumps({
        "text": limpio,
        "model_id": config.get("modelo", POR_DEFECTO["modelo"]),
        "voice_settings": {
            "stability": config.get("estabilidad", 0.5),
            "similarity_boost": config.get("similitud", 0.75),
            "style": config.get("estilo", 0.0),
            "use_speaker_boost": True,
            "speed": config.get("velocidad", 1.0),
        },
    }).encode("utf-8")

    url = URL_BASE.format(voz_id=voz_id) + f"?output_format={config.get('output_format', POR_DEFECTO['output_format'])}"
    peticion = urllib.request.Request(url, data=cuerpo, headers={
        "xi-api-key": clave,
        "Content-Type": "application/json",
    })

    # La llamada real va dentro del generador: así el error se captura aquí
    # y no se propaga al llamador, que está a medio camino de reproducir.
    try:
        with urllib.request.urlopen(peticion, timeout=config.get("timeout_s", POR_DEFECTO["timeout_s"])) as r:
            while True:
                trozo = r.read(8192)
                if not trozo:
                    break
                yield trozo
    except urllib.error.HTTPError as e:
        # El código HTTP es lo único que se registra: NUNCA la clave ni el
        # texto, que podrían acabar en la traza y de ahí fuera del equipo.
        # Los dos códigos que se ven en plan Free tienen motivo conocido y
        # conviene distinguirlos en la traza para no confundirlos con un fallo
        # de red: 402 = voz de librería (requiere plan de pago), 403 = formato
        # PCM (requiere Pro). Ambos caen a spd-say igualmente.
        import estado as st
        motivos = {
            402: "voz de librería (requiere plan de pago)",
            403: "formato no disponible en el plan actual",
        }
        detalle = motivos.get(e.code, "")
        st.traza(f"tts: HTTP {e.code}" + (f" ({detalle})" if detalle else ""))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        import estado as st
        st.traza(f"tts: red: {type(e).__name__}")
