import shutil
import os
import sys
import time
import json
import threading
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler

from PySide6.QtWidgets import QApplication, QFileDialog, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter
from PySide6.QtCore import QUrl, QTimer, Qt
from PySide6.QtWebEngineWidgets import QWebEngineView

# ──────────────────────────────────────────────
#  CONFIG PERSISTENTE
# ──────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH   = os.path.join(BASE_DIR, "config.json")
HTML_CARPETA  = os.path.join(BASE_DIR, "ui", "popup_carpeta.html")
HTML_SYNC     = os.path.join(BASE_DIR, "ui", "popup_sync.html")
HTML_SETTINGS = os.path.join(BASE_DIR, "ui", "popup_settings.html")

CONFIG_DEFAULT = {
    "carpeta_descargas":  os.path.join(os.path.expanduser("~"), "Downloads"),
    "carpeta_biblioteca": os.path.join(os.path.expanduser("~"), "Music", "Biblioteca"),
    "carpeta_pd":         "E:\\",
    "arranque_automatico": False,
    "navegador": "edge"
}

def cargar_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                for k, v in CONFIG_DEFAULT.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
        except Exception:
            pass
    return CONFIG_DEFAULT.copy()

def guardar_config(cfg):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

_config = cargar_config()

def get_descargas():           return _config["carpeta_descargas"]
def get_biblioteca():          return _config["carpeta_biblioteca"]
def get_pd():                  return _config["carpeta_pd"]
def get_arranque_automatico(): return _config.get("arranque_automatico", False)
def get_navegador():           return _config.get("navegador", "edge")

# ──────────────────────────────────────────────
#  ARRANQUE AUTOMÁTICO — S28
# ──────────────────────────────────────────────
REGISTRO_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRO_NAME = "Sounduct"

def set_arranque_automatico(activar):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRO_KEY, 0, winreg.KEY_SET_VALUE)
        if activar:
            exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
            winreg.SetValueEx(key, REGISTRO_NAME, 0, winreg.REG_SZ, f'"{exe}"')
            print(f"[ARRANQUE] Registrado: {exe}")
        else:
            try:
                winreg.DeleteValue(key, REGISTRO_NAME)
                print("[ARRANQUE] Entrada eliminada.")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[ERROR] Arranque automatico: {e}")

# ──────────────────────────────────────────────
#  CONSTANTES
# ──────────────────────────────────────────────
AUDIO_FORMATOS = ('.mp3', '.wav', '.aiff')
CHECK_PD_CADA  = 10

# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────
def pd_conectado():
    return os.path.isdir(get_pd())

def es_audio(path):
    return os.path.splitext(path)[1].lower() in AUDIO_FORMATOS

def relativo(path):
    return os.path.relpath(path, get_biblioteca())

def esperar_archivo_completo(path, espera=3, intentos=60):
    tamano_anterior = -1
    for _ in range(intentos):
        try:
            tamano_actual = os.path.getsize(path)
        except Exception:
            return False
        if tamano_actual == tamano_anterior and tamano_actual > 0:
            return True
        tamano_anterior = tamano_actual
        time.sleep(espera)
    return False

def listar_carpetas():
    carpetas = ["/ Raiz"]
    try:
        for entry in sorted(os.scandir(get_biblioteca()), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith('.'):
                carpetas.append(entry.name)
    except Exception as e:
        print(f"[ERROR] Listando carpetas: {e}")
    return carpetas

def puerto_libre():
    """Encuentra un puerto TCP disponible."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

# ──────────────────────────────────────────────
#  DIFF
# ──────────────────────────────────────────────
def calcular_diff():
    copiar, borrar = [], []
    casa = {}
    for raiz, _, archivos in os.walk(get_biblioteca()):
        for nombre in archivos:
            path = os.path.join(raiz, nombre)
            rel  = os.path.relpath(path, get_biblioteca())
            try: casa[rel] = os.path.getsize(path)
            except: pass
    pd_files = {}
    for raiz, _, archivos in os.walk(get_pd()):
        if 'System Volume Information' in raiz:
            continue
        for nombre in archivos:
            path = os.path.join(raiz, nombre)
            rel  = os.path.relpath(path, get_pd())
            try: pd_files[rel] = os.path.getsize(path)
            except: pass

    # B02: detectar renames de carpeta antes del diff normal
    # Si una carpeta desapareció del PD y apareció una nueva con los mismos archivos
    # (mismo nombre de archivo y mismo tamaño), es un rename — no borrar+copiar
    renames = _detectar_renames(casa, pd_files)

    for rel, size in casa.items():
        if rel not in pd_files or pd_files[rel] != size:
            # Verificar que no es parte de un rename ya detectado
            if not any(rel == dst for _, dst in renames):
                copiar.append(rel)
    for rel in pd_files:
        if rel not in casa:
            if not any(rel == src for src, _ in renames):
                borrar.append(rel)
    return copiar, borrar

def _detectar_renames(casa, pd_files):
    """B02: detecta carpetas renombradas comparando archivos por nombre+tamaño.
    Devuelve lista de (rel_pd_viejo, rel_biblioteca_nuevo)."""
    import os.path as osp

    # Agrupar archivos del PD por carpeta
    pd_por_carpeta = {}
    for rel, size in pd_files.items():
        carpeta = osp.dirname(rel)
        pd_por_carpeta.setdefault(carpeta, {})[osp.basename(rel)] = size

    # Agrupar archivos de la biblioteca por carpeta
    bib_por_carpeta = {}
    for rel, size in casa.items():
        carpeta = osp.dirname(rel)
        bib_por_carpeta.setdefault(carpeta, {})[osp.basename(rel)] = size

    renames = []
    carpetas_pd  = set(pd_por_carpeta.keys())
    carpetas_bib = set(bib_por_carpeta.keys())
    desaparecidas = carpetas_pd - carpetas_bib
    nuevas        = carpetas_bib - carpetas_pd

    for vieja in desaparecidas:
        for nueva in nuevas:
            archivos_viejos = pd_por_carpeta[vieja]
            archivos_nuevos = bib_por_carpeta[nueva]
            # Si tienen los mismos archivos (nombre y tamaño), es un rename
            if archivos_viejos == archivos_nuevos and len(archivos_viejos) > 0:
                for nombre in archivos_viejos:
                    renames.append((
                        osp.join(vieja, nombre),   # ruta vieja en PD
                        osp.join(nueva, nombre)    # ruta nueva en biblioteca
                    ))
                break
    return renames

def _limpiar_carpetas_vacias(dirpath):
    while True:
        try:
            if os.path.isdir(dirpath) and not os.listdir(dirpath):
                if os.path.abspath(dirpath) == os.path.abspath(get_pd()): break
                os.rmdir(dirpath)
                dirpath = os.path.dirname(dirpath)
            else: break
        except: break

def pd_copy(rel):
    src = os.path.join(get_biblioteca(), rel)
    dst = os.path.join(get_pd(), rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[PD] Copiado: {rel}")

def pd_delete(rel):
    dst = os.path.join(get_pd(), rel)
    if os.path.isfile(dst): os.remove(dst)
    elif os.path.isdir(dst): shutil.rmtree(dst)
    _limpiar_carpetas_vacias(os.path.dirname(dst))

def pd_move(rel_src, rel_dst):
    src = os.path.join(get_pd(), rel_src)
    dst = os.path.join(get_pd(), rel_dst)
    if os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        _limpiar_carpetas_vacias(os.path.dirname(src))

def pd_rename_dir(rel_src, rel_dst):
    src = os.path.join(get_pd(), rel_src)
    dst = os.path.join(get_pd(), rel_dst)
    if os.path.isdir(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)

# ──────────────────────────────────────────────
#  ESTADO COMPARTIDO — popups activos
# ──────────────────────────────────────────────
# Épica 10: bandeja de tracks pendientes
_bandeja             = []   # lista de {nombre, path, callback}
_bandeja_lock        = threading.Lock()
_popup_auto_abierto  = False   # True cuando hay un popup de track auto abierto
_popup_manual_abierto = False  # True cuando está abierto el popup manual

_estado = {
    "carpeta": {
        "nombre":    None,
        "path":      None,
        "resultado": None,
        "evento":    None,
        "modo":      "auto",
        "posicion":  1,     # B03: posición del track actual en la sesión
        "total":     1,     # B03: total de tracks en la sesión
        "descarga": {
            "activa":     False,
            "progreso":   [],
            "completado": False,
            "error":      None,
            "archivo":    None,
        }
    },
    "sync": {
        "copiar":      [],
        "borrar":      [],
        "progreso":    [],   # lista de {pct, nombre}
        "aplicando":   False,
        "completado":  False,
    },
    "settings": {
        "ruta_elegida": None,
        "campo_elegido": None,
        "evento_ruta": None,
    }
}

# ──────────────────────────────────────────────
#  SERVIDOR HTTP LOCAL
# ──────────────────────────────────────────────
_PUERTO = None

class SoundductHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silenciar logs HTTP en consola

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/ping":
            self._json_response({"ok": True})

        # ── SETTINGS ──
        elif path == "/settings/config":
            self._json_response({
                "descargas":           get_descargas(),
                "biblioteca":          get_biblioteca(),
                "pd":                  get_pd(),
                "arranque_automatico": get_arranque_automatico(),
                "navegador":           get_navegador()
            })

        elif path == "/settings/ruta":
            # Espera hasta que Python haya mandado la ruta via elegirCarpeta
            ev = _estado["settings"]["evento_ruta"]
            if ev:
                ev.wait(timeout=60)
            self._json_response({
                "campo": _estado["settings"]["campo_elegido"] or "",
                "ruta":  _estado["settings"]["ruta_elegida"] or ""
            })

        # ── CARPETA ──
        elif path == "/carpeta/modo":
            # S40: el HTML necesita saber en qué modo está
            self._json_response({"modo": _estado["carpeta"]["modo"]})

        elif path == "/carpeta/nombre":
            self._json_response({"nombre": _estado["carpeta"]["nombre"] or ""})

        elif path == "/carpeta/carpetas":
            self._json_response({"carpetas": listar_carpetas()})

        elif path == "/carpeta/audio":
            # S31/S51: sirve audio del track actual (idx=-1) o de uno de la bandeja (idx>=0)
            qs  = parse_qs(urlparse(self.path).query)
            idx = int(qs.get("idx", ["-1"])[0])
            if idx == -1:
                audio_path = _estado["carpeta"].get("path")
            else:
                with _bandeja_lock:
                    if 0 <= idx < len(_bandeja):
                        audio_path = _bandeja[idx]["path"]
                    else:
                        audio_path = None
            if not audio_path or not os.path.exists(audio_path):
                self.send_response(404)
                self.end_headers()
                return
            ext = os.path.splitext(audio_path)[1].lower()

            # QWebEngineView/Chromium no soporta AIFF — transcodificar a WAV con ffmpeg al vuelo
            if ext in ('.aiff', '.aif'):
                import subprocess
                try:
                    proc = subprocess.Popen(
                        ["ffmpeg", "-i", audio_path, "-f", "wav", "-acodec", "pcm_s16le", "pipe:1", "-loglevel", "quiet"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                    )
                    audio_data = proc.stdout.read()
                    proc.wait()
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(audio_data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(audio_data)
                except Exception as e:
                    print(f"[ERROR] ffmpeg transcodificando AIFF: {e}")
                    self.send_response(500)
                    self.end_headers()
                return

            mime = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav'}.get(ext, 'audio/mpeg')
            size = os.path.getsize(audio_path)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(audio_path, 'rb') as f:
                self.wfile.write(f.read())
            return

        elif path == "/carpeta/progreso_descarga":
            # S42: long-poll para progreso de descarga yt-dlp
            d = _estado["carpeta"]["descarga"]
            inicio = time.time()
            while not d["progreso"] and not d["completado"] and not d["error"]:
                if time.time() - inicio > 30:
                    break
                time.sleep(0.2)
            prog = d["progreso"].copy()
            d["progreso"].clear()
            self._json_response({
                "items":      prog,
                "completado": d["completado"],
                "error":      d["error"],
                "archivo":    d["archivo"]
            })
            return

        elif path == "/carpeta/bandeja":
            # S50: devuelve lista de tracks en bandeja + track actual + posición
            with _bandeja_lock:
                pendientes = [{"nombre": t["nombre"], "idx": i} for i, t in enumerate(_bandeja)]
            self._json_response({
                "actual": {
                    "nombre": _estado["carpeta"]["nombre"] or "",
                    "idx":    -1
                },
                "pendientes": pendientes,
                "posicion":   _estado["carpeta"]["posicion"],
                "total":      _estado["carpeta"]["total"]
            })

        elif path == "/carpeta/esperar":
            # Bloquea hasta que el usuario confirme o cancele
            ev = _estado["carpeta"]["evento"]
            if ev:
                ev.wait(timeout=300)
            self._json_response({"resultado": _estado["carpeta"]["resultado"]})

        # ── SYNC ──
        elif path == "/sync/datos":
            s = _estado["sync"]
            self._json_response({
                "copiar":      len(s["copiar"]),
                "borrar":      len(s["borrar"]),
                "lista_borrar": s["borrar"]
            })

        elif path == "/sync/progreso":
            # Long-poll: espera hasta que haya nuevo progreso
            s = _estado["sync"]
            inicio = time.time()
            while not s["progreso"] and not s["completado"]:
                if time.time() - inicio > 30:
                    break
                time.sleep(0.2)
            prog = s["progreso"].copy()
            s["progreso"].clear()
            self._json_response({
                "items":      prog,
                "completado": s["completado"]
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_body()

        # ── SETTINGS ──
        if path == "/settings/guardar":
            global _config
            _config["carpeta_descargas"]   = data.get("descargas",           get_descargas())
            _config["carpeta_biblioteca"]  = data.get("biblioteca",          get_biblioteca())
            _config["carpeta_pd"]          = data.get("pd",                  get_pd())
            _config["arranque_automatico"] = data.get("arranque_automatico", get_arranque_automatico())
            _config["navegador"]           = data.get("navegador",           get_navegador())
            guardar_config(_config)
            set_arranque_automatico(_config["arranque_automatico"])
            # S34: crear carpetas al guardar (no al arrancar watchers)
            try:
                os.makedirs(get_descargas(),  exist_ok=True)
                os.makedirs(get_biblioteca(), exist_ok=True)
            except Exception as e:
                print(f"[WARN] No se pudo crear carpeta: {e}")
            print(f"[CONFIG] Guardada: {_config}")
            encolar(_on_settings_guardado)
            self._json_response({"ok": True})

        elif path == "/settings/cancelar":
            encolar(_on_settings_cancelado)
            self._json_response({"ok": True})

        elif path == "/settings/elegir":
            campo  = data.get("campo", "")
            titulo = data.get("titulo", "Seleccionar carpeta")
            _estado["settings"]["campo_elegido"] = campo
            _estado["settings"]["ruta_elegida"]  = None
            ev = threading.Event()
            _estado["settings"]["evento_ruta"] = ev
            encolar(lambda c=campo, t=titulo, e=ev: _abrir_dialogo_carpeta(c, t, e))
            self._json_response({"ok": True})

        # ── CARPETA ──
        elif path == "/carpeta/descargar":
            # S40/S41/S43/S44/S46: descarga con yt-dlp directo a la carpeta elegida
            url     = data.get("url", "")
            carpeta = data.get("carpeta", "")
            formato = data.get("formato", "mp3")   # S46: mp3/wav/aiff
            if not url:
                self._json_response({"ok": False, "error": "URL vacía"})
                return
            threading.Thread(
                target=_descargar_url,
                args=(url, carpeta, formato),
                daemon=True
            ).start()
            self._json_response({"ok": True})

        elif path == "/carpeta/confirmar":
            carpeta = data.get("carpeta", "")
            idx     = data.get("idx", -1)

            if idx == -1:
                # Confirmar track actual (comportamiento original)
                _estado["carpeta"]["resultado"] = carpeta
                ev = _estado["carpeta"]["evento"]
                if ev: ev.set()
            else:
                # S52: confirmar track de la bandeja por índice
                with _bandeja_lock:
                    if 0 <= idx < len(_bandeja):
                        track = _bandeja.pop(idx)
                    else:
                        track = None
                if track:
                    actualizar_tray_bandeja()
                    threading.Thread(
                        target=_mover_track,
                        args=(track["path"], track["nombre"], carpeta, track["callback"]),
                        daemon=True
                    ).start()

            self._json_response({"ok": True})

        elif path == "/carpeta/cancelar":
            _estado["carpeta"]["resultado"] = None
            ev = _estado["carpeta"]["evento"]
            if ev: ev.set()
            self._json_response({"ok": True})

        # ── SYNC ──
        elif path == "/sync/aplicar":
            threading.Thread(target=_aplicar_sync, daemon=True).start()
            self._json_response({"ok": True})

        elif path == "/sync/cancelar":
            encolar(lambda: _cerrar_popup("sync"))
            self._json_response({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()


def iniciar_servidor():
    global _PUERTO
    _PUERTO = puerto_libre()
    server  = HTTPServer(("127.0.0.1", _PUERTO), SoundductHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[HTTP] Servidor en puerto {_PUERTO}")
    return server

# ──────────────────────────────────────────────
#  COLA DE TAREAS → hilo principal Qt
# ──────────────────────────────────────────────
_cola = []
_cola_lock = threading.Lock()

def encolar(fn):
    with _cola_lock:
        _cola.append(fn)

def procesar_cola():
    with _cola_lock:
        tareas = _cola[:]
        _cola.clear()
    for fn in tareas:
        fn()

# ──────────────────────────────────────────────
#  VENTANA BASE — sin QWebChannel
# ──────────────────────────────────────────────
_ventanas = {}  # nombre → view

from PySide6.QtWebEngineCore import QWebEngineSettings

def crear_ventana(nombre, ancho, alto, html_path, frameless=True):
    view = QWebEngineView()
    view.setFixedSize(ancho, alto)
    flags = Qt.WindowType.Window
    if frameless:
        flags |= Qt.WindowType.FramelessWindowHint
    view.setWindowFlags(flags)

    # Habilitar acceso a localhost desde file://
    settings = view.page().settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

    # Pasar puerto en la URL para que el HTML sepa dónde hacer fetch
    url = QUrl.fromLocalFile(html_path)
    url.setQuery(f"port={_PUERTO}")
    view.load(url)
    view.show()
    view.raise_()
    view.activateWindow()

    _ventanas[nombre] = view
    return view

def _cerrar_popup(nombre):
    view = _ventanas.pop(nombre, None)
    if view:
        view.close()

# ──────────────────────────────────────────────
#  POPUP SETTINGS
# ──────────────────────────────────────────────
_settings_callback = None

def _abrir_dialogo_carpeta(campo, titulo, evento):
    """Abre QFileDialog en el hilo principal y guarda la ruta."""
    dlg = QFileDialog(None, titulo, os.path.expanduser("~"))
    dlg.setFileMode(QFileDialog.FileMode.Directory)
    dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.raise_()
    dlg.activateWindow()
    if dlg.exec():
        ruta = dlg.selectedFiles()[0]
    else:
        ruta = ""
    _estado["settings"]["ruta_elegida"]  = ruta
    _estado["settings"]["campo_elegido"] = campo
    evento.set()

def _on_settings_guardado():
    _cerrar_popup("settings")
    if _settings_callback:
        _settings_callback(True)

def _on_settings_cancelado():
    _cerrar_popup("settings")
    if _settings_callback:
        _settings_callback(False)

def abrir_popup_settings(callback=None):
    global _settings_callback
    _settings_callback = callback
    crear_ventana("settings", 600, 420, HTML_SETTINGS, frameless=True)

# ──────────────────────────────────────────────
#  POPUP CARPETA
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
#  DESCARGA DESDE URL — Épica 9
# ──────────────────────────────────────────────
def _get_cookies_args():
    """S55/S56: devuelve args de cookies para yt-dlp según el navegador configurado.
    Chrome requiere copia manual del SQLite porque lo bloquea mientras está abierto.
    Edge, Firefox y Brave usan --cookies-from-browser directamente."""
    navegador = get_navegador()
    if navegador == "ninguno":
        print("[COOKIES] Sin cookies configuradas")
        return [], None

    if navegador == "chrome":
        import shutil as _shutil, tempfile
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
        cookies_path = None
        for perfil in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
            candidate = os.path.join(base, perfil, "Cookies")
            if os.path.exists(candidate):
                cookies_path = candidate
                break
        if not cookies_path:
            print("[COOKIES] No se encontro base de cookies de Chrome, intentando --cookies-from-browser")
            return ["--cookies-from-browser", "chrome"], None
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            _shutil.copy2(cookies_path, tmp.name)
            print(f"[COOKIES] Chrome: cookies copiadas a temp")
            return ["--cookies", tmp.name], tmp.name
        except Exception as e:
            print(f"[COOKIES] Chrome: no se pudo copiar ({e}), intentando --cookies-from-browser")
            return ["--cookies-from-browser", "chrome"], None

    # Edge, Firefox, Brave: --cookies-from-browser funciona sin bloqueo
    print(f"[COOKIES] Usando cookies de: {navegador}")
    return ["--cookies-from-browser", navegador], None


def _limpiar_url(url):
    """Limpia parámetros de playlist de URLs de YouTube."""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        # Mantener solo 'v' (video ID) y descartar list, index, pp, etc.
        limpio = {k: v for k, v in params.items() if k == 'v'}
        nueva_query = urlencode(limpio, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, nueva_query, parsed.fragment))
    except:
        return url


def _descargar_url(url, carpeta, formato="mp3"):
    """S40/S41/S46: descarga con yt-dlp en el formato elegido directo a la carpeta."""
    import subprocess
    url = _limpiar_url(url)
    d = _estado["carpeta"]["descarga"]
    d["activa"]     = True
    d["completado"] = False
    d["error"]      = None
    d["archivo"]    = None
    d["progreso"]   = []

    # Carpeta destino
    if carpeta in ("/ Raiz", "/ Raíz", ""):
        destino = get_biblioteca()
    else:
        destino = os.path.join(get_biblioteca(), carpeta)
    os.makedirs(destino, exist_ok=True)

    cookies_args, cookies_file = _get_cookies_args()

    # S46: yt-dlp no acepta "aiff" en --audio-format, hay que extraer como wav y remuxear
    if formato == "aiff":
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--extract-audio",
            "--audio-format", "wav",
            "--remux-video", "aiff",
            "--audio-quality", "0",
            "--no-playlist",
            "--output", os.path.join(destino, "%(title)s.%(ext)s"),
            "--newline",
        ] + cookies_args + [url]
    else:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--extract-audio",
            "--audio-format", formato,
            "--audio-quality", "0",
            "--no-playlist",
            "--output", os.path.join(destino, "%(title)s.%(ext)s"),
            "--newline",
        ] + cookies_args + [url]

    print(f"[DESCARGA] {url} → {destino}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        archivo_final = None
        for line in proc.stdout:
            line = line.strip()
            print(f"[yt-dlp] {line}")
            # Parsear progreso
            if "[download]" in line and "%" in line:
                try:
                    pct_str = line.split("%")[0].split()[-1]
                    pct = float(pct_str)
                    d["progreso"].append({"pct": pct, "texto": line})
                except: pass
            # Detectar archivo final
            if "[ExtractAudio] Destination:" in line:
                archivo_final = line.split("Destination:")[-1].strip()
            elif "Destination:" in line:
                candidate = line.split("Destination:")[-1].strip()
                if any(candidate.lower().endswith(ext) for ext in ('.mp3', '.wav', '.aiff', '.aif', '.m4a')):
                    archivo_final = candidate

        proc.wait()
        if proc.returncode == 0:
            d["completado"] = True
            d["archivo"]    = archivo_final
            d["progreso"].append({"pct": 100, "texto": "Descarga completada"})
            print(f"[DESCARGA] Completada: {archivo_final}")
            # S44: copiar al PD si está conectado
            if archivo_final and pd_conectado():
                try:
                    rel = os.path.relpath(archivo_final, get_biblioteca())
                    pd_copy(rel)
                    encolar(lambda n=os.path.basename(archivo_final): notificar("Sounduct", f"{n} copiado al PD"))
                except Exception as e:
                    print(f"[ERROR] Copiando al PD: {e}")
        else:
            d["error"] = "Error al descargar. Verificá la URL."
            print(f"[ERROR] yt-dlp returncode: {proc.returncode}")
    except FileNotFoundError:
        d["error"] = "yt-dlp no encontrado. Instala con: pip install yt-dlp"
        print("[ERROR] yt-dlp no instalado")
    except Exception as e:
        d["error"] = str(e)
        print(f"[ERROR] Descarga: {e}")
    finally:
        d["activa"] = False
        if cookies_file and os.path.exists(cookies_file):
            try: os.remove(cookies_file)
            except: pass


def _abrir_siguiente_de_bandeja():
    """S50/S51: abre el siguiente track de la bandeja si hay alguno."""
    global _popup_auto_abierto
    with _bandeja_lock:
        if not _bandeja:
            _popup_auto_abierto = False
            actualizar_tray_bandeja()
            return
        siguiente = _bandeja.pop(0)

    actualizar_tray_bandeja()
    _abrir_popup_auto(siguiente["nombre"], siguiente["path"], siguiente["callback"])


def _mover_track(path, nombre, carpeta, callback):
    """S52: mueve un track de la bandeja a su carpeta destino."""
    import shutil as _shutil
    if carpeta in ("/ Raiz", "/ Raíz", ""):
        destino = os.path.join(get_biblioteca(), nombre)
        rel = nombre
    else:
        destino = os.path.join(get_biblioteca(), carpeta, nombre)
        rel = os.path.join(carpeta, nombre)

    try:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        if os.path.abspath(path) != os.path.abspath(destino):
            _shutil.move(path, destino)
            print(f"[LOCAL] Movido: {rel}")
        if pd_conectado():
            pd_copy(rel)
            encolar(lambda n=nombre: notificar("Sounduct", f"{n} copiado al PD"))
    except Exception as e:
        print(f"[ERROR] _mover_track: {e}")

    if callback:
        callback(carpeta)


def _abrir_popup_auto(nombre_archivo, path_archivo, callback, posicion=1, total=None):
    """S50: abre el popup con bandeja integrada."""
    global _popup_auto_abierto
    _popup_auto_abierto = True

    ev = threading.Event()
    _estado["carpeta"]["nombre"]          = nombre_archivo
    _estado["carpeta"]["path"]            = path_archivo
    _estado["carpeta"]["resultado"]       = None
    _estado["carpeta"]["evento"]          = ev
    _estado["carpeta"]["modo"]            = "auto"
    _estado["carpeta"]["callback_actual"] = callback
    # B03: calcular total si no se pasa
    with _bandeja_lock:
        n_bandeja = len(_bandeja)
    _estado["carpeta"]["posicion"] = posicion
    _estado["carpeta"]["total"]    = total if total is not None else (posicion + n_bandeja)

    crear_ventana("carpeta", 680, 580, HTML_CARPETA)

    def esperar():
        global _popup_auto_abierto

        while True:
            ev_actual = _estado["carpeta"]["evento"]
            ev_actual.wait(timeout=300)
            carpeta = _estado["carpeta"]["resultado"]
            nombre  = _estado["carpeta"]["nombre"]
            path    = _estado["carpeta"]["path"]
            cb      = _estado["carpeta"]["callback_actual"]

            # Mover el track actual
            threading.Thread(
                target=_mover_track,
                args=(path, nombre, carpeta if carpeta is not None else "", cb),
                daemon=True
            ).start()

            # Ver si quedan tracks en bandeja
            with _bandeja_lock:
                quedan = len(_bandeja)

            if quedan > 0:
                with _bandeja_lock:
                    siguiente = _bandeja.pop(0)
                actualizar_tray_bandeja()

                # Actualizar estado con el siguiente track
                ev2 = threading.Event()
                pos_actual = _estado["carpeta"]["posicion"]
                tot_actual = _estado["carpeta"]["total"]
                _estado["carpeta"]["nombre"]          = siguiente["nombre"]
                _estado["carpeta"]["path"]            = siguiente["path"]
                _estado["carpeta"]["resultado"]       = None
                _estado["carpeta"]["evento"]          = ev2
                _estado["carpeta"]["callback_actual"] = siguiente["callback"]
                _estado["carpeta"]["posicion"]        = pos_actual + 1
                # total puede haber crecido si llegaron más tracks mientras tanto
                with _bandeja_lock:
                    nuevos = len(_bandeja)
                _estado["carpeta"]["total"] = max(tot_actual, pos_actual + 1 + nuevos)

                # Decirle al HTML que recargue
                view = _ventanas.get("carpeta")
                if view:
                    encolar(lambda v=view: v.page().runJavaScript(
                        "if(typeof recargarBandeja==='function') recargarBandeja();"
                    ))
                # Continuar el loop con el siguiente track
                continue
            else:
                # No quedan tracks — cerrar
                _popup_auto_abierto = False
                encolar(lambda: _cerrar_popup("carpeta"))
                actualizar_tray_bandeja()
                break

    threading.Thread(target=esperar, daemon=True).start()


def abrir_popup_carpeta(nombre_archivo, path_archivo, callback, modo="auto"):
    global _popup_manual_abierto
    ev = threading.Event()
    _estado["carpeta"]["nombre"]    = nombre_archivo
    _estado["carpeta"]["path"]      = path_archivo
    _estado["carpeta"]["resultado"] = None
    _estado["carpeta"]["evento"]    = ev
    _estado["carpeta"]["modo"]      = modo

    if modo == "manual":
        _popup_manual_abierto = True

    crear_ventana("carpeta", 650, 520, HTML_CARPETA)

    def esperar():
        ev.wait(timeout=300)
        carpeta = _estado["carpeta"]["resultado"]
        encolar(lambda: _cerrar_popup("carpeta"))
        if modo == "manual":
            global _popup_manual_abierto
            _popup_manual_abierto = False
            # Drenar bandeja si quedaron tracks encolados mientras estaba el popup manual
            with _bandeja_lock:
                quedan = len(_bandeja)
            if quedan > 0:
                with _bandeja_lock:
                    siguiente = _bandeja.pop(0)
                actualizar_tray_bandeja()
                encolar(lambda s=siguiente: _abrir_popup_auto(s["nombre"], s["path"], s["callback"]))
        callback(carpeta)

    threading.Thread(target=esperar, daemon=True).start()

# ──────────────────────────────────────────────
#  POPUP SYNC
# ──────────────────────────────────────────────
def abrir_popup_sync(copiar, borrar):
    s = _estado["sync"]
    s["copiar"]     = copiar
    s["borrar"]     = borrar
    s["progreso"]   = []
    s["aplicando"]  = False
    s["completado"] = False
    crear_ventana("sync", 520, 420, HTML_SYNC)

def _aplicar_sync():
    s      = _estado["sync"]
    total  = len(s["copiar"]) + len(s["borrar"])
    actual = 0

    for rel in s["copiar"]:
        actual += 1
        pct = actual / total * 100 if total else 100
        s["progreso"].append({"pct": round(pct, 1), "nombre": os.path.basename(rel)})
        try:
            src = os.path.join(get_biblioteca(), rel)
            dst = os.path.join(get_pd(), rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"[PD] Copiado: {rel}")
        except Exception as e:
            print(f"[ERROR] {rel}: {e}")

    for rel in s["borrar"]:
        actual += 1
        pct = actual / total * 100 if total else 100
        s["progreso"].append({"pct": round(pct, 1), "nombre": os.path.basename(rel)})
        try:
            dst = os.path.join(get_pd(), rel)
            if os.path.isfile(dst): os.remove(dst)
            _limpiar_carpetas_vacias(os.path.dirname(dst))
            print(f"[PD] Borrado: {rel}")
        except Exception as e:
            print(f"[ERROR] {rel}: {e}")

    s["completado"] = True
    time.sleep(1)
    encolar(lambda: _cerrar_popup("sync"))

# ──────────────────────────────────────────────
#  NOTIFICACIONES — S24/S25
# ──────────────────────────────────────────────
_tray = None

def notificar(titulo, mensaje, critico=False):
    if _tray:
        icono = QSystemTrayIcon.MessageIcon.Critical if critico else QSystemTrayIcon.MessageIcon.Information
        _tray.showMessage(titulo, mensaje, icono, 4000)

# ──────────────────────────────────────────────
#  PROCESAR ARCHIVO NUEVO
# ──────────────────────────────────────────────
def procesar_archivo_nuevo(path):
    nombre = os.path.basename(path)

    if not es_audio(path):
        print(f"[IGNORADO] No es audio: {nombre}")
        return

    print(f"[NUEVO] Audio detectado: {nombre}")
    if not esperar_archivo_completo(path):
        msg = f"No se pudo leer: {nombre}"
        print(f"[ERROR] {msg}")
        encolar(lambda: notificar("Sounduct — Error", msg, critico=True))
        return

    resultado = [None]
    done = threading.Event()

    def callback(carpeta):
        resultado[0] = carpeta
        done.set()

    # S48/S50: si ya hay un popup auto abierto o hay tracks en bandeja, encolar
    with _bandeja_lock:
        hay_bandeja = len(_bandeja) > 0

    if _popup_auto_abierto or hay_bandeja:
        print(f"[BANDEJA] Encolando: {nombre}")
        with _bandeja_lock:
            _bandeja.append({"nombre": nombre, "path": path, "callback": callback})
        actualizar_tray_bandeja()
        done.wait()
        return

    # Si popup manual abierto, encolar solo si hay una descarga activa
    if _popup_manual_abierto:
        descarga_activa = _estado["carpeta"]["descarga"]["activa"]
        if descarga_activa:
            print(f"[BANDEJA] Descarga en curso, encolando: {nombre}")
        else:
            print(f"[BANDEJA] Popup manual idle, encolando para procesar al cerrar: {nombre}")
        with _bandeja_lock:
            _bandeja.append({"nombre": nombre, "path": path, "callback": callback})
        actualizar_tray_bandeja()
        done.wait()
        return

    # S48: primer track — abrir popup directamente
    encolar(lambda: _abrir_popup_auto(nombre, path, callback))
    done.wait()

# ──────────────────────────────────────────────
#  WATCHERS
# ──────────────────────────────────────────────
IGNORAR_EXT = {'.tmp', '.crdownload', '.part', '.download'}
_archivos_en_proceso = set()
_proceso_lock = threading.Lock()

def es_ignorable(path):
    return os.path.splitext(path)[1].lower() in IGNORAR_EXT

def _esta_en_biblioteca(path):
    """Verifica si un path está dentro de la biblioteca — evita doble detección."""
    try:
        bib = os.path.abspath(get_biblioteca())
        return os.path.abspath(path).startswith(bib + os.sep) or os.path.abspath(path) == bib
    except:
        return False


class ManejadorDescargas(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or es_ignorable(event.src_path):
            return
        if _esta_en_biblioteca(event.src_path):
            return
        with _proceso_lock:
            if event.src_path in _archivos_en_proceso: return
            _archivos_en_proceso.add(event.src_path)
        threading.Thread(target=self._procesar, args=(event.src_path,), daemon=True).start()

    def _procesar(self, path):
        try:
            procesar_archivo_nuevo(path)
        finally:
            with _proceso_lock:
                _archivos_en_proceso.discard(path)

    def on_moved(self, event):
        if not event.is_directory and es_audio(event.dest_path):
            if _esta_en_biblioteca(event.dest_path):
                return
            src_ext = os.path.splitext(event.src_path)[1].lower()
            if src_ext in IGNORAR_EXT or not es_audio(event.src_path):
                threading.Thread(target=procesar_archivo_nuevo, args=(event.dest_path,), daemon=True).start()

class ManejadorBiblioteca(FileSystemEventHandler):
    def on_deleted(self, event):
        if es_ignorable(event.src_path): return
        if pd_conectado(): pd_delete(relativo(event.src_path))

    def on_moved(self, event):
        if es_ignorable(event.src_path): return
        if not pd_conectado(): return
        rel_src = relativo(event.src_path)
        rel_dst = relativo(event.dest_path)
        if event.is_directory: pd_rename_dir(rel_src, rel_dst)
        else: pd_move(rel_src, rel_dst)

# ──────────────────────────────────────────────
#  MONITOR PENDRIVE
# ──────────────────────────────────────────────
_sync_lock = threading.Lock()

def hacer_sync():
    with _sync_lock:
        print("[PD] Calculando diff...")
        copiar, borrar = calcular_diff()
        print(f"[PD] Diff: {len(copiar)} copiar, {len(borrar)} borrar")
        if copiar or borrar:
            encolar(lambda c=copiar, b=borrar: abrir_popup_sync(c, b))
        else:
            print("[PD] TODO EN ORDEN.")

def monitor_pendrive():
    estaba = pd_conectado()
    encolar(lambda e=estaba: actualizar_icono_tray(e))
    if estaba:
        threading.Thread(target=hacer_sync, daemon=True).start()
    while True:
        time.sleep(CHECK_PD_CADA)
        ahora = pd_conectado()
        if ahora != estaba:
            encolar(lambda a=ahora: actualizar_icono_tray(a))
        if ahora and not estaba:
            print("[PD] Pendrive detectado.")
            threading.Thread(target=hacer_sync, daemon=True).start()
        estaba = ahora

# ──────────────────────────────────────────────
#  WATCHERS — arrancar/detener en caliente
# ──────────────────────────────────────────────
_observer_dl  = None
_observer_bib = None

def arrancar_watchers():
    global _observer_dl, _observer_bib

    if _observer_dl and _observer_dl.is_alive():
        _observer_dl.stop()
        _observer_dl.join()
    if _observer_bib and _observer_bib.is_alive():
        _observer_bib.stop()
        _observer_bib.join()

    # S34: las carpetas se crean al guardar config, no acá

    manejador_dl = ManejadorDescargas()
    _observer_dl = Observer()
    _observer_dl.schedule(manejador_dl, get_descargas(), recursive=False)
    _observer_dl.start()

    manejador_bib = ManejadorBiblioteca()
    _observer_bib = Observer()
    _observer_bib.schedule(manejador_bib, get_biblioteca(), recursive=True)
    _observer_bib.start()

    print(f"[WATCHERS] Descargas : {get_descargas()}")
    print(f"[WATCHERS] Biblioteca: {get_biblioteca()}")

# ──────────────────────────────────────────────
#  SYSTEM TRAY
# ──────────────────────────────────────────────
_ICONO_AMBAR = None
_ICONO_GRIS  = None

def _crear_icono(color):
    px = QPixmap(32, 32)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(QColor(color))
    p.drawEllipse(4, 4, 24, 24)
    p.end()
    return QIcon(px)

def inicializar_iconos():
    global _ICONO_AMBAR, _ICONO_GRIS
    ico_path = os.path.join(BASE_DIR, "assets", "sounduct.ico")
    if os.path.exists(ico_path):
        _ICONO_AMBAR = QIcon(ico_path)
        _ICONO_GRIS  = QIcon(ico_path)
    else:
        _ICONO_AMBAR = _crear_icono("#8B53FF")
        _ICONO_GRIS  = _crear_icono("#6b7280")

def actualizar_icono_tray(conectado):
    if _tray is None: return
    with _bandeja_lock:
        pendientes = len(_bandeja)
    if pendientes > 0:
        # S49: mostrar cantidad de tracks en cola
        _tray.setIcon(_ICONO_AMBAR)
        _tray.setToolTip(f"Sounduct — {pendientes} track{'s' if pendientes > 1 else ''} pendiente{'s' if pendientes > 1 else ''}")
    elif conectado:
        _tray.setIcon(_ICONO_AMBAR)
        _tray.setToolTip("Sounduct — PD conectado")
    else:
        _tray.setIcon(_ICONO_GRIS)
        _tray.setToolTip("Sounduct — PD desconectado")


def actualizar_tray_bandeja():
    """S49: actualizar ícono y tooltip según bandeja."""
    pd = pd_conectado()
    encolar(lambda p=pd: actualizar_icono_tray(p))

def setup_tray(app, config_pendiente=False):
    global _tray
    inicializar_iconos()
    _tray = QSystemTrayIcon(_ICONO_GRIS, app)
    _tray.setToolTip("Sounduct — configuracion pendiente" if config_pendiente else "Sounduct")

    menu = QMenu()

    # S35: estado diferente si no hay config
    if config_pendiente:
        accion_estado = menu.addAction("⚠ Configuracion pendiente")
    else:
        accion_estado = menu.addAction("● Sounduct activo")
    accion_estado.setEnabled(False)
    menu.addSeparator()

    # S39: Abrir menu manual de descarga
    if not config_pendiente:
        accion_menu = menu.addAction("Descargar URL")
        def abrir_menu_manual():
            if _popup_manual_abierto:
                print("[TRAY] Menu manual ya abierto.")
                return
            def callback_manual(carpeta):
                pass  # en modo manual no hay archivo que mover
            encolar(lambda: abrir_popup_carpeta("", None, callback_manual, modo="manual"))
        accion_menu.triggered.connect(abrir_menu_manual)
        menu.addSeparator()

    accion_settings = menu.addAction("Configuracion")
    def abrir_settings_desde_tray():
        def on_cerrado(guardo):
            if guardo:
                # Si venía de config pendiente, arrancar app completa
                if config_pendiente:
                    iniciar_app()
                else:
                    arrancar_watchers()
                    print("[TRAY] Watchers reiniciados.")
        encolar(lambda: abrir_popup_settings(callback=on_cerrado))
    accion_settings.triggered.connect(abrir_settings_desde_tray)

    menu.addSeparator()

    accion_salir = menu.addAction("Salir")
    def salir():
        if _observer_dl:  _observer_dl.stop()
        if _observer_bib: _observer_bib.stop()
        _tray.hide()
        app.quit()
    accion_salir.triggered.connect(salir)

    _tray.setContextMenu(menu)
    _tray.show()
    return _tray

# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  SOUNDUCT v1")
    print("=" * 52)

    # Levantar servidor HTTP antes que todo
    iniciar_servidor()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    timer = QTimer()
    timer.timeout.connect(procesar_cola)
    timer.start(100)

    def iniciar_app():
        print(f"  Descargas : {get_descargas()}")
        print(f"  Biblioteca: {get_biblioteca()}")
        print(f"  PD        : {get_pd()}  ({'CONECTADO' if pd_conectado() else 'NO conectado'})")
        arrancar_watchers()
        threading.Thread(target=monitor_pendrive, daemon=True).start()
        setup_tray(app)
        print("  Listo.")

    if not os.path.exists(CONFIG_PATH):
        print("[CONFIG] Primera vez — abriendo configuracion inicial...")
        # S35: tray mínimo con estado "pendiente" antes de configurar
        setup_tray(app, config_pendiente=True)
        def on_primer_setup(guardo):
            if guardo:
                iniciar_app()
            else:
                print("[CONFIG] Cancelado. Usa el tray para configurar.")
        abrir_popup_settings(callback=on_primer_setup)
    else:
        iniciar_app()

    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        if _observer_dl:  _observer_dl.stop()
        if _observer_bib: _observer_bib.stop()
        print("\n[SOUNDUCT] Detenido.")
    if _observer_dl:  _observer_dl.join()
    if _observer_bib: _observer_bib.join()
