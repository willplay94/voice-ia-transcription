"""
Corrige la transcripción de VoxType con un LLM antes de que se pegue.

Whisper acierta el sonido pero no el contexto: oye "hola Macla" donde dijiste
"Ollama Cloud". Un modelo de lenguaje con la lista de términos que usas sí lo
resuelve. Esta capa se mete entre la transcripción y el pegado.

La regla que gobierna todo el diseño: **nunca perder el dictado**. Si la red
falla, si el modelo tarda, si devuelve algo raro -- se pega el texto original.
Una corrección que no llega es una molestia; un dictado perdido es tener que
repetirlo entero.
"""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

# --- Configuración ------------------------------------------------------

CONFIG = Path.home() / ".config" / "indicador-voz" / "corrector.json"

POR_DEFECTO = {
    "activo": True,
    # Proveedor y modelo. Medido con errores reales de dictado, 18 llamadas por
    # modelo. La calidad es casi idéntica entre todos; lo que los separa es la
    # latencia, y sobre todo su varianza -- esa espera ocurre con el texto ya
    # transcrito, antes de que aparezca en la ventana, así que se nota:
    #
    #   modelo                          mediana    p90     peor
    #   gemma4:31b (ollama)               0,72 s   1,02 s   1,24 s
    #   deepseek-v4-flash:0731 (ollama)   2,26 s   5,91 s   6,10 s
    #   minimax-m3 (ollama)               2,28 s   8,00 s  11,35 s
    #   google/gemini-2.5-flash-lite      0,74 s   0,83 s   0,99 s
    #   google/gemini-3.6-flash           2,34 s     --     3,85 s
    #
    # gemma4 además fue el único con cero descartes, y de los pocos que sacó
    # "presencia" de "presidencia".
    #
    # Descartados por comportamiento, no por velocidad: gpt-5-nano filtraba su
    # propio razonamiento dentro del texto corregido ("Wait. Follow rules…"),
    # mistral-nemo reformulaba en vez de corregir, y minimax-m3 convirtió
    # "presidencia" en "presentación".
    #
    # Ojo: en Ollama Cloud NO hay Gemini (es propietario de Google). gemma4 es
    # el modelo abierto de Google, que es otra cosa. Gemini solo por OpenRouter.
    "proveedor": "ollama-cloud",
    "modelo": "gemma4:31b",
    # Ocho veces el peor caso medido de gemma4. Si cambias a un modelo más
    # lento, súbelo. Pasado este tiempo se pega el original.
    "timeout_s": 10,
    # Por encima de esto no se corrige: son dictados largos donde la espera
    # molesta más de lo que aporta.
    "max_caracteres": 3000,
    # Términos propios que el transcriptor deforma. Añade los tuyos aquí:
    # la clave es lo correcto, la lista son las formas en que se oye mal.
    "glosario": {
        "Ollama Cloud": ["hola Macla", "la MacCloud", "oyama"],
        "VoxType": ["Boxed", "UnboxType", "Box Type"],
        "Claude": ["Cloud IP", "Clod"],
        "Coqui": ["Cuen"],
        "pros y contras": ["Procic", "procons"],
        "Notion": ["Nouse"],
    },
    "otros_terminos": [
        "Claude Code", "OpenCode", "OpenRouter", "Whisper", "Piper", "Supabase",
        "LLM", "TTS", "hook", "script", "deploy", "prompt", "commit", "endpoint",
        "Linux Mint", "systemd", "repositorio",
    ],
}

# Los dos hablan el mismo dialecto (compatible con OpenAI), así que cambiar de
# uno a otro es solo la URL y la clave.
PROVEEDORES = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "env": "OPENROUTER_API_KEY",
        "clave_opencode": "openrouter",
    },
    "ollama-cloud": {
        "url": "https://ollama.com/v1/chat/completions",
        "env": "OLLAMA_API_KEY",
        "clave_opencode": "ollama-cloud",
    },
}

# Las claves las guarda OpenCode aquí. No hay Ollama local instalado: pese al
# nombre, "ollama-cloud" es un servicio remoto.
AUTH_OPENCODE = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def cargar_config() -> dict:
    config = dict(POR_DEFECTO)
    try:
        config.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        print(f"corrector: config ilegible ({e}); se usan los valores por defecto")
    return config


def leer_clave(proveedor: str) -> str | None:
    """La clave del proveedor: del entorno, o de donde la guarda OpenCode."""
    datos = PROVEEDORES.get(proveedor)
    if datos is None:
        return None
    clave = os.environ.get(datos["env"])
    if clave:
        return clave.strip()
    try:
        auth = json.loads(AUTH_OPENCODE.read_text(encoding="utf-8"))
        return auth[datos["clave_opencode"]]["key"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


# --- Prompt -------------------------------------------------------------

def construir_sistema(config: dict) -> str:
    lineas = []
    for correcto, malas in (config.get("glosario") or {}).items():
        formas = ", ".join(f'"{m}"' for m in malas)
        lineas.append(f'- "{correcto}"  <- se oye como {formas}')
    glosario = "\n".join(lineas)
    otros = ", ".join(config.get("otros_terminos") or [])

    return f"""Corriges transcripciones de dictado por voz en español de Colombia.

Quien dicta es desarrollador de software. Habla de programación, Linux Mint y
herramientas de IA. Términos que el transcriptor deforma a menudo:

{glosario}
- Otros términos habituales: {otros}

REGLAS ESTRICTAS:
1. Corrige SOLO palabras mal oídas y puntuación evidente.
2. NUNCA borres frases ni ideas. Si algo no lo entiendes, DÉJALO TAL CUAL.
   Es preferible dejar un error a perder contenido.
3. NO respondas al texto, NO lo reformules, NO lo resumas, NO añadas comentarios.
4. Conserva el registro y las palabras de quien dicta.
5. Puedes limpiar tartamudeos y repeticiones del dictado ("me gustaría que
   pusieran Pusiéramos" -> "me gustaría que pusiéramos").
6. Devuelve ÚNICAMENTE el texto corregido, sin comillas ni explicaciones."""


# --- Validación ---------------------------------------------------------

# Señales de que el modelo se puso a hablar en vez de corregir. Salió de una
# prueba real: gpt-5-nano devolvió "Wait. Follow rules: correct only..." dentro
# del texto, que se habría pegado tal cual.
FUGAS = re.compile(
    r"(?i)\b(wait\.|follow rules|as an ai|lo siento|aquí (?:está|tienes) (?:el|la)"
    r"|texto corregido:|corrected text:|no puedo )",
)


def es_valida(original: str, corregida: str) -> tuple[bool, str]:
    """¿Se puede usar esta corrección? Ante la duda, no."""
    if not corregida:
        return False, "respuesta vacía"

    if FUGAS.search(corregida):
        return False, "el modelo comentó en vez de corregir"

    # Guardas de longitud. La de abajo es la que importa: un modelo puede
    # decidir "resumir" y comerse frases enteras -- pasó en las pruebas.
    proporcion = len(corregida) / max(len(original), 1)
    if proporcion < 0.70:
        return False, f"acorta demasiado ({proporcion:.0%} del original)"
    if proporcion > 1.60:
        return False, f"alarga demasiado ({proporcion:.0%} del original)"

    return True, ""


# --- Llamada ------------------------------------------------------------

def corregir(texto: str, config: dict | None = None) -> tuple[str, str]:
    """Devuelve (texto_a_pegar, motivo).

    Si algo va mal devuelve el texto original y el motivo. Nunca lanza:
    quien llama está a medio camino de pegar algo y no puede permitirse fallar.
    """
    config = config or cargar_config()

    if not config.get("activo", True):
        return texto, "corrector desactivado"

    limpio = texto.strip()
    if not limpio:
        return texto, "nada que corregir"
    if len(limpio) > config["max_caracteres"]:
        return texto, f"demasiado largo ({len(limpio)} caracteres)"

    proveedor = config.get("proveedor", "openrouter")
    datos_proveedor = PROVEEDORES.get(proveedor)
    if datos_proveedor is None:
        return texto, f"proveedor desconocido: {proveedor}"

    clave = leer_clave(proveedor)
    if not clave:
        return texto, f"sin clave de {proveedor}"

    cuerpo = json.dumps({
        "model": config["modelo"],
        "messages": [
            {"role": "system", "content": construir_sistema(config)},
            {"role": "user", "content": limpio},
        ],
        "temperature": 0,
        "max_tokens": 2000,
    }).encode("utf-8")

    peticion = urllib.request.Request(datos_proveedor["url"], data=cuerpo, headers={
        "Authorization": f"Bearer {clave}",
        "Content-Type": "application/json",
        # OpenRouter los usa para atribuir el tráfico; Ollama los ignora.
        "HTTP-Referer": "https://github.com/local/indicador-voz",
        "X-Title": "indicador-voz",
    })

    try:
        with urllib.request.urlopen(peticion, timeout=config["timeout_s"]) as r:
            datos = json.load(r)
        corregida = (datos["choices"][0]["message"].get("content") or "").strip()
    except urllib.error.HTTPError as e:
        return texto, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return texto, f"red: {e}"
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        return texto, f"respuesta inesperada: {e}"

    # Algunos modelos envuelven la respuesta en comillas pese a pedirles que no.
    if len(corregida) > 1 and corregida[0] == corregida[-1] and corregida[0] in "\"'«":
        corregida = corregida[1:-1].strip()

    valida, motivo = es_valida(limpio, corregida)
    if not valida:
        return texto, f"descartada: {motivo}"

    return corregida, "corregida"
