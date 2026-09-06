#!/usr/bin/env python3
"""
Tarjeta flotante que muestra quién tiene la palabra y qué texto hay en juego.

Cuando no pasa nada se esconde del todo -- un indicador siempre visible se
vuelve invisible a los cinco minutos, y además estorba.

Decisiones de implementación que no son obvias:

  - GTK3 vía PyGObject, que ya viene en Mint. Es deliberado: este Python no
    tiene el módulo `pip`, así que cualquier dependencia externa obligaría a
    montar un venv antes de poder ver nada en pantalla.

  - Se vigilan los *directorios* de estado, no los archivos. Un monitor sobre
    un archivo deja de funcionar en cuanto alguien lo reemplaza por rename
    (que es como se escriben los estados de forma atómica) -- es la causa
    clásica de que estos indicadores se queden congelados. El monitor de
    directorio sí recibe los cambios de sus hijos y sobrevive al reemplazo.

    Medido en este equipo: sondear cada 100 ms costaba 0,35% de CPU
    permanente; por eventos queda en 0,00%. Sobre un portátil, eso es
    batería a cambio de nada.

  - Queda un sondeo de red de seguridad cada segundo. Cubre el caso de
    arrancar antes que VoxType (su directorio aún no existe) y, sobre todo,
    caducar los estados efímeros: "Pegado" se apaga solo porque el script que
    lo escribió ya terminó, así que ningún evento de archivo va a avisarnos.

  - La ventana es click-through (región de entrada vacía). Es un indicador,
    no un control: si intercepta clics, tapa lo que haya debajo.
"""

import difflib
import math
import re
import signal
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")

import cairo
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import estado as st

# --- Apariencia ---------------------------------------------------------

# Un solo número para ajustar el tamaño de todo. Súbelo o bájalo y reinicia
# el servicio; el resto de medidas salen de aquí.
ESCALA = 1.4

# Opacidad del fondo. 1.0 es opaco; por debajo de 0.65 el texto empieza a
# competir con lo que haya detrás.
OPACIDAD_FONDO = 0.80

# Más ancha que antes (330) para que el texto dictado, ahora más pequeño,
# alcance a aparecer completo en las tres líneas sin recortarse.
ANCHO_TARJETA = round(380 * ESCALA)
ALTO_MINIMO = round(34 * ESCALA)
MARGEN_PANTALLA = 18
PADDING_H = round(14 * ESCALA)
PADDING_V = round(9 * ESCALA)
RADIO_PUNTO = 5.5 * ESCALA
SEPARACION_PUNTO_TEXTO = round(9 * ESCALA)
INTERLINEA = round(4 * ESCALA)
MAX_LINEAS = 3

# Franja de botones (Copiar y X) que aparece solo en PEGADO con texto. El
# alto de cada botón y de la franja salen de ESCALA como el resto de medidas,
# para que todo escale junto al ajustar el tamaño de la tarjeta.
ALTO_BOTON = round(20 * ESCALA)          # alto de cada botón
MARGEN_BOTON_V = round(3 * ESCALA)      # margen vertical dentro de la franja
ALTO_FRANJA_BOTONES = ALTO_BOTON + 2 * MARGEN_BOTON_V
ANCHO_BOTON_COPIAR = round(64 * ESCALA)
ANCHO_BOTON_CERRAR = round(28 * ESCALA)
SEPARACION_BOTONES = round(8 * ESCALA)  # hueco entre el texto y la franja
RADIO_BOTON = round(6 * ESCALA)

# Jerarquía visual: la etiqueta de estado (arriba) es lo primero que se lee y
# va en negrita y más grande; el texto dictado/corregido (abajo) es secundario
# y va más pequeño y tenue. Antes ambas fuentes eran casi iguales (solo
# diferían en Bold) y no había jerarquía. Al encoger FUENTE_TEXTO caben más
# palabras por línea, así que el texto alcanza a aparecer completo.
FUENTE_ESTADO = f"Sans Bold {9.5 * ESCALA:.1f}"
FUENTE_TEXTO = f"Sans {7.5 * ESCALA:.1f}"

MS_RED_SEGURIDAD = 1000
MS_ANIMACION = 33  # ~30 fps mientras algo late
MS_FUNDIDO = 16

# Cada estado: etiqueta, color RGB del punto, y si late.
#
# Late solo lo que está vivo en ese instante (grabando, hablando). Lo que es
# una espera o un resultado se queda fijo, para que el movimiento signifique
# algo en vez de ser decoración.
ESTILOS = {
    st.GRABANDO: ("Grabando", (0.94, 0.27, 0.27), True),
    st.TRANSCRIBIENDO: ("Transcribiendo…", (0.96, 0.68, 0.18), True),
    st.RECIBIDO: ("Recibido", (0.96, 0.68, 0.18), False),
    st.CORRIGIENDO: ("Corrigiendo…", (0.72, 0.52, 0.96), True),
    st.PEGADO: ("Pegado", (0.30, 0.78, 0.47), False),
    st.PENSANDO: ("Claude pensando…", (0.38, 0.60, 0.96), True),
    st.HABLANDO: ("Claude hablando", (0.30, 0.78, 0.47), True),
}

# Nombres con sus mayúsculas correctas para los orígenes conocidos. Es una
# tabla abierta, no un if cerrado: un origen que no esté aquí se dibuja
# igualmente (capitalizado, "cursor" -> "Cursor") sin tocar código; la tabla
# solo existe para las mayúsculas bonitas de los conocidos ("opencode" ->
# "OpenCode"). El campo lo publica estado.py (contrato con @integracion)
# y puede no venir: un agente.json escrito por una versión anterior no
# debe romper el servicio en marcha.
NOMBRES_ORIGEN = {"claude": "Claude", "opencode": "OpenCode"}

# Estados en los que se está esperando a algo que puede tardar. Muestran los
# segundos transcurridos: sin eso la tarjeta parece colgada, que es justo la
# impresión que daba mientras el modelo tardaba 8 u 11 segundos.
ESTADOS_CON_RELOJ = {st.CORRIGIENDO, st.TRANSCRIBIENDO, st.PENSANDO}

FONDO = (0.09, 0.09, 0.11)
BORDE = (1, 1, 1, 0.10)
TEXTO = (0.90, 0.90, 0.92)
TEXTO_TENUE = (0.62, 0.62, 0.66)
RESALTE = (0.55, 0.85, 0.60)  # palabras que cambió el corrector

# Botones Copiar y X. Fondo tenue y texto claro, como el resto de la tarjeta,
# para que no compitan con el texto dictado que es lo importante.
BOTON_FONDO = (0.16, 0.16, 0.19)
BOTON_BORDE = (1, 1, 1, 0.18)
BOTON_TEXTO = (0.90, 0.90, 0.92)
BOTON_FONDO_ACTIVO = (0.30, 0.78, 0.47)  # verde breve al copiar, como feedback

_PALABRAS = re.compile(r"\S+")


def palabras_cambiadas(original: str, final: str) -> set:
    """Índices de las palabras de `final` que no estaban en `original`.

    Comparar palabra por palabra en vez de carácter por carácter: lo que
    interesa ver de un vistazo es *qué términos* tocó el modelo.
    """
    if not original or not final:
        return set()
    a, b = original.split(), final.split()
    cambiadas = set()
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for etiqueta, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if etiqueta in ("replace", "insert"):
            cambiadas.update(range(j1, j2))
    return cambiadas


class Indicador(Gtk.Window):
    def __init__(self, esquina="abajo-derecha"):
        super().__init__(type=Gtk.WindowType.POPUP)

        self.esquina = esquina
        self.estado = st.INACTIVO
        self.texto = ""
        self.original = ""
        self.origen = ""  # quién publica el estado del agente, si lo dice
        self.ts = 0.0  # cuándo empezó la fase actual, para el reloj
        self.fase = 0.0  # avanza el latido
        self.opacidad = 0.0  # fundido de entrada/salida
        self.objetivo = 0.0
        self.alto = ALTO_MINIMO
        self._lineas = []  # [(texto_linea, [indices de palabra])]
        self._tick_animacion = None
        self._tick_fundido = None
        self._monitores = {}

        # Botones Copiar y X (solo en PEGADO con texto). Los rectángulos se
        # calculan en _cambiar_a() y se guardan aquí para el hit-testing
        # manual en _al_clic(); no hay widgets GTK, todo es Cairo.
        self._rect_copiar = None
        self._rect_cerrar = None
        # La X oculta la tarjeta localmente sin escribir INACTIVO en el estado
        # compartido (ver _al_clic). Se resetea en _cambiar_a() para que el
        # próximo dictado reabra la tarjeta.
        self._oculta_manual = False
        # El ratón encima de la tarjeta pausa el auto-cierre de PEGADO. Se
        # recalcula por posición en cada _sondear() (ver _raton_sobre_tarjeta),
        # no por eventos enter/leave: la input-shape solo cubre la franja de
        # botones y los leave no llegaban al salir del área clicable.
        self._hover = False
        # Marca de tiempo del último clic en Copiar, para el feedback verde.
        self._ts_copiar = 0.0

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)

        pantalla = self.get_screen()
        visual = pantalla.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)  # transparencia real; hay compositor

        self.set_size_request(ANCHO_TARJETA, self.alto)
        self.connect("draw", self._dibujar)
        self.connect("realize", self._al_realizar)
        self.connect("destroy", Gtk.main_quit)
        # Botones interactivos: hit-testing manual sobre rectángulos conocidos
        # (no hay widgets GTK). El auto-cierre de PEGADO se pausa por posición
        # del puntero en _sondear(), no por enter/leave: la input-shape solo
        # cubre la franja de botones, así que los eventos leave no llegan al
        # salir del área clicable y _hover quedaba pegado en True tras pulsar
        # la X (bug: el siguiente PEGADO no se cerraba nunca).
        self.connect("button-press-event", self._al_clic)

        st.asegurar_directorio()
        self._armar_monitores()
        GLib.timeout_add(MS_RED_SEGURIDAD, self._red_de_seguridad)

    # --- Vigilancia del estado ------------------------------------------

    def _armar_monitores(self):
        """Vigila los directorios de estado que ya existan."""
        for carpeta in (st.ARCHIVO_VOXTYPE.parent, st.DIR_PROPIO):
            if carpeta in self._monitores or not carpeta.is_dir():
                continue
            try:
                monitor = Gio.File.new_for_path(str(carpeta)).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None
                )
            except GLib.Error:
                continue  # sin monitor, la red de seguridad se encarga
            monitor.connect("changed", self._al_cambiar_archivo)
            self._monitores[carpeta] = monitor

    def _al_cambiar_archivo(self, _monitor, _archivo, _otro, _evento):
        self._sondear()

    def _red_de_seguridad(self):
        self._armar_monitores()  # por si VoxType arrancó después que nosotros
        self._sondear()          # y para caducar los estados efímeros
        return GLib.SOURCE_CONTINUE

    def _sondear(self):
        # Medir si el ratón está sobre la tarjeta para pausar el auto-cierre
        # de PEGADO. Se hace por posición en vez de por enter/leave porque
        # la input-shape solo cubre la franja de botones: los eventos leave
        # no llegan cuando el ratón sale del área clicable, y _hover quedaba
        # pegado en True tras pulsar la X (bug: el siguiente PEGADO no se
        # cerraba). Por posición funciona sobre la tarjeta entera, incluido
        # el texto, y no depende de la input-shape.
        self._hover = self._raton_sobre_tarjeta()

        actual = st.estado_actual()
        # Si estamos en PEGADO y el ratón está encima, ignorar la caducidad:
        # estado_actual() ya resolvió que PEGADO caducó y devuelve INACTIVO,
        # pero el usuario está leyendo el texto y decidiendo si copiar. Es
        # lógica local de UI: la caducidad de PEGADO sigue en 10s en estado.py,
        # solo la ignoramos mientras el ratón esté encima.
        if (self.estado == st.PEGADO and self._hover
                and actual["estado"] == st.INACTIVO):
            return
        # El origen también cuenta como cambio: si OpenCode releva a Claude
        # sin cambiar de fase (los dos pasan por PENSANDO con el mismo
        # texto), sin esta línea la etiqueta seguiría diciendo el nombre
        # equivocado hasta el próximo cambio de fase.
        if (actual["estado"] != self.estado
                or actual["texto"] != self.texto
                or actual["original"] != self.original
                or self._origen_de(actual) != self.origen):
            self._cambiar_a(actual)

    def _raton_sobre_tarjeta(self):
        """¿El puntero está sobre la ventana? Por posición, no por eventos.

        Se consulta en cada _sondear() (red de seguridad, 1/s) y en cada
        cambio de archivo de estado; el coste es despreciable. Si cualquier
        llamada falla, devolvemos False: no pausar el auto-cierre es más
        seguro que pausarlo para siempre.
        """
        ventana = self.get_window()
        if ventana is None:
            return False
        try:
            pantalla = self.get_screen()
            display = pantalla.get_display()
            asiento = display.get_default_seat()
            if asiento is None:
                return False
            puntero = asiento.get_pointer()
            if puntero is None:
                return False
            # Gdk.Device.get_position() devuelve 3 valores (screen, x, y),
            # no 4 — el de 4 era Gdk.Display.get_pointer() de GTK2. Las
            # coordenadas son de pantalla, al igual que Gdk.Window.get_position(),
            # así que son comparables directamente.
            _pantalla, px, py = puntero.get_position()
            wx, wy = ventana.get_position()
        except Exception:
            # Fallo silencioso sin traza es el enemigo declarado del proyecto
            # (CLAUDE.md: "VoxType captura stdout y los fallos no se ven").
            # Dejamos constancia en cada fallo; el tope de 200 KB de
            # traza.log evita que inunde.
            st.traza("hover: fallo al consultar el puntero; no se pausa el auto-cierre")
            return False
        ww, wh = ANCHO_TARJETA, self.alto
        return wx <= px <= wx + ww and wy <= py <= wy + wh

    # --- Ventana --------------------------------------------------------

    def _al_realizar(self, _widget):
        """Quita la ventana del alcance del ratón (click-through)."""
        self._actualizar_region_entrada()

    def _actualizar_region_entrada(self):
        """Solo la franja de botones es clicable, y solo en PEGADO.

        La ventana es click-through (región de entrada vacía) porque es un
        indicador, no un control: si interceptara clics, taparía lo que haya
        debajo. Pero en PEGADO hay botones que el usuario debe poder pulsar,
        así que solo entonces se da entrada a la franja de botones -- y solo
        a esa franja, no a toda la tarjeta, para robar el mínimo de clics a
        las ventanas de debajo.

        Se re-aplica en cada _cambiar_a() porque el alto de la tarjeta cambia
        con el texto, y se retira en cuanto el estado deja de ser PEGADO --
        incluido durante el fundido de salida, que la ventana sigue mapeada.
        """
        region = cairo.Region()
        if (self.estado == st.PEGADO and self.texto
                and not self._oculta_manual and self._rect_copiar):
            x, y, w, h = self._rect_copiar
            x2, y2, w2, h2 = self._rect_cerrar
            # La franja abarca desde el borde izquierdo del botón Copiar
            # hasta el borde derecho del botón X, con su alto común.
            x_franja = x
            y_franja = y
            ancho_franja = (x2 + w2) - x
            alto_franja = h
            region = cairo.Region(
                cairo.RectangleInt(x_franja, y_franja, ancho_franja, alto_franja)
            )
        self.get_window().input_shape_combine_region(region, 0, 0)

    def _al_clic(self, _widget, evento):
        """Hit-testing manual de los botones sobre rectángulos conocidos."""
        if self.estado != st.PEGADO or self._oculta_manual:
            return
        if self._en_rectangulo(evento, self._rect_copiar):
            # Gtk.Clipboard funciona aquí porque el indicador es un proceso
            # vivo con bucle GTK (a diferencia de un script efímero, que
            # moriría antes de que el portapapeles se asentara).
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.texto, -1)
            self._ts_copiar = time.time()
            self.queue_draw()  # feedback verde breve
        elif self._en_rectangulo(evento, self._rect_cerrar):
            # La X oculta la tarjeta localmente. NO escribe INACTIVO en el
            # estado compartido: eso rompería la frontera UI↔hooks y causaría
            # un bug de prioridad -- VoxType sigue en "transcribing" al pulsar
            # X, y estado_actual() fusionaría propio=INACTIVO con
            # voxtype=TRANSCRIBIENDO, mostrando "Transcribiendo…" en vez de
            # ocultarse. El flag se resetea en _cambiar_a().
            self._oculta_manual = True
            self._actualizar_region_entrada()  # retirar la entrada ya
            self.objetivo = 0.0
            self._arrancar_fundido()

    @staticmethod
    def _en_rectangulo(evento, rect):
        if rect is None:
            return False
        x, y, w, h = rect
        return x <= evento.x <= x + w and y <= evento.y <= y + h

    def _reubicar(self):
        """Coloca la tarjeta en el área útil, que excluye los paneles."""
        pantalla = self.get_screen()
        display = pantalla.get_display()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()

        if "derecha" in self.esquina:
            x = area.x + area.width - ANCHO_TARJETA - MARGEN_PANTALLA
        else:
            x = area.x + MARGEN_PANTALLA

        if "abajo" in self.esquina:
            y = area.y + area.height - self.alto - MARGEN_PANTALLA
        else:
            y = area.y + MARGEN_PANTALLA

        self.move(x, y)

    def _crear_layout(self, fuente):
        layout = Pango.Layout(self.get_pango_context())
        layout.set_font_description(Pango.FontDescription(fuente))
        return layout

    def _partir_texto(self, texto):
        """Reparte el texto en líneas que quepan, guardando qué palabra es cada una.

        Pango sabe ajustar líneas solo, pero necesitamos saber en qué línea
        cae cada palabra para poder pintar de otro color las que cambiaron.
        Así que el reparto se hace a mano, midiendo palabra a palabra.
        """
        disponible = ANCHO_TARJETA - 2 * PADDING_H
        layout = self._crear_layout(FUENTE_TEXTO)
        lineas, actual, indices = [], "", []

        for i, palabra in enumerate(texto.split()):
            tentativa = f"{actual} {palabra}".strip()
            layout.set_text(tentativa, -1)
            if layout.get_pixel_size()[0] > disponible and actual:
                lineas.append((actual, indices))
                actual, indices = palabra, [i]
                if len(lineas) == MAX_LINEAS:
                    break
            else:
                actual, indices = tentativa, indices + [i]

        if actual and len(lineas) < MAX_LINEAS:
            lineas.append((actual, indices))

        # Si sobró texto, marcar el recorte en la última línea.
        total = len(texto.split())
        if lineas and indices and lineas[-1][1] and lineas[-1][1][-1] < total - 1:
            lineas[-1] = (lineas[-1][0].rstrip() + " …", lineas[-1][1])

        return lineas

    # --- Estado ---------------------------------------------------------

    @staticmethod
    def _origen_de(actual):
        """Origen del agente que publicó el estado, normalizado a texto.

        Con .get() y nunca por índice: un agente.json escrito por una
        versión anterior no lleva este campo y no debe romper el servicio
        en marcha. El isinstance cubre además un valor corrupto (un null,
        un número): .capitalize() al dibujar la etiqueta reventaría con
        cualquier cosa que no sea str, y el indicador tiene que sobrevivir
        a cualquier estado.
        """
        origen = actual.get("origen")
        return origen if isinstance(origen, str) else ""

    def _cambiar_a(self, actual):
        # Cualquier estado nuevo reabre la tarjeta: si el usuario pulsó la X
        # (que solo oculta localmente), el próximo dictado la vuelve a mostrar.
        self._oculta_manual = False

        # El reloj se reinicia solo al cambiar de fase, no cuando llega texto
        # nuevo dentro de la misma: durante "Corrigiendo" el texto se
        # reescribe al terminar, y reiniciar ahí borraría la cuenta.
        if actual["estado"] != self.estado:
            self.ts = time.time()

        self.estado = actual["estado"]
        self.texto = actual["texto"]
        self.original = actual["original"]
        # Normalizado aquí (ver _origen_de) para que el dibujo pueda asumir
        # que self.origen es siempre un str.
        self.origen = self._origen_de(actual)

        if self.estado == st.INACTIVO:
            # Retirar la entrada ya: durante el fundido de salida la ventana
            # sigue mapeada y no debe seguir robando clics a lo de debajo.
            self._rect_copiar = None
            self._rect_cerrar = None
            self._actualizar_region_entrada()
            self.objetivo = 0.0
            self._parar_animacion()
            self._arrancar_fundido()
            return

        _etiqueta, _color, late = ESTILOS[self.estado]
        self._lineas = self._partir_texto(self.texto) if self.texto else []

        layout = self._crear_layout(FUENTE_TEXTO)
        layout.set_text("Ag", -1)
        alto_linea = layout.get_pixel_size()[1]
        layout = self._crear_layout(FUENTE_ESTADO)
        layout.set_text("Ag", -1)
        alto_estado = layout.get_pixel_size()[1]

        self.alto = 2 * PADDING_V + alto_estado
        if self._lineas:
            self.alto += INTERLINEA + len(self._lineas) * (alto_linea + INTERLINEA // 2)

        # En PEGADO con texto hay botones debajo del texto: la tarjeta crece
        # para acomodar la franja. Los rectángulos se calculan aquí porque el
        # alto de la tarjeta (y por tanto la posición de la franja) depende
        # del número de líneas, que acaba de calcularse.
        self._rect_copiar = None
        self._rect_cerrar = None
        if self.estado == st.PEGADO and self.texto:
            y_botones = self.alto + MARGEN_BOTON_V
            self.alto += SEPARACION_BOTONES + ALTO_FRANJA_BOTONES
            # Copiar a la izquierda, X a la derecha, ambos en la franja.
            self._rect_copiar = (
                PADDING_H, y_botones, ANCHO_BOTON_COPIAR, ALTO_BOTON)
            self._rect_cerrar = (
                ANCHO_TARJETA - PADDING_H - ANCHO_BOTON_CERRAR,
                y_botones, ANCHO_BOTON_CERRAR, ALTO_BOTON)

        self.alto = max(self.alto, ALTO_MINIMO)

        self.set_size_request(ANCHO_TARJETA, self.alto)
        self.resize(ANCHO_TARJETA, self.alto)
        self._reubicar()
        self.show_all()

        # La región de entrada cambia con el alto y con el estado: re-aplicarla
        # siempre (en PEGADO da entrada a la franja; en el resto la deja vacía).
        self._actualizar_region_entrada()

        self.objetivo = 1.0
        self.fase = 0.0
        self._parar_animacion()
        if late:
            self._tick_animacion = GLib.timeout_add(MS_ANIMACION, self._latir)
        self._arrancar_fundido()

    def _parar_animacion(self):
        if self._tick_animacion is not None:
            GLib.source_remove(self._tick_animacion)
            self._tick_animacion = None

    def _latir(self):
        self.fase += MS_ANIMACION / 900.0
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _arrancar_fundido(self):
        if self._tick_fundido is None:
            self._tick_fundido = GLib.timeout_add(MS_FUNDIDO, self._fundir)

    def _fundir(self):
        paso = 0.10
        if self.opacidad < self.objetivo:
            self.opacidad = min(self.objetivo, self.opacidad + paso)
        elif self.opacidad > self.objetivo:
            self.opacidad = max(self.objetivo, self.opacidad - paso)
        else:
            self._tick_fundido = None
            if self.opacidad == 0.0:
                self.hide()  # fuera de la pantalla del todo
            return GLib.SOURCE_REMOVE

        self._opacidad(self.opacidad)
        return GLib.SOURCE_CONTINUE

    def _opacidad(self, valor):
        # Gtk.Window.set_opacity está obsoleto; el de Gtk.Widget no.
        Gtk.Widget.set_opacity(self, valor)

    # --- Dibujo ---------------------------------------------------------

    def _reloj(self):
        """Segundos esperando, para los estados que pueden tardar.

        Solo aparece pasado un segundo: en las esperas cortas un contador
        parpadeando molesta más de lo que informa.
        """
        if self.estado not in ESTADOS_CON_RELOJ or not self.ts:
            return ""
        transcurrido = time.time() - self.ts
        if transcurrido < 1.0:
            return ""
        return f"  {transcurrido:.0f} s"

    def _dibujar(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        if self.estado == st.INACTIVO:
            return False

        etiqueta, color, late = ESTILOS[self.estado]

        # Los estados del agente llevan el nombre de quién los origina.
        # Se resuelve aquí, en el punto de dibujo, y no en ESTILOS: el color
        # y el latido son iguales para todos los orígenes, solo cambia el
        # texto. La tabla solo pone las mayúsculas bonitas ("opencode" ->
        # "OpenCode"); un origen desconocido se dibuja igual sin tocar
        # código ("cursor" -> "Cursor pensando…"), y sin origen (un
        # agente.json de una versión anterior) se cae en "Claude" y las
        # etiquetas quedan como siempre -- regresión cero.
        if self.estado in (st.PENSANDO, st.HABLANDO):
            nombre = (NOMBRES_ORIGEN.get(self.origen)
                      or self.origen.capitalize() or "Claude")
            sufijo = "pensando…" if self.estado == st.PENSANDO else "hablando"
            etiqueta = f"{nombre} {sufijo}"

        w, h = ANCHO_TARJETA, self.alto
        radio = min(ALTO_MINIMO / 2, 18 * ESCALA)

        self._caja(cr, 0, 0, w, h, radio)
        cr.set_source_rgba(*FONDO, OPACIDAD_FONDO)
        cr.fill_preserve()
        cr.set_source_rgba(*BORDE)
        cr.set_line_width(1)
        cr.stroke()

        cx = PADDING_H + RADIO_PUNTO
        layout_estado = self._crear_layout(FUENTE_ESTADO)
        layout_estado.set_text(etiqueta + self._reloj(), -1)
        _ew, eh = layout_estado.get_pixel_size()
        cy = PADDING_V + eh / 2

        if late:
            # Halo que se expande y se desvanece, como una onda saliendo del
            # punto. Comunica "esto está pasando ahora" mejor que un parpadeo.
            ciclo = self.fase % 1.0
            cr.set_source_rgba(*color, 0.45 * (1.0 - ciclo))
            cr.arc(cx, cy, RADIO_PUNTO + ciclo * 7.0 * ESCALA, 0, 2 * math.pi)
            cr.fill()
            brillo = 0.80 + 0.20 * math.sin(self.fase * 2 * math.pi)
        else:
            brillo = 1.0

        cr.set_source_rgba(*color, brillo)
        cr.arc(cx, cy, RADIO_PUNTO, 0, 2 * math.pi)
        cr.fill()

        x_texto = cx + RADIO_PUNTO + SEPARACION_PUNTO_TEXTO
        cr.set_source_rgb(*TEXTO)
        cr.move_to(x_texto, PADDING_V)
        PangoCairo.show_layout(cr, layout_estado)

        # Recuento de correcciones, a la derecha de la etiqueta.
        cambiadas = palabras_cambiadas(self.original, self.texto)
        # Solo al final: durante "Corrigiendo" ese hueco lo ocupa el reloj.
        if cambiadas and self.estado == st.PEGADO:
            # Contar tramos seguidos, no palabras sueltas: "hola Macla" ->
            # "Ollama Cloud" son dos palabras pero una sola corrección, que
            # es como lo contaría cualquiera al mirarlo.
            n = sum(1 for i in sorted(cambiadas) if i - 1 not in cambiadas)
            resumen = f"{n} corrección" if n == 1 else f"{n} correcciones"
            layout_res = self._crear_layout(FUENTE_TEXTO)
            layout_res.set_text(resumen, -1)
            rw, rh = layout_res.get_pixel_size()
            cr.set_source_rgb(*TEXTO_TENUE)
            cr.move_to(w - PADDING_H - rw, PADDING_V + (eh - rh) / 2)
            PangoCairo.show_layout(cr, layout_res)

        # El texto, palabra a palabra para poder resaltar lo que cambió.
        if self._lineas:
            layout = self._crear_layout(FUENTE_TEXTO)
            layout.set_text("Ag", -1)
            alto_linea = layout.get_pixel_size()[1]
            y = PADDING_V + eh + INTERLINEA

            for linea, indices in self._lineas:
                x = PADDING_H
                for palabra, idx in zip(_PALABRAS.findall(linea), indices + [None]):
                    layout.set_text(palabra, -1)
                    pw, _ph = layout.get_pixel_size()
                    resaltar = idx is not None and idx in cambiadas
                    cr.set_source_rgb(*(RESALTE if resaltar else TEXTO_TENUE))
                    cr.move_to(x, y)
                    PangoCairo.show_layout(cr, layout)
                    layout.set_text(" ", -1)
                    x += pw + layout.get_pixel_size()[0]
                y += alto_linea + INTERLINEA // 2

        # Botones Copiar y X, debajo del texto, solo en PEGADO con texto.
        # Van después del texto para no desplazarlo: el resaltado de las
        # correcciones sigue alineado.
        if self.estado == st.PEGADO and self.texto and not self._oculta_manual:
            self._dibujar_botones(cr)

        return False

    def _dibujar_botones(self, cr):
        """Dibuja los botones Copiar y X con Cairo.

        No hay widgets GTK: todo el dibujo es Cairo manual, y el hit-testing
        se hace sobre los rectángulos guardados en _cambiar_a(). El botón
        Copiar muestra un feedback verde breve tras pulsarlo.
        """
        layout = self._crear_layout(FUENTE_TEXTO)

        # Defensa: los rectángulos se calculan en _cambiar_a() bajo las mismas
        # condiciones que este dibujo, pero si por cualquier motivo faltaran,
        # mejor no dibujar que reventar el proceso (el indicador tiene que
        # sobrevivir a cualquier estado).
        if self._rect_copiar is None or self._rect_cerrar is None:
            return

        # Botón Copiar (izquierda).
        x, y, w, h = self._rect_copiar
        activo = time.time() - self._ts_copiar < 0.6
        cr.set_source_rgba(*(BOTON_FONDO_ACTIVO if activo else BOTON_FONDO))
        self._caja(cr, x, y, w, h, RADIO_BOTON)
        cr.fill_preserve()
        cr.set_source_rgba(*BOTON_BORDE)
        cr.set_line_width(1)
        cr.stroke()
        layout.set_text("Copiar", -1)
        tw, th = layout.get_pixel_size()
        cr.set_source_rgb(*BOTON_TEXTO)
        cr.move_to(x + (w - tw) / 2, y + (h - th) / 2)
        PangoCairo.show_layout(cr, layout)

        # Botón X (derecha).
        x, y, w, h = self._rect_cerrar
        cr.set_source_rgba(*BOTON_FONDO)
        self._caja(cr, x, y, w, h, RADIO_BOTON)
        cr.fill_preserve()
        cr.set_source_rgba(*BOTON_BORDE)
        cr.set_line_width(1)
        cr.stroke()
        layout.set_text("✕", -1)
        tw, th = layout.get_pixel_size()
        cr.set_source_rgb(*BOTON_TEXTO)
        cr.move_to(x + (w - tw) / 2, y + (h - th) / 2)
        PangoCairo.show_layout(cr, layout)

    @staticmethod
    def _caja(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


def main():
    esquina = sys.argv[1] if len(sys.argv) > 1 else "abajo-derecha"
    validas = {"abajo-derecha", "abajo-izquierda", "arriba-derecha", "arriba-izquierda"}
    if esquina not in validas:
        print(f"esquina no válida: {esquina}\nusa una de: {', '.join(sorted(validas))}",
              file=sys.stderr)
        return 1

    # Sin esto, Ctrl+C no corta: GTK se come la señal.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    indicador = Indicador(esquina)
    indicador._opacidad(0.0)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
