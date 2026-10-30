"""
El unico sitio que arma y lanza una orden del CLI de Claude.

Por que existe
--------------
La lista de argumentos estaba copiada en tres ficheros (p3_guion, p5_revision_audio
y app.py) y ya paso una vez que "faltaban los flags del CLI en dos de los cuatro
sitios": el paso de guion tardaba 70 s y el mismo trabajo lanzado desde otra
pantalla tardaba diez minutos, porque a esa copia se le habia olvidado --effort.
Un solo sitio hace imposible ese fallo.

El esfuerzo es la variable que mas manda
----------------------------------------
Medido con el mismo transcript de 9.341 palabras y la misma instruccion:

    sin --effort, con el entorno limpio      605,8 s   78.447 tokens de salida
    sin --effort, heredando CLAUDE_EFFORT    336,3 s   39.679 tokens de salida
    con --effort low                          67,0 s    7.031 tokens de salida

De ahi salen dos reglas que este modulo aplica siempre:

1. El esfuerzo va SIEMPRE explicito. Si no, el mismo ajuste tarda distinto segun
   con que entorno se arranco el servicio, y entonces ninguna medida vale.
2. **El tiempo maximo escala con el esfuerzo.** Un techo fijo de 420 s con
   esfuerzo 'high' es una trampa: garantiza TiempoAgotado, y TiempoAgotado no se
   reintenta a proposito. Ofrecer un esfuerzo cuyo techo no le da tiempo a
   terminar es ofrecer un boton que solo sabe fallar.

Uso
---
    from . import cli_claude

    texto, sobre = cli_claude.ejecutar(
        instruccion, modelo="sonnet", esfuerzo="low",
        cwd=carpeta, tiempo_max_s=0)      # 0 = automatico segun el esfuerzo

`sobre` es el JSON del CLI con un bloque `_ajuste` anadido (modelo, esfuerzo y
segundos reales), que es lo que despues leen el medidor de coste y el historico.
El CLI no informa del esfuerzo en su respuesta, asi que si no lo estampa quien
llama, el historico se queda ciego justo en la variable que mas mueve el tiempo.
"""
import os
import re
import shutil
import signal
import subprocess
import time

# Asegura que las carpetas habituales de instalacion de `agy` esten en PATH
# antes de que `shutil.which` busque el binario, sin romper los dobles de prueba
# que sustituyen `shutil.which`.
for _dir_extra in (
    os.path.expanduser("~/.gemini/bin"),
    os.path.expanduser("~/.local/bin"),
    os.path.join(os.environ.get("LOCALAPPDATA") or "", "agy", "bin"),
):
    if _dir_extra and os.path.isdir(_dir_extra):
        _path_actual = os.environ.get("PATH") or ""
        if _dir_extra not in _path_actual.split(os.pathsep):
            os.environ["PATH"] = (
                f"{_dir_extra}{os.pathsep}{_path_actual}" if _path_actual else _dir_extra
            )

# ------------------------------------------------------------------ catalogo

# Alias que entiende el CLI (Antigravity CLI `agy`). Se validan contra esta
# lista porque un modelo mal escrito solo se detecta por returncode != 0, o sea
# despues de haber pagado el arranque del CLI y la espera entera.
SIN_VENTANA = {}
if os.name == "nt":
    SIN_VENTANA = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}

MODELOS = ("gemini-3.8-flash", "gemini-4-argon")

# Alias cortos y nombres heredados (para que proyectos guardados o pruebas que
# pidan haiku/sonnet/opus se traduzcan limpiamente a los modelos de agy).
ALIAS_MODELOS = {
    "flash": "gemini-3.8-flash",
    "argon": "gemini-4-argon",
    "haiku": "gemini-3.8-flash",
    "sonnet": "gemini-3.8-flash",
    "opus": "gemini-4-argon",
}

# Un id completo tambien vale: el catalogo cambia mas rapido que este fichero.
PATRON_MODELO_COMPLETO = re.compile(
    r"^(?:gemini-[a-z0-9][a-z0-9.\-]*|claude-[a-z0-9][a-z0-9.\-]*)$"
)

ESFUERZOS = ("low", "medium", "high", "xhigh", "max")

# LO QUE CORRE CUANDO NADIE ELIGE: GEMINI 4 ARGON CON ESFUERZO MUY ALTO.
MODELO_POR_DEFECTO = "gemini-4-argon"
ESFUERZO_POR_DEFECTO = "xhigh"

# Cuanto multiplica cada esfuerzo al tiempo de 'low'. NO son medidas: 'low' es lo
# unico medido de verdad (67 s frente a los 605,8 s de no pasar el flag). Son
# ordenes de magnitud para que el techo de tiempo y la primera estimacion sean
# razonables; en cuanto haya historial de una combinacion, manda el historial.
FACTOR_ESFUERZO = {"low": 1.0, "medium": 2.5, "high": 5.0, "xhigh": 8.0, "max": 12.0}

# Lo mismo para el modelo, relativo a la referencia base (1.0).
FACTOR_MODELO = {
    "gemini-3.8-flash": 1.0,
    "gemini-4-argon": 2.2,
    "haiku": 0.6,
    "sonnet": 1.0,
    "opus": 2.2,
}

# Techo absoluto. Un trabajo puede tardar mucho y eso es legitimo (la barra lo
# ensena y se puede cancelar), pero mas de una hora esperando a una sola llamada
# es casi siempre algo colgado, no algo trabajando.
TIEMPO_MAXIMO_S = 3600
TIEMPO_MINIMO_S = 30

# Variables del entorno que le cambian el razonamiento al CLI.
ENTORNO_FUERA = ("CLAUDE_EFFORT", "MAX_THINKING_TOKENS", "AGY_EFFORT")

# Y las que sacarian la llamada FUERA de la suscripcion del CLI.
PAGO_POR_USO = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

# Sin herramientas el CLI contesta en un solo turno: para redactar texto no tiene
# nada que leer ni que escribir, todo el material va en la instruccion.
HERRAMIENTAS_VETADAS = ("Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob",
                        "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite")


class TiempoAgotado(RuntimeError):
    """La llamada al CLI vencio. No se reintenta: volveria a vencer."""


class LimiteAgotado(RuntimeError):
    """Se acabo el cupo de la suscripcion. Tampoco se reintenta.

    Pasa lo mismo que con un timeout y por eso comparte forma: reintentar es
    volver a fallar. Pero se distingue por dos motivos, y los dos se vieron el
    24-08-2026, cuando el cupo semanal se agoto a mitad de una tanda:

    1. **Lo que se ve.** El CLI sale con codigo 1 y su mensaje ("has agotado tu
       limite semanal, se renueva el 27 de agosto a las 7:00") viaja DENTRO del
       sobre JSON, en `result`. Sin mirar ahi, lo que llegaba a la pantalla era
       600 caracteres de JSON crudo con `input_tokens: 0` -- que parece una
       averia del Estudio y no lo es.

    2. **Lo que se hace.** Un cupo agotado se reintenta unas cuantas veces en un
       segundo cada una, se gastan todos los reintentos y el paso muere igual;
       peor: el error que se guarda es el del ULTIMO intento, o sea el mismo
       mensaje pero sin la pista de que ya se sabia desde el primero.

    Con la cuenta de respaldo SI se reintenta una vez (`ejecutar`): un login
    distinto tiene un cupo distinto, que es justo para lo que existe.
    """


# ------------------------------------------------------------------ validacion

#: Con que codigo sale una SUITE que no ha podido correr porque no hay cupo.
#: No es 1 a proposito: `pruebas.ps1` lo pinta como CUPO y no lo cuenta como
#: fallo. Una suite que necesita el CLI de verdad --la cadena de guion es la
#: unica-- se pondria en rojo durante los dias que tarde en renovarse el cupo, y
#: un rojo que no significa nada roto es peor que no correrla: ensena a ignorar
#: el rojo. 3 y no 2 porque 2 es lo que devuelve el propio python al no poder
#: abrir el fichero.
CODIGO_SIN_CUPO = 3

#: Como suena un cupo agotado, en los dos idiomas en que puede contestar el CLI.
#: Se casa por PAREJAS (algo de limite + algo de agotado/renovado) y no por
#: frases enteras: el texto exacto cambia cada vez que cambia el CLI, y una
#: frase literal que deje de casar volveria a dar el JSON crudo en pantalla.
_LIMITE_QUE = ("limit", "limite", "límite", "quota", "cuota")
_LIMITE_COMO = ("reset", "renov", "reach", "hit your", "has alcanzado",
                "has agotado", "agotado", "exceed", "superado", "upgrade")


def limite_de(texto):
    """El mensaje si el CLI dice que se acabo el cupo; si no, None. -> str|None

    Se lee del `result` del sobre, que es donde el CLI pone su explicacion en
    cristiano. Lo que devuelve es ese mismo texto limpio, para poder ensenarlo
    tal cual: dice la fecha en que se renueva, y esa fecha es lo unico que le
    importa a quien esta mirando la pantalla.
    """
    plano = str(texto or "").strip()
    if not plano:
        return None
    bajo = plano.lower()
    if any(q in bajo for q in _LIMITE_QUE) and any(c in bajo for c in _LIMITE_COMO):
        return plano
    return None


def modelo_valido(valor):
    """True si el CLI va a entender ese modelo."""
    texto = str(valor or "").strip().lower()
    return bool(texto) and (texto in MODELOS
                            or texto in ALIAS_MODELOS
                            or bool(PATRON_MODELO_COMPLETO.match(texto)))


def normalizar_modelo(valor, estricto=True, defecto=MODELO_POR_DEFECTO):
    respaldo = ALIAS_MODELOS.get(str(defecto or "").strip().lower(),
                                 defecto or MODELO_POR_DEFECTO)
    texto = str(valor or "").strip().lower()
    if not texto:
        return respaldo
    if texto in ALIAS_MODELOS:
        return ALIAS_MODELOS[texto]
    if not modelo_valido(texto):
        if estricto:
            raise ValueError(
                f"modelo desconocido: {texto!r}. Usa uno de "
                f"{', '.join(MODELOS)} o un id completo tipo 'gemini-4-argon'")
        return respaldo
    return texto


def normalizar_esfuerzo(valor, estricto=True, defecto=ESFUERZO_POR_DEFECTO):
    texto = str(valor or "").strip().lower()
    if not texto:
        return defecto
    if texto not in ESFUERZOS:
        if estricto:
            raise ValueError(
                f"esfuerzo tiene que ser uno de {', '.join(ESFUERZOS)}")
        return defecto
    return texto


def factor(modelo=MODELO_POR_DEFECTO, esfuerzo=ESFUERZO_POR_DEFECTO):
    """Cuanto se espera que multiplique una combinacion al tiempo de referencia."""
    m = str(modelo or "").strip().lower()
    m = ALIAS_MODELOS.get(m, m) if m not in FACTOR_MODELO else m
    return (FACTOR_MODELO.get(m, 1.0)
            * FACTOR_ESFUERZO.get(str(esfuerzo or "").strip().lower(), 1.0))


def tiempo_max(esfuerzo=ESFUERZO_POR_DEFECTO, base_s=420, modelo=None,
               pedido_s=0):
    """Segundos que se le dan a UNA llamada antes de cortarla.

    `pedido_s` mayor que cero manda: es el techo que ha fijado una persona a
    mano. Con 0 el techo se deriva del esfuerzo, que es lo unico que evita la
    trampa de ofrecer 'high' contra un techo pensado para 'low'.
    """
    try:
        pedido_s = int(pedido_s or 0)
    except (TypeError, ValueError):
        pedido_s = 0
    if pedido_s > 0:
        return max(TIEMPO_MINIMO_S, min(TIEMPO_MAXIMO_S, pedido_s))
    escala = factor(modelo or MODELO_POR_DEFECTO, esfuerzo)
    return int(max(TIEMPO_MINIMO_S,
                   min(TIEMPO_MAXIMO_S, round(float(base_s) * escala))))


# Ajuste por defecto de cada fase, elegido por la COMPLEJIDAD de su tarea en
# Antigravity CLI (`agy`): `gemini-3.8-flash` para clasificacion y transformacion
# acotada; `gemini-4-argon` para redaccion y criterio visual/narrativo.
POR_FASE = {
    "guion": {"modelo": "gemini-3.8-flash", "esfuerzo": "medium",
              "porque": "generacion de contenido larga con restricciones; en la "
                        "matriz por tarea eso es esfuerzo medio, y el historial "
                        "dice que en bajo ya termina bien 20 de 20 veces"},

    "tono": {"modelo": "gemini-4-argon", "esfuerzo": "medium",
             "porque": "destila de varias transcripciones como escribe un canal, "
                       "y eso lo heredan todos sus videos; medio y no alto "
                       "porque lo lee y lo corrige una persona antes de usarlo"},

    "voz_descrita": {"modelo": "gemini-3.8-flash", "esfuerzo": "low",
                     "porque": "leer un catalogo y elegir; lo que salga se ve "
                               "en los mandos antes de pagar ninguna sintesis"},

    "catalogo_visual": {"modelo": "gemini-3.8-flash", "esfuerzo": "medium",
                        "porque": "una sola llamada por video que decide el "
                                  "reparto y los sitios de todo el montaje; "
                                  "equivocarse aqui sale mucho mas caro que el "
                                  "escalon de esfuerzo"},

    "direccion": {"modelo": "gemini-4-argon", "esfuerzo": "high",
                  "porque": "el trabajo es COMPARAR unos planos con otros para "
                            "que no se repitan, no describir uno: es criterio "
                            "sobre el conjunto, y son una o dos llamadas por "
                            "video, asi que el esfuerzo alto sale barato"},

    "redactor": {"modelo": "gemini-4-argon", "esfuerzo": "high",
                 "porque": "escribe el encargo entero de cada plano y elige su "
                           "sitio entre los del tramo: son mas decisiones por "
                           "plano que dirigirlo, y sigue siendo una sola "
                           "llamada por video"},

    "plan_cartelas": {"modelo": "gemini-3.8-flash", "esfuerzo": "high",
                      "porque": "pocas decisiones por video pero de criterio "
                                "narrativo, y ademas hay que redactar el texto: "
                                "una o dos llamadas, el esfuerzo alto cuesta "
                                "poco y se nota en lo que escribe"},

    "revision_audio": {"modelo": "gemini-3.8-flash", "esfuerzo": "low",
                       "porque": "reescribir 30 palabras con una nota concreta "
                                 "es de los casos que salen igual de bien en "
                                 "bajo, y se llama una vez por bloque"},

    "capturas_agente": {"modelo": "gemini-3.8-flash", "esfuerzo": "high",
                        "porque": "es agentico y toca ficheros: la matriz pide "
                                  "maximo, pero alto da el 90-95 % de esa "
                                  "calidad por una fraccion del gasto"},

    "guia_estilo": {"modelo": "gemini-4-argon", "esfuerzo": "high",
                    "porque": "es la UNICA descripcion en palabras del dibujo de "
                              "este video, y la reciben todas las imagenes que se "
                              "generan: los planos y las hojas de personaje. Era "
                              "el escalon mas barato del Estudio -- 23 segundos, "
                              "193 palabras -- para la pieza que decide como se "
                              "ve el video entero, y se notaba: describia bien el "
                              "trazo y se dejaba lo que mas canta, como se "
                              "resuelve una cara. Es UNA llamada por video"},

    "conservacion": {"modelo": "gemini-3.8-flash", "esfuerzo": "low",
                     "porque": "clasificar si dos frases piden la misma imagen "
                               "es de lo que sale igual de bien en bajo, y el "
                               "analisis tiene que costar menos que rehacer"},

    "repaso": {"modelo": "gemini-4-argon", "esfuerzo": "high",
               "porque": "elegir el cambio mas barato que resuelve una nota "
                         "pide entender el pipeline entero: equivocarse de "
                         "ambito puede regrabar un video para bajar la musica"},

    "corrector_imagen": {"modelo": "gemini-4-argon", "esfuerzo": "high",
                         "porque": "entender una nota como «el mismo ordenador "
                                   "que en la escena anterior» exige mirar las "
                                   "imagenes y elegir bien que se adjunta; una "
                                   "eleccion mala se paga en imagenes"},
}


def _orden_modelo(nombre):
    """Cuanto sube un modelo en la escala. Los ids ya van de menos a mas."""
    norm = ALIAS_MODELOS.get(nombre, nombre)
    return MODELOS.index(norm) if norm in MODELOS else len(MODELOS)


def _orden_esfuerzo(nombre):
    """Lo mismo para el esfuerzo."""
    return ESFUERZOS.index(nombre) if nombre in ESFUERZOS else 0


def por_defecto_de(fase):
    """Lo que corre en esa fase si nadie elige. Hoy, lo mismo en todas."""
    ficha = POR_FASE.get(str(fase))
    modelo, esfuerzo = MODELO_POR_DEFECTO, ESFUERZO_POR_DEFECTO
    porque = f"el canal corre todo en {modelo} con esfuerzo {esfuerzo}"
    if ficha:
        modelo_ficha = normalizar_modelo(ficha["modelo"], estricto=False)
        modelo = max(modelo, modelo_ficha, key=_orden_modelo)
        esfuerzo = max(esfuerzo, ficha["esfuerzo"], key=_orden_esfuerzo)
        if (modelo, esfuerzo) != (modelo_ficha, ficha["esfuerzo"]):
            porque += (f"; para esta fase bastaria con {modelo_ficha}/"
                       f"{ficha['esfuerzo']} ({ficha['porque']})")
        else:
            porque = ficha["porque"]
    return {"modelo": modelo, "esfuerzo": esfuerzo, "porque": porque,
            "recomendado": dict(ficha) if ficha else None}


def catalogo():
    """Lo que la interfaz necesita para pintar el selector, desde una sola fuente."""
    return {
        "modelos": [
            {"id": "gemini-3.8-flash", "nombre": "Gemini 3.8 Flash",
             "nota": "rapido y eficiente; para trabajo estructurado y clasificacion"},
            {"id": "gemini-4-argon", "nombre": "Gemini 4 Argon",
             "nota": "el mas capaz en razonamiento y redaccion; es el que usa el "
                     "Estudio por defecto, para lo que hay que pensar"},
        ],
        "esfuerzos": [
            {"id": "low", "nombre": "Bajo",
             "nota": "contesta casi sin razonar. Medido: 67 s donde sin el flag "
                     "tardaba 606 s"},
            {"id": "medium", "nombre": "Medio",
             "nota": "razona un poco antes de escribir"},
            {"id": "high", "nombre": "Alto",
             "nota": "piensa de verdad; cuesta varias veces mas tiempo"},
            {"id": "xhigh", "nombre": "Muy alto",
             "nota": "para material dificil, con paciencia; es el que usa el "
                     "Estudio por defecto"},
            {"id": "max", "nombre": "Maximo",
             "nota": "todo lo que sabe dar. Puede tardar mas de diez minutos"},
        ],
        "por_defecto": {"modelo": MODELO_POR_DEFECTO,
                        "esfuerzo": ESFUERZO_POR_DEFECTO},
        "factores": {"modelo": dict(FACTOR_MODELO),
                     "esfuerzo": dict(FACTOR_ESFUERZO)},
        "aviso_factores": ("los factores son ordenes de magnitud para estimar "
                           "antes de tener historial, no medidas"),
    }


def clave_ajuste(modelo, esfuerzo):
    """Identificador estable de una combinacion, para agrupar el historico."""
    return f"{normalizar_modelo(modelo, estricto=False)}|" \
           f"{normalizar_esfuerzo(esfuerzo, estricto=False)}"


# -------------------------------------------------------------------- proceso

def localizar(para="el Estudio"):
    """Ruta del ejecutable del CLI (`agy`, con respaldo a `claude`)."""
    for nombre in ("agy.cmd", "agy.exe", "agy", "claude.cmd", "claude.exe", "claude"):
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    raise RuntimeError(
        f"no se encuentra el CLI `agy` en el PATH y {para} lo necesita "
        f"(instalalo o anade ~/.gemini/bin al PATH)")


def entorno(cuenta=None):
    """Entorno del hijo: sin lo que le cambia el razonamiento y sin lo que lo
    sacaria de la suscripcion.
    """
    limpio = dict(os.environ)
    for clave in ENTORNO_FUERA + PAGO_POR_USO:
        limpio.pop(clave, None)
    carpeta = (cuenta or {}).get("config_dir") if isinstance(cuenta, dict) else None
    if carpeta:
        limpio["CLAUDE_CONFIG_DIR"] = carpeta
        limpio["AGY_CONFIG_DIR"] = carpeta
    return limpio


#: Con que se sale del paso cuando no hay ni almacen: el login por defecto del
#: CLI, que es como iba antes de que existiera el almacen. NUNCA una lista
#: vacia: sin cuentas no habria a quien llamar, y un almacen ilegible no puede
#: dejar al Estudio mudo.
POR_DEFECTO = {"etiqueta": "", "config_dir": ""}


def cuentas():
    """Las cuentas del CLI EN ORDEN, la favorita primero. -> list, nunca vacia

    Se lee del almacen en cada llamada y no al importar: se puede entrar con una
    cuenta nueva desde la pantalla con el servicio levantado, y tiene que entrar
    sin reiniciar nada.

    Solo salen las que tienen sesion hecha (`claves.cuentas_cli`): una cuenta a
    medio loguear es una carpeta con un `.credentials.json` a medias, y correr
    con ella falla de una forma que no se parece a nada.
    """
    try:
        from . import claves                              # noqa: PLC0415
    except ImportError:                                   # pasos/ suelto en sys.path
        try:
            import claves                                 # noqa: PLC0415
        except ImportError:
            return [dict(POR_DEFECTO)]
    try:
        lista = claves.cuentas_cli()
    except Exception:                                     # noqa: BLE001
        return [dict(POR_DEFECTO)]
    return list(lista) if lista else [dict(POR_DEFECTO)]


def nombre_de(cuenta, indice=None):
    """Como se llama una cuenta cuando hay que decirlo por escrito."""
    etiqueta = (cuenta or {}).get("etiqueta") or ""
    if etiqueta:
        return etiqueta
    if indice is None:
        return "la cuenta"
    return "la principal" if indice == 0 else f"la {indice + 1}.a"


def matar_arbol(proceso):
    """Mata el proceso Y sus hijos.

    En Windows el CLI se lanza a traves de claude.cmd, que arranca un node
    aparte: matar solo el padre deja el node vivo consumiendo y esperando, que es
    justo lo que se veia colgando de las llamadas que vencian.
    """
    # esto es limpieza: si taskkill no esta, no puede o falla, lo que importa
    # sigue siendo el error que provoco la limpieza, no el de matar
    #
    # EN LINUX NO HAY TASKKILL, y matar solo el node del CLI dejaba sueltos sus
    # hijos (el servidor MCP del asistente, una herramienta a medias). Alli el
    # CLI va en su propio grupo de procesos (`_una_pasada_cruda`) y se mata el
    # grupo entero.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proceso.pid)],
                           capture_output=True, timeout=30, **SIN_VENTANA)
        else:
            os.killpg(proceso.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proceso.kill()
    except Exception:  # noqa: BLE001
        pass


def construir_orden(modelo=MODELO_POR_DEFECTO, esfuerzo=ESFUERZO_POR_DEFECTO,
                    sistema=None, herramientas_vetadas=HERRAMIENTAS_VETADAS,
                    permisos=None, extra=None, herramientas_permitidas=()):
    """La lista de argumentos, en un unico sitio.

    `sistema` tiene que ser UNA sola linea y sin metacaracteres: en Windows el
    CLI se invoca a traves de claude.cmd y un salto de linea dentro de un
    argumento parte el comando.

    `herramientas_permitidas` son las que puede usar SIN PEDIR PERMISO, y hace
    falta para una sola cosa: **`WebFetch` pide permiso POR DOMINIO**. En
    headless eso no es una pregunta, es una negativa -- y no da error: el agente
    anota «bloqueado: sin permiso de acceso a ejemplo.com» y sigue con lo que
    tenga. Medido en la primera tanda de `fuentes` del video largo: de ocho
    fuentes que quiso abrir, SIETE bloqueadas.
    """
    if sistema is not None and ("\n" in sistema or "\r" in sistema):
        raise ValueError("el prompt de sistema tiene que caber en una sola linea: "
                         "en Windows un salto de linea parte la orden de claude.cmd")
    orden = [localizar(), "-p",
             "--model", normalizar_modelo(modelo),
             "--effort", normalizar_esfuerzo(esfuerzo),
             "--output-format", "json",
             "--strict-mcp-config"]
    if herramientas_permitidas:
        orden += ["--allowedTools", ",".join(herramientas_permitidas)]
    if herramientas_vetadas:
        orden += ["--disallowedTools", ",".join(herramientas_vetadas)]
    if sistema:
        orden += ["--append-system-prompt", sistema]
    if permisos:
        orden += ["--permission-mode", permisos]
    orden += list(extra or [])
    return orden


def ejecutar(instruccion, modelo=MODELO_POR_DEFECTO,
             esfuerzo=ESFUERZO_POR_DEFECTO, cwd=None, tiempo_max_s=0,
             base_tiempo_s=420, sistema=None,
             herramientas_vetadas=HERRAMIENTAS_VETADAS, permisos=None,
             extra=None, avance=None, consejos_extra=None, para="la llamada",
             herramientas_permitidas=()):
    """Lanza el CLI en headless y devuelve (texto de la respuesta, sobre JSON).

    La instruccion va SIEMPRE por stdin, nunca como argumento: un transcript de
    20 minutos pasa de 60 KB y no cabe en una linea de comandos de Windows.

    El timeout es finito y mata el arbol de procesos: nunca se deja un paso en
    'ejecutando' para siempre.
    """
    modelo = normalizar_modelo(modelo)
    esfuerzo = normalizar_esfuerzo(esfuerzo)
    lista = cuentas()
    for indice, cuenta in enumerate(lista):
        try:
            return _una_pasada(
                instruccion, modelo, esfuerzo, cwd, tiempo_max_s, base_tiempo_s,
                sistema, herramientas_vetadas, permisos, extra, avance,
                consejos_extra, para, cuenta, herramientas_permitidas)
        except TiempoAgotado:
            # A proposito NO se baja a la siguiente cuenta con un timeout: el
            # techo ya escala con el esfuerzo y volver a intentarlo es volver a
            # esperar media hora para volver a vencer. La clase lo lleva escrito.
            raise
        except RuntimeError as fallo:
            # EL ULTIMO FALLO SUBE TAL CUAL, con su tipo y su texto. No se
            # envuelve en un "las N cuentas fallaron" por dos motivos medidos:
            # `LimiteAgotado` deja de reconocerse como tal (y con el, el aviso
            # en cristiano de que el cupo se renueva el dia X), y la valvula
            # CUPO de las suites casa por TEXTO -- envolverlo pinta FALLA donde
            # tenia que pintar CUPO.
            queda = indice + 1 < len(lista)
            cancelado = avance is not None and getattr(avance, "cancelado", False)
            if not queda or cancelado:
                raise
            print(f"[cli] {nombre_de(cuenta, indice)} fallo "
                  f"({str(fallo)[:120]}); se sigue con "
                  f"{nombre_de(lista[indice + 1], indice + 1)}", flush=True)
    # inalcanzable: `cuentas()` nunca devuelve una lista vacia. Escrito de todas
    # formas porque "inalcanzable" envejece mal.
    raise RuntimeError("no hay ninguna cuenta del CLI de claude configurada")


def _anotar_salud(cuenta, fallo=None, para=""):
    """Apunta en salud_cli como respondio la cuenta. Nunca lanza: es un registro."""
    try:
        try:
            from . import salud_cli                       # noqa: PLC0415
        except ImportError:                               # pasos/ suelto en sys.path
            import salud_cli                              # noqa: PLC0415
        if fallo is None:
            salud_cli.anotar(cuenta, "ok", "", para)
        else:
            estado, mensaje = salud_cli.clasificar(fallo)
            salud_cli.anotar(cuenta, estado, mensaje, para)
    except Exception:                                     # noqa: BLE001
        pass


def _una_pasada(instruccion, modelo, esfuerzo, cwd, tiempo_max_s, base_tiempo_s,
                sistema, herramientas_vetadas, permisos, extra, avance,
                consejos_extra, para, cuenta, herramientas_permitidas=()):
    """UNA llamada al CLI con UNA cuenta, apuntando como respondio.

    Es el UNICO sitio por el que pasa toda llamada al CLI, y por eso la salud de
    la cuenta (pasos/salud_cli.py) se apunta aqui y no en cada paso: un cupo
    agotado a mitad del guion se ve en la burbuja al momento. Una cancelacion
    no se apunta: no dice nada de la cuenta.
    """
    try:
        resultado = _una_pasada_cruda(
            instruccion, modelo, esfuerzo, cwd, tiempo_max_s, base_tiempo_s,
            sistema, herramientas_vetadas, permisos, extra, avance,
            consejos_extra, para, cuenta, herramientas_permitidas)
    except Exception as fallo:
        if not (avance is not None and getattr(avance, "cancelado", False)):
            _anotar_salud(cuenta, fallo, para)
        raise
    _anotar_salud(cuenta, None, para)
    return resultado


def _adaptar_orden_agy(orden, instruccion):
    """Traduce la lista canonica de `construir_orden` a los flags nativos de `agy`.

    `agy` entra en modo print automaticamente al recibir el prompt por stdin
    (sin `-p`, que en `agy` exige argumento propio) y recibe permisos de
    herramientas en `--settings` JSON y reanudacion en `--conversation`.
    """
    import json

    if not orden:
        return orden, instruccion
    binario = os.path.basename(str(orden[0])).lower()
    if not binario.startswith("agy"):
        return orden, instruccion

    nueva = [orden[0]]
    permitidas = []
    vetadas = []
    sistema_extra = []
    i = 1
    while i < len(orden):
        arg = orden[i]
        if arg == "-p":
            i += 1
            continue
        if arg == "--strict-mcp-config":
            nueva.append("--disable-slash-commands")
            i += 1
            continue
        if arg == "--allowedTools" and i + 1 < len(orden):
            permitidas.extend(
                h.strip() for h in str(orden[i + 1]).split(",") if h.strip()
            )
            i += 2
            continue
        if arg == "--disallowedTools" and i + 1 < len(orden):
            vetadas.extend(
                h.strip() for h in str(orden[i + 1]).split(",") if h.strip()
            )
            i += 2
            continue
        if arg == "--append-system-prompt" and i + 1 < len(orden):
            if str(orden[i + 1]).strip():
                sistema_extra.append(str(orden[i + 1]).strip())
            i += 2
            continue
        if arg == "--permission-mode" and i + 1 < len(orden):
            modo_raw = str(orden[i + 1]).strip()
            mapa_modos = {
                "acceptedits": "accept-edits",
                "accept-edits": "accept-edits",
                "auto": "auto",
                "plan": "plan",
            }
            modo_agy = mapa_modos.get(modo_raw.lower())
            if modo_agy:
                nueva += ["--mode", modo_agy]
            i += 2
            continue
        if arg == "--resume" and i + 1 < len(orden):
            nueva += ["--conversation", str(orden[i + 1])]
            i += 2
            continue
        if arg == "--mcp-config" and i + 1 < len(orden):
            i += 2
            continue
        nueva.append(arg)
        i += 1

    if permitidas or vetadas:
        permisos_cfg = {}
        if permitidas:
            permisos_cfg["allow"] = permitidas
        if vetadas:
            permisos_cfg["deny"] = vetadas
        nueva += [
            "--settings",
            json.dumps({"permissions": permisos_cfg}, ensure_ascii=False),
        ]

    texto_entrada = instruccion or ""
    if sistema_extra:
        bloque_sis = "\n".join(sistema_extra)
        texto_entrada = f"<system>\n{bloque_sis}\n</system>\n\n{texto_entrada}"
    return nueva, texto_entrada


def _normalizar_sobre_agy(sobre):
    """Unifica el sobre JSON de `agy` con los campos que esperan los pasos y coste."""
    if not isinstance(sobre, dict):
        return sobre
    if "result" not in sobre and "response" in sobre:
        sobre["result"] = sobre.get("response") or ""
    if not sobre.get("session_id") and sobre.get("conversation_id"):
        sobre["session_id"] = sobre["conversation_id"]
    if "is_error" not in sobre and str(sobre.get("status") or "").upper() == "ERROR":
        sobre["is_error"] = True
    if sobre.get("is_error") and not str(sobre.get("result") or "").strip():
        if sobre.get("error"):
            sobre["result"] = str(sobre["error"])
    if not sobre.get("stop_reason") and sobre.get("finished_reason"):
        sobre["stop_reason"] = sobre["finished_reason"]
    if "duration_ms" not in sobre and isinstance(
        sobre.get("duration_seconds"), (int, float)
    ):
        sobre["duration_ms"] = int(round(float(sobre["duration_seconds"]) * 1000))
    return sobre


def _una_pasada_cruda(instruccion, modelo, esfuerzo, cwd, tiempo_max_s, base_tiempo_s,
                      sistema, herramientas_vetadas, permisos, extra, avance,
                      consejos_extra, para, cuenta, herramientas_permitidas=()):
    """El cuerpo de la llamada, sin el registro de salud."""
    techo = tiempo_max(esfuerzo, base_s=base_tiempo_s, modelo=modelo,
                       pedido_s=tiempo_max_s)
    orden = construir_orden(modelo, esfuerzo, sistema=sistema,
                            herramientas_vetadas=herramientas_vetadas,
                            permisos=permisos, extra=extra,
                            herramientas_permitidas=herramientas_permitidas)
    orden_real, instruccion_real = _adaptar_orden_agy(orden, instruccion)
    try:
        # en Linux, en su propio grupo de procesos: es lo que deja a
        # `matar_arbol` cerrar tambien sus hijos
        grupo = {} if os.name == "nt" else {"start_new_session": True}
        proceso = subprocess.Popen(orden_real, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   cwd=cwd, env=entorno(cuenta), **SIN_VENTANA,
                                   **grupo)
    except OSError as fallo:
        raise RuntimeError(
            f"no se ha podido lanzar el CLI: {fallo}") from fallo

    if avance is not None:
        avance.al_cancelar = lambda: matar_arbol(proceso)

    arranque = time.time()
    try:
        crudo, error_crudo = proceso.communicate(
            (instruccion_real or "").encode("utf-8"), timeout=techo)
    except subprocess.TimeoutExpired as fallo:
        matar_arbol(proceso)
        consejos = list(consejos_extra or [])
        if esfuerzo != "low":
            consejos.insert(0, f"baja 'esfuerzo' de '{esfuerzo}' a 'low': es lo "
                               f"que mas alarga la espera")
        consejos.append("comprueba que 'agy -p hola' contesta a mano desde una consola")
        consejos.append(f"sube 'tiempo_max_s' por encima de {techo} si el trabajo "
                        f"es largo de verdad")
        raise TiempoAgotado(
            f"el CLI no ha respondido en {techo} s y se ha cortado "
            f"{para} (se ha matado el proceso y sus hijos). Que probar: "
            + "; ".join(consejos) + ".") from fallo

    segundos = time.time() - arranque
    if avance is not None and getattr(avance, "cancelado", False):
        raise RuntimeError(f"{para} se cancelo mientras esperaba al CLI")

    salida = crudo.decode("utf-8", errors="replace")
    error = error_crudo.decode("utf-8", errors="replace").strip()
    import json
    if proceso.returncode != 0:
        # El CLI sale con codigo 1 y AUN ASI escribe su sobre JSON: el motivo
        # de verdad esta en `result` (o `error`/`response` en `agy`).
        motivo = ""
        try:
            sobre_err = _normalizar_sobre_agy(json.loads(salida))
            motivo = str(sobre_err.get("result") or sobre_err.get("error") or "").strip()
        except (ValueError, AttributeError):
            motivo = ""
        limite = limite_de(motivo or error)
        if limite:
            raise LimiteAgotado(
                f"se ha agotado el cupo de la suscripcion del CLI y no se "
                f"puede hacer {para}: {limite}")
        raise RuntimeError(
            f"el CLI fallo (codigo {proceso.returncode}) despues de "
            f"{int(segundos)} s: {(motivo or error or salida)[:600] or 'sin mensaje'}")
    try:
        sobre = _normalizar_sobre_agy(json.loads(salida))
    except ValueError as fallo:
        raise RuntimeError(
            f"el CLI no devolvio JSON: {salida[:600] or '(vacio)'}") from fallo
    if not isinstance(sobre, dict):
        raise RuntimeError(f"respuesta inesperada del CLI: {salida[:600]}")
    if sobre.get("is_error"):
        limite = limite_de(sobre.get("result"))
        if limite:
            raise LimiteAgotado(
                f"se ha agotado el cupo de la suscripcion del CLI y no se "
                f"puede hacer {para}: {limite}")
        raise RuntimeError(
            f"el CLI devolvio error: {str(sobre.get('result'))[:600]}")
    texto = sobre.get("result")
    if not isinstance(texto, str) or not texto.strip():
        raise RuntimeError("el CLI devolvio una respuesta vacia")

    # El sobre del CLI no dice con que esfuerzo se le hablo, y el esfuerzo es la
    # variable que mas mueve el tiempo: si no se estampa aqui, el medidor de
    # coste y el historico se quedan ciegos justo donde importa.
    sobre["_ajuste"] = {"modelo": modelo, "esfuerzo": esfuerzo,
                        "segundos": round(segundos, 2), "tiempo_max_s": techo,
                        "cuenta": (cuenta or {}).get("etiqueta") or "principal"}
    sobre.setdefault("model", modelo)
    return texto, sobre


# =========================================================================
# PEDIR UNA RESPUESTA LARGA: QUE LA ESCRIBA, NO QUE LA DEVUELVA
#
# El limite que obligaba a trocear las listas largas en tandas (`POR_TANDA`) es
# el largo del MENSAJE, no el del contexto ni el del modelo. Medido el
# 22-08-2026 con el mismo encargo por los dos caminos, en sonnet/medium:
#
#     120 items de 60 palabras   en la RESPUESTA  120/120  16.941 car.   88 s
#                                a FICHERO        120/120  14.738 car.   78 s
#     300 items de 60 palabras   en la RESPUESTA   NO PARSEA   790 car.  623 s
#                                a FICHERO        300/300  95.370 car.  578 s
#
# A 300 la respuesta vuelve rota despues de diez minutos y el fichero sale
# entero en menos tiempo. Es lo mismo que hace un agente cuando escribe miles de
# lineas de codigo: no las mete en su mensaje final, las escribe con Write en
# varias pasadas y contesta «hecho».
#
# LO QUE OBLIGA A CUIDAR, porque esto le da la herramienta Write a un agente:
#   - corre en una carpeta TEMPORAL de un solo uso, creada aqui y borrada aqui.
#     Nunca el proyecto ni el repo, que es donde corren las demas fases;
#   - `Bash` se veta siempre, pase lo que pase: no hace falta para escribir un
#     JSON y es la herramienta con la que se sale de una carpeta;
#   - si el fichero no aparece, el error dice QUE ficheros si dejo. Sin eso,
#     «no escribio nada» no distingue entre equivocarse de nombre, de carpeta o
#     no llegar a escribir, y son tres arreglos distintos.
#
# 

#: Como se llama el fichero que se le pide. Uno solo para todas las fases: si
#: cada una eligiera el suyo, el dia que una falle habria que ir a mirar cual
#: era el nombre en esa.
FICHERO_SALIDA = "salida.json"

CIERRE = """

=====================================================================
COMO ENTREGARLO
=====================================================================

ESCRIBE EL RESULTADO EN EL FICHERO `{fichero}` de este directorio, con la forma
exacta que se pide arriba. Escribelo en varias pasadas si te resulta mas comodo
(Write y despues Edit para ir anadiendo); lo unico que importa es que al
terminar sea un JSON valido y completo.

Se escribe a fichero y NO se devuelve en el mensaje a proposito: una lista larga
en la respuesta se corta a la mitad y se pierde entera.

Cuando este escrito, responde solo con: LISTO
"""


def escribiendo(instruccion, modelo=MODELO_POR_DEFECTO,
                esfuerzo=ESFUERZO_POR_DEFECTO, tiempo_max_s=0,
                base_tiempo_s=600, herramientas_vetadas=(), avance=None,
                para="la llamada", que="la respuesta", carpetas=(),
                herramientas_permitidas=()):
    """Como `ejecutar`, pero el agente ESCRIBE su JSON y aqui se lee. -> dict.

    La instruccion NO tiene que decir como entregarlo: se le anade el cierre
    (ver CIERRE), asi que todas las fases piden la entrega igual y el dia que
    esto cambie, cambia en un sitio.

    `herramientas_vetadas` se respeta y se le suma `Bash` siempre.

    `carpetas` son rutas que el agente puede LEER ademas de la suya (--add-dir).
    Sin esto, Read de una ruta fuera del directorio de trabajo pide permiso y en
    headless eso es colgarse. Se pasan las carpetas que EXISTEN: una ruta
    inventada aborta el CLI entero antes de arrancar.
    """
    import json                                                # noqa: PLC0415
    import shutil                                              # noqa: PLC0415
    import tempfile                                            # noqa: PLC0415

    vetadas = tuple(dict.fromkeys(tuple(herramientas_vetadas or ()) + ("Bash",)))
    for imprescindible in ("Write", "Edit", "Read"):
        if imprescindible in vetadas:
            raise ValueError(
                f"'{imprescindible}' no se puede vetar aqui: es como el agente "
                f"entrega la respuesta (ver cli_claude.escribiendo)")
    extra = ["--no-session-persistence"]
    for ruta in dict.fromkeys(str(c) for c in (carpetas or ()) if c):
        if os.path.isdir(ruta):
            extra += ["--add-dir", os.path.abspath(ruta)]
    carpeta = tempfile.mkdtemp(prefix="cli_salida_")
    try:
        ejecutar(instruccion + CIERRE.format(fichero=FICHERO_SALIDA),
                 modelo=modelo, esfuerzo=esfuerzo, cwd=carpeta,
                 tiempo_max_s=tiempo_max_s, base_tiempo_s=base_tiempo_s,
                 herramientas_vetadas=vetadas, permisos="acceptEdits",
                 extra=extra, avance=avance, para=para,
                 herramientas_permitidas=herramientas_permitidas)
        ruta = os.path.join(carpeta, FICHERO_SALIDA)
        if not os.path.exists(ruta):
            hay = sorted(os.listdir(carpeta))[:8]
            raise RuntimeError(
                f"el agente de {para} no ha escrito {FICHERO_SALIDA}"
                + (f" (dejo: {', '.join(hay)})" if hay else " (no dejo nada)"))
        with open(ruta, "r", encoding="utf-8-sig") as fh:
            crudo = fh.read()
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)
    try:
        return json.loads(crudo)
    except ValueError:
        # con vallas de markdown o una frase delante: se rescata igual que se
        # rescata la respuesta de cualquier otro agente
        try:
            from . import comun                                # noqa: PLC0415
        except ImportError:                          # pasos/ suelto en sys.path
            import comun                                       # noqa: PLC0415
        return comun.extraer_json(crudo, que)
