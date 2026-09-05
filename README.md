# Indicador de voz

![Demo](demo.gif)

Una tarjeta flotante en la esquina de la pantalla que muestra quién tiene la
palabra —tú dictando, o el agente pensando y hablando— **y qué texto hay en
juego** en cada momento.

Cuando no pasa nada se esconde del todo. Un indicador permanente se vuelve
invisible a los cinco minutos y encima estorba.

## Estados

| En pantalla | Color | Significado | Quién lo publica |
|---|---|---|---|
| Grabando | rojo, latiendo | VoxType está capturando tu voz | VoxType |
| Transcribiendo… | ámbar, latiendo | Whisper está trabajando | VoxType |
| Recibido | ámbar, fijo | Lo que entendió Whisper, tal cual | `corregir` |
| Corrigiendo… | morado, latiendo | El LLM está repasando la transcripción | `corregir` |
| Pegado | verde, fijo | El texto final, con los cambios resaltados | `pegar` |
| Claude pensando… | azul, latiendo | El agente está generando la respuesta | hook del agente |
| Claude hablando | verde, latiendo | El TTS está leyendo la respuesta | script de voz |

Late todo lo que está en marcha (grabar, transcribir, corregir, pensar,
hablar); los resultados —`Recibido` y `Pegado`— se quedan fijos, para que el
movimiento signifique algo en vez de ser decoración.

Si coinciden varios, gana el de más arriba en la tabla, con dos excepciones
que no son evidentes:

- **"Grabando" manda sobre todo**: es el único que confirma algo que estás
  haciendo tú en ese momento, y perderlo de vista inutiliza el indicador.
- **`recibido`, `corrigiendo` y `pegado` van por delante de
  `transcribiendo`**, porque VoxType mantiene su estado en `transcribing`
  mientras ejecuta los hooks — que es justo cuando esas tres fases ocurren.
  Sin esa excepción no se verían nunca.

## La tarjeta

El texto se parte a mano palabra por palabra en vez de dejárselo a Pango,
porque hace falta saber en qué línea cae cada palabra para poder **pintar de
verde las que cambió el corrector**. Máximo 3 líneas; lo que sobra se corta
con `…`.

El recuento ("4 correcciones") cuenta **tramos seguidos**, no palabras
sueltas: `hola Macla → Ollama Cloud` son dos palabras pero una sola
corrección, que es como lo contaría cualquiera al mirarlo.

En `Pegado` con texto aparecen dos botones: **Copiar** (devuelve el texto al
portapapeles) y **X** (esconde la tarjeta). La X solo la oculta localmente —
no toca el estado compartido, que VoxType sigue en lo suyo.

La etiqueta de estado va en negrita y mayor; el texto dictado, pequeño y
tenue. Jerarquía: primero se lee qué pasa, después el texto.

`Pegado` y `Recibido` se desvanecen solos (10 s y 12 s). Hace falta porque el
script que los escribió ya terminó: nadie va a apagarlos, y ningún evento de
archivo va a avisar. De ahí el sondeo de respaldo cada segundo. El más largo
es `Pegado`: muestra los botones Copiar/X y hace falta tiempo para leer y
decidir; tener el ratón encima pausa el cierre.

**Los estados de espera muestran los segundos** ("Corrigiendo… 4 s") y su
punto late. Sin eso la tarjeta parece colgada, que es justo la impresión que
daba cuando el modelo tardaba 8 u 11 segundos. El reloj se reinicia al cambiar
de fase, no cuando llega texto nuevo dentro de la misma.

## Depurar la cadena

VoxType ejecuta los hooks con la salida capturada, así que un `print` no
aparece en ninguna parte. Por eso dejan traza en un archivo:

```sh
tail -f /run/user/1000/indicador-voz/traza.log
```

```
20:28:10.244  corregir: entra (21 car.)
20:28:21.240  corregir: descartada: respuesta vacía
20:28:21.327  pegar: entra
20:28:21.993  pegar: estado -> pegado
```

Esa traza es la que reveló que la tarjeta no estaba colgada: los hooks
funcionaban y el que tardaba 11 s era el modelo.

## Piezas

| Archivo | Qué hace |
|---|---|
| `indicador.py` | La ventana GTK. Es el proceso que corre de fondo. |
| `estado.py` | Une las dos fuentes de estado en una sola lectura. |
| `voz-estado` | Orden que usan los hooks para publicar el estado del agente. |
| `corrector.py` | Manda la transcripción a un LLM para que la repase. |
| `corregir` | Hook `post_process` de VoxType: stdin → stdout. |
| `pegar` | Hook `post_output_command`: pulsa Ctrl+Shift+V y marca "Pegado". |
| `tts.py` | Llama a ElevenLabs por REST (`urllib`) para sintetizar voz. |
| `hablar` | Resume si es largo, sintetiza con ElevenLabs, reproduce y publica `hablando`. |
| `hook-stop` | Hook `Stop` de Claude Code: lanza `hablar` despegado (fire-and-forget). |

El servicio systemd y los hooks se configuran fuera del repo, referenciados
por rutas absolutas desde `~/.config/systemd/user/indicador-voz.service` y
`~/.config/voxtype/config.toml`. La cadena necesita `ydotool` y `xclip`
instalados. Clonar el repo no basta para reproducir el montaje.

## Corrección de transcripciones

Whisper acierta el sonido pero no el contexto: oye *"hola Macla"* donde dijiste
*"Ollama Cloud"*. Entre la transcripción y el pegado se mete un LLM con la lista
de términos que usas.

```
VoxType → [LLM por stdin] → portapapeles → Ctrl+Shift+V
```

Va enganchado en **`[output.post_process]`** del `config.toml` de VoxType, un
hook que ese programa ya trae: recibe el texto por stdin y devuelve el
corregido por stdout. **No hay que parchear VoxType.**

Es mejor sitio que `post_output_command` por tres razones: el texto llega por
stdin en vez de por el portapapeles, ocurre *antes* de que VoxType escriba el
portapapeles (así su flujo normal sigue intacto), y VoxType ya trae su propio
timeout y su propio respaldo al original.

### La regla que gobierna el diseño: nunca perder el dictado

Si la red falla, si el modelo tarda, si devuelve algo raro — se pega el texto
original. Una corrección que no llega es una molestia; un dictado perdido es
tener que repetirlo entero. Se descarta la corrección si:

- llega vacía, o el modelo se puso a comentar en vez de corregir;
- **acorta el texto por debajo del 70%** — un modelo puede decidir "resumir" y
  comerse frases enteras, y pasó en las pruebas;
- lo alarga por encima del 160%, señal de que respondió en vez de corregir.

### Modelo y proveedor

En uso: **`gemma4:31b`** por **Ollama Cloud**. Los dos proveedores hablan el
mismo dialecto (compatible con OpenAI), así que cambiar es una línea.

Medido con errores de dictado reales, 18 llamadas por modelo. **En calidad casi
empatan** — todos aciertan los términos habituales. Lo que los separa es la
latencia y sobre todo **su varianza**: esa espera ocurre con el texto ya
transcrito, antes de que aparezca en la ventana, así que se nota mucho.

| Proveedor / modelo | Mediana | p90 | Peor | Descartes |
|---|---|---|---|---|
| ollama / **`gemma4:31b`** | **0,72 s** | **1,02 s** | **1,24 s** | **0/18** |
| openrouter / `gemini-2.5-flash-lite` | 0,74 s | 0,83 s | 0,99 s | 0/6 |
| ollama / `deepseek-v4-flash:0731` | 2,26 s | 5,91 s | 6,10 s | 1/18 |
| ollama / `minimax-m3` | 2,28 s | 8,00 s | 11,35 s | 1/18 |
| openrouter / `gemini-3.6-flash` | 2,34 s | — | 3,85 s | 0/6 |

`gemma4` fue además de los pocos que sacó *"presencia"* de *"presidencia"*.

<details>
<summary>Mediciones completas: variantes de Gemma, descartes y notas</summary>

#### Otras variantes de Gemma

En Ollama Cloud solo hay `gemma4:31b`. En OpenRouter hay más, y se midieron
(segunda tanda, sesión distinta):

| Modelo | Mediana | p90 | Peor | Notas |
|---|---|---|---|---|
| ollama / **`gemma4:31b`** | **0,67 s** | **0,94 s** | 2,28 s | el que está en uso |
| `google/gemma-4-26b-a4b-it` | 1,18 s | 1,87 s | **1,88 s** | MoE, el más constante |
| `google/gemma-4-31b-it` | 0,98 s | 5,27 s | 8,26 s | mismo modelo, peor servido |
| `google/gemma-4-31b-it:free` | — | — | — | **HTTP 429 siempre** |
| `google/gemma-3-27b-it` | 1,11 s | 2,83 s | 2,97 s | falla el caso difícil |
| `google/gemma-3-12b-it` | 0,97 s | 1,27 s | 1,90 s | falla el caso difícil |

Dos conclusiones que no se ven en los números:

- **Solo la familia Gemma 4 corrige el caso difícil.** Los Gemma 3 dejaron
  *"interés artificial"* y *"presidencia"* sin tocar. Para esta tarea el
  tamaño importa menos que la generación.
- **Las versiones `:free` no sirven**: devuelven `429 rate-limited upstream`
  de forma sistemática. Las 12 llamadas de prueba se descartaron — y el
  dictado se conservó intacto las 12 veces, que es justo para lo que están
  las guardas.

Si alguna vez `gemma4:31b` da picos en Ollama Cloud, el relevo natural es
`google/gemma-4-26b-a4b-it`: algo más lento de mediana pero el más regular de
todos (peor caso 1,88 s), y acierta el caso difícil.

**En Ollama Cloud no hay Gemini**: es propietario de Google y ese servicio
aloja modelos de pesos abiertos. `gemma4` es el modelo abierto de Google, que
es otra cosa. Gemini solo está por OpenRouter.

Descartados por comportamiento, no por velocidad: **`gpt-5-nano` filtraba su
propio razonamiento** dentro del texto ("Wait. Follow rules: correct only…"),
que se habría pegado tal cual; `mistral-nemo` tardaba 6,6 s y reformulaba;
`minimax-m3` convirtió *"presidencia"* en *"presentación"*.

</details>

Las claves salen de `OLLAMA_API_KEY` / `OPENROUTER_API_KEY` o, si no están, de
donde las guarda OpenCode (`~/.local/share/opencode/auth.json`). Pese al nombre,
**`ollama-cloud` es un servicio remoto**: no hay Ollama local instalado.

### Ajustes

Opcional, en `~/.config/indicador-voz/corrector.json`. Lo más útil es el
glosario: la clave es el término correcto y la lista, las formas en que se oye
mal.

```json
{
  "activo": true,
  "proveedor": "ollama-cloud",
  "modelo": "gemma4:31b",
  "timeout_s": 10,
  "glosario": { "Ollama Cloud": ["hola Macla", "la MacCloud"] }
}
```

Para cambiar de proveedor bastan dos líneas:

```json
{ "proveedor": "openrouter", "modelo": "google/gemini-2.5-flash-lite" }
```

Los modelos de Ollama Cloud se listan con:

```sh
curl -s -H "Authorization: Bearer $(python3 -c "import json;print(json.load(open('$HOME/.local/share/opencode/auth.json'))['ollama-cloud']['key'])")" \
  https://ollama.com/api/tags | python3 -m json.tool
```

Para probar sin pegar nada:

```sh
echo "lo hice en Cloud IP con Boxed" | ./corregir
```

**Aviso:** el texto dictado se envía al proveedor configurado (Ollama Cloud
por defecto). Si dictas algo que no deba salir del equipo, pon `"activo":
false`.

## Lectura de respuestas en voz alta (TTS)

Cuando el agente termina de responder, un hook `Stop` de Claude Code lanza
`hablar`, que resume la respuesta (si es larga), la sintetiza con ElevenLabs
y la reproduce por el altavoz. La tarjeta muestra "Claude hablando" mientras
suele.

```
Claude responde → hook Stop → hablar → resumir (LLM) → sintetizar (ElevenLabs) → paplay
```

- **Motor**: ElevenLabs por API REST directa (`urllib`, sin SDK — respeta la
  restricción sin-pip). Modelo `eleven_flash_v2_5` (latencia ~75ms,
  $0.05/1K chars). Voz por defecto en el código: Carlos Aguilar
  (`8MeTTgXVwMEhRVfblXOj`), que es de la **librería y requiere plan de
  pago** — en plan Free devuelve HTTP 402 y el TTS cae a `spd-say`. La voz
  predefinida de Free es **Adam** (`pNInz6obpgDQGcFmaJgB`), que es la que
  trae el `hablar.json` de la máquina de uso diario; si montas desde cero
  con plan Free, pon esa en tu config.
- **Resumen**: respuestas largas se resumen con el mismo LLM del corrector
  (`gemma4:31b` por Ollama Cloud). Personalidad 80% técnica, 20% amable y
  jocosa, colombiana. Máximo 350 caracteres. Las respuestas cortas (<200
  chars) y limpias (sin código) se leen directo sin resumir.
- **Corte**: si empiezas a dictar mientras suena, el audio se corta solo
  (sondea el estado de VoxType cada 250ms). También `hablar --cortar`
  silencia al instante.
- **Fallback**: si ElevenLabs falla (red, clave, HTTP), cae a `spd-say`
  (voz robótica local, sin coste).
- **Config**: `~/.config/indicador-voz/hablar.json` (permisos 0600) con
  `api_key`, `voz_id`, `modelo`, `activo` (false por defecto).

**Aviso de privacidad:** las respuestas del agente salen del equipo hacia
ElevenLabs (síntesis) y hacia el LLM resumidor (Ollama Cloud). Pueden
contener contenido de archivos, credenciales o datos sensibles. Si no
quieres que salgan, pon `"activo": false` en `hablar.json`.

### Hook de Claude Code

El hook `Stop` está en `~/.claude/settings.json` y apunta a `hook-stop` por
ruta absoluta. Es fire-and-forget: lanza `hablar` despegado y sale en
milisegundos, sin bloquear a Claude Code.

### La trampa de xclip (histórica, pero no la repitas)

El montaje anterior usaba `xclip` desde `post_output_command`. `xclip` deja un
hijo vivo que hereda las tuberías de VoxType; la tubería nunca da EOF y VoxType
se queda en `transcribing` para siempre. Mover la corrección a `post_process`
(stdin/stdout) lo elimina de raíz. Si alguna vez añades una orden que sobreviva
al script, mándale stdout/stderr a `/dev/null` y usa `start_new_session=True`.
Ver un `xclip` vivo en `ps` es normal: es el que sostiene el portapapeles.

## Uso

```sh
voz-estado              # qué se está mostrando ahora
voz-estado pensando     # el agente empezó a trabajar
voz-estado hablando     # el TTS está leyendo
voz-estado inactivo     # terminó
```

Los estados de VoxType **no hay que publicarlos**: el demonio ya escribe su
propio estado y esto lo lee directamente.

## Servicio

Corre como servicio de usuario, arranca solo al iniciar sesión:

```sh
systemctl --user status indicador-voz
systemctl --user restart indicador-voz
```

Para probar a mano, en otra esquina:

```sh
systemctl --user stop indicador-voz
./indicador.py arriba-derecha
```

Esquinas válidas: `abajo-derecha` (por defecto), `abajo-izquierda`,
`arriba-derecha`, `arriba-izquierda`.

## Notas de implementación

**No usa `graphical-session.target`.** En Cinnamon ese target nunca se
activa, y el servicio se quedaría parado sin dar ningún error. Es el mismo
problema que tenía VoxType. Va colgado de `default.target`.

**Vigila directorios, no archivos.** Un monitor sobre un archivo deja de
funcionar en cuanto alguien lo reemplaza por `rename` — que es justo como se
escriben los estados de forma atómica. Es la causa clásica de que estos
indicadores se queden congelados. El monitor de directorio sobrevive al
reemplazo.

**Por eventos, no por sondeo.** Medido en este equipo: sondear cada 100 ms
costaba 0,35% de CPU permanente; por eventos queda en 0,00%, con detección en
13–44 ms. Sobre un portátil eso es batería a cambio de nada. Queda un sondeo
de respaldo cada 1 s que cubre el arranque anterior a VoxType y rearma los
monitores cuando su directorio aparece.

**GTK3 del sistema, sin dependencias.** Es deliberado: este Python no tiene
el módulo `pip`, así que cualquier dependencia externa obligaría a montar un
entorno virtual antes de poder ver nada en pantalla. PyGObject ya viene en
Mint.

**La ventana es click-through salvo los botones.** Solo la franja de
Copiar/X en `Pegado` recibe clics; el resto de la tarjeta se atraviesa. Es un
indicador, no un control.

## Tamaño y transparencia

Dos constantes al principio de `indicador.py`:

- **`ESCALA`** (1.4): el alto, la fuente, el punto y los márgenes salen de
  aquí. Súbela o bájala y reinicia el servicio.
- **`OPACIDAD_FONDO`** (0.80): 1.0 es opaco. Por debajo de 0.65 el texto
  empieza a competir con lo que haya detrás.
- **`ANCHO_TARJETA`** y **`MAX_LINEAS`** (3) controlan cuánto texto cabe.

```sh
systemctl --user restart indicador-voz
```

## Estado del proyecto

Los siete estados de la tabla se dibujan, la integración con VoxType está
probada de punta a punta, la corrección de transcripciones funciona, y el
TTS con ElevenLabs está implementado y enganchado a Claude Code. Lo que
falta:

- Plugin de OpenCode sobre `session.idle` → `hablando` / `inactivo`
- Revisión de @security sobre la superficie de privacidad del TTS
- Streaming de MP3 para reducir latencia (reproducir antes de tener el
  archivo completo)
- Mejorar la interfaz gráfica (distinguir Claude vs OpenCode)

Mientras tanto, `voz-estado` publica los estados a mano y sirve para probar.
