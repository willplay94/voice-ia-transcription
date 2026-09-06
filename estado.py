"""
Estado compartido del indicador de voz.

Hay dos fuentes de estado y esta capa las une en una sola:

  - VoxType escribe su propio estado en $XDG_RUNTIME_DIR/voxtype/state.
    Es texto plano ("idle", "recording", ...) y lo mantiene el demonio, así
    que no hay que tocar nada de VoxType para saber si está grabando.

  - El agente y el corrector no publican nada, así que sus estados los
    escribimos nosotros. Ese archivo es JSON porque además del estado lleva
    el texto que se está manejando, que es lo que muestra la tarjeta.

La UI solo lee `estado_actual()` y no sabe de dónde sale cada cosa. Así, si
mañana cambia el motor de voz o el agente, la UI no se entera.
"""

import json
import os
import re
import time
from pathlib import Path

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

ARCHIVO_VOXTYPE = RUNTIME / "voxtype" / "state"
DIR_PROPIO = RUNTIME / "indicador-voz"
ARCHIVO_AGENTE = DIR_PROPIO / "agente.json"

# Estados que entiende la UI, de más a menos prioritario.
GRABANDO = "grabando"
RECIBIDO = "recibido"
CORRIGIENDO = "corrigiendo"
TRANSCRIBIENDO = "transcribiendo"
PEGADO = "pegado"
HABLANDO = "hablando"
PENSANDO = "pensando"
INACTIVO = "inactivo"

# El orden importa. Dos excepciones que no son evidentes:
#
#  - GRABANDO manda sobre todo: es el único que confirma algo que estás
#    haciendo tú en ese instante, y perderlo de vista inutiliza el indicador.
#  - RECIBIDO, CORRIGIENDO y PEGADO van por delante de TRANSCRIBIENDO porque
#    VoxType mantiene su estado en "transcribing" mientras ejecuta el
#    post_process y el post_output_command, que es justo cuando esas tres
#    fases ocurren. Sin esta excepción no se verían nunca.
PRIORIDAD = [GRABANDO, RECIBIDO, CORRIGIENDO, PEGADO,
             TRANSCRIBIENDO, HABLANDO, PENSANDO, INACTIVO]

# Estados propios: los que escriben los hooks, el corrector y el TTS.
ESTADOS_PROPIOS = {RECIBIDO, CORRIGIENDO, PEGADO, PENSANDO, HABLANDO, INACTIVO}

# Estados finales: nadie los apaga después porque el script que los escribió
# ya terminó. Se desvanecen solos pasado este tiempo.
# PEGADO dura 10s (no 5s) porque la tarjeta en ese estado muestra botones
# interactivos (Copiar y X): hace falta tiempo para leer el texto, decidir y
# acertar el botón. El hover de la UI pausa el auto-cierre como complemento.
# HABLANDO dura 15s: si `hablar` muere (OOM, kill, crash de paplay), nadie
# apaga HABLANDO y la tarjeta queda clavada. El TTL es generoso porque un
# resumen puede sonar ~1 min, pero `hablar` refresca el ts cada 5s (heartbeat):
# proceso vivo → no caduca; proceso muerto → caduca solo.
ESTADOS_EFIMEROS = {PEGADO: 10.0, RECIBIDO: 12.0, HABLANDO: 15.0}

_MAPA_VOXTYPE = {
    "idle": INACTIVO,
    "recording": GRABANDO,
    "transcribing": TRANSCRIBIENDO,
    "processing": TRANSCRIBIENDO,
}

# El origen es un slug abierto, no un enum cerrado: mañana puede haber Cursor
# CLI, Codex u otro agente, y cerrar "claude|opencode" en una lista sale barato
# ahora y caro después (plan v3 §10). Se valida la FORMA, nunca la pertenencia:
# lo que no encaje se ignora (sin romper nada), no se rechaza con error.
ORIGEN_VALIDO = re.compile(r"^[a-z0-9-]{1,20}$")


ARCHIVO_TRAZA = DIR_PROPIO / "traza.log"


def traza(mensaje: str) -> None:
    """Deja constancia con marca de tiempo. Para depurar la cadena de hooks.

    VoxType ejecuta los hooks con la salida capturada, así que un `print` no
    se ve en ninguna parte; hace falta un archivo.
    """
    try:
        DIR_PROPIO.mkdir(parents=True, exist_ok=True)
        # Vive en tmpfs (se borra al reiniciar), pero aun así conviene que no
        # crezca sin freno si algo se pone a dictar en bucle.
        if ARCHIVO_TRAZA.exists() and ARCHIVO_TRAZA.stat().st_size > 200_000:
            ARCHIVO_TRAZA.unlink()
        marca = time.strftime("%H:%M:%S") + f".{int(time.time() % 1 * 1000):03d}"
        with ARCHIVO_TRAZA.open("a", encoding="utf-8") as f:
            f.write(f"{marca}  {mensaje}\n")
    except OSError:
        pass


def asegurar_directorio() -> None:
    """Crea el directorio propio si falta, para poder vigilarlo desde el arranque."""
    DIR_PROPIO.mkdir(parents=True, exist_ok=True)


def leer_voxtype() -> str:
    """Nunca lanza: el indicador tiene que sobrevivir a que VoxType esté parado."""
    try:
        crudo = ARCHIVO_VOXTYPE.read_text(encoding="utf-8", errors="replace").strip().lower()
    except (OSError, ValueError):
        return INACTIVO
    return _MAPA_VOXTYPE.get(crudo, INACTIVO)


def leer_propio() -> dict:
    """El estado propio con su carga: {estado, texto, original, ts, origen}."""
    vacio = {"estado": INACTIVO, "texto": "", "original": "", "ts": 0.0, "origen": ""}
    try:
        datos = json.loads(ARCHIVO_AGENTE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return vacio
    if not isinstance(datos, dict) or datos.get("estado") not in ESTADOS_PROPIOS:
        return vacio

    # Los estados efímeros caducan solos: quien los escribió ya no existe
    # para apagarlos.
    caducidad = ESTADOS_EFIMEROS.get(datos["estado"])
    if caducidad is not None and time.time() - float(datos.get("ts") or 0) > caducidad:
        return vacio

    return {
        "estado": datos["estado"],
        "texto": str(datos.get("texto") or ""),
        "original": str(datos.get("original") or ""),
        "ts": float(datos.get("ts") or 0),
        # .get con respaldo y no por índice: un agente.json escrito por una
        # versión anterior no lleva "origen" y no debe romper la lectura
        # (plan v3 §8). Se transporta intacto: `actualizar_estado()` (que
        # llaman `pegar` y el latido de `reproducir()`) lo conserva solo si
        # la lectura lo devuelve.
        "origen": str(datos.get("origen") or ""),
    }


def escribir_propio(estado: str, texto: str = "", original: str = "",
                    origen: str = "") -> None:
    """Publica estado y texto.

    Escribe a un temporal y renombra, porque `rename` es atómico: la UI lee
    esto constantemente y así nunca pilla una escritura a medias.

    `origen` es opcional y por defecto vacío: los callers que no lo pasan
    (corregir, pegar, voz-estado sin 2º argumento) escriben exactamente lo
    mismo que antes — regresión bit a bit.
    """
    if estado not in ESTADOS_PROPIOS:
        raise ValueError(f"estado desconocido: {estado!r} (usa: {sorted(ESTADOS_PROPIOS)})")

    # Validación de forma, no de pertenencia (plan v3 §10): un slug raro
    # ("Cursor CLI!") no se rechaza con excepción — se normaliza a vacío y
    # queda en la traza. Un ValueError aquí tumbaría al publicador, y un
    # origen inválido nunca vale una tarjeta rota.
    origen = str(origen or "")
    if origen and not ORIGEN_VALIDO.match(origen):
        traza(f"estado: origen inválido {origen!r}; se ignora")
        origen = ""

    asegurar_directorio()
    datos = {"estado": estado, "texto": texto, "original": original,
             "ts": time.time(), "origen": origen}
    temporal = ARCHIVO_AGENTE.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    temporal.replace(ARCHIVO_AGENTE)


def actualizar_estado(estado: str) -> None:
    """Cambia solo el estado y conserva el texto que ya hubiera.

    Lo usa el paso de pegado: el texto y su original los publicó el corrector
    un momento antes, y aquí solo se marca que ya está pegado.

    Este es el sitio exacto donde `origen` se perdería si nadie lo cuida
    (plan v3 §8): lo llaman `pegar` y el latido de `reproducir()` cada 5 s,
    y ambos llaman sin kwarg. La conservación sale de `leer_propio()`, que
    ahora transporta el campo.
    """
    previo = leer_propio()
    escribir_propio(estado, previo["texto"], previo["original"],
                    previo.get("origen", ""))


def estado_actual() -> dict:
    """Lo que toca mostrar: {estado, texto, original, origen}, resuelto por prioridad."""
    propio = leer_propio()
    candidatos = {leer_voxtype(), propio["estado"]}
    for estado in PRIORIDAD:
        if estado in candidatos:
            if estado == propio["estado"]:
                return propio
            # Rama de VoxType: sin carga propia. `origen` vacío a propósito —
            # grabando/transcribiendo no tienen agente detrás.
            return {"estado": estado, "texto": "", "original": "", "ts": 0.0,
                    "origen": ""}
    return {"estado": INACTIVO, "texto": "", "original": "", "ts": 0.0,
            "origen": ""}
