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
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(BASE_DIR, "config.json")
HTML_CARPETA  = os.path.join(BASE_DIR, "popup_carpeta.html")
HTML_SYNC     = os.path.join(BASE_DIR, "popup_sync.html")
HTML_SETTINGS = os.path.join(BASE_DIR, "popup_settings.html")

CONFIG_DEFAULT = {
    "carpeta_descargas":  os.path.join(os.path.expanduser("~"), "Downloads"),
    "carpeta_biblioteca": os.path.join(os.path.expanduser("~"), "Music", "Biblioteca"),
    "carpeta_pd":         "E:\\",
    "arranque_automatico": False
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
    for rel, size in casa.items():
        if rel not in pd_files or pd_files[rel] != size:
            copiar.append(rel)
    for rel in pd_files:
        if rel not in casa:
            borrar.append(rel)
    return copiar, borrar

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
_estado = {
    "carpeta": {
        "nombre":    None,
        "resultado": None,
        "evento":    None,
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
                "arranque_automatico": get_arranque_automatico()
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
        elif path == "/carpeta/nombre":
            self._json_response({"nombre": _estado["carpeta"]["nombre"] or ""})

        elif path == "/carpeta/carpetas":
            self._json_response({"carpetas": listar_carpetas()})

        elif path == "/carpeta/audio":
            # S31: sirve el archivo de audio desde Descargas
            audio_path = _estado["carpeta"].get("path")
            if not audio_path or not os.path.exists(audio_path):
                self.send_response(404)
                self.end_headers()
                return
            ext  = os.path.splitext(audio_path)[1].lower()
            mime = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.aiff': 'audio/aiff'}.get(ext, 'audio/mpeg')
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
            guardar_config(_config)
            set_arranque_automatico(_config["arranque_automatico"])
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
        elif path == "/carpeta/confirmar":
            carpeta = data.get("carpeta", "")
            _estado["carpeta"]["resultado"] = carpeta
            ev = _estado["carpeta"]["evento"]
            if ev: ev.set()
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
    flags = Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Window
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
def abrir_popup_carpeta(nombre_archivo, path_archivo, callback):
    ev = threading.Event()
    _estado["carpeta"]["nombre"]    = nombre_archivo
    _estado["carpeta"]["path"]      = path_archivo
    _estado["carpeta"]["resultado"] = None
    _estado["carpeta"]["evento"]    = ev

    crear_ventana("carpeta", 650, 480, HTML_CARPETA)

    def esperar():
        ev.wait(timeout=300)
        carpeta = _estado["carpeta"]["resultado"]
        encolar(lambda: _cerrar_popup("carpeta"))
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

    encolar(lambda: abrir_popup_carpeta(nombre, path, callback))
    done.wait()

    carpeta = resultado[0]
    if carpeta is None:
        destino_raiz = os.path.join(get_biblioteca(), nombre)
        try:
            os.makedirs(get_biblioteca(), exist_ok=True)
            shutil.move(path, destino_raiz)
            print(f"[CANCELADO] Movido a raiz: {nombre}")
        except Exception as e:
            print(f"[ERROR] Moviendo a raiz: {e}")
        return

    if carpeta == "/ Raiz":
        destino_local = os.path.join(get_biblioteca(), nombre)
        rel           = nombre
    else:
        destino_local = os.path.join(get_biblioteca(), carpeta, nombre)
        rel           = os.path.join(carpeta, nombre)

    if os.path.abspath(path) != os.path.abspath(destino_local):
        try:
            os.makedirs(os.path.dirname(destino_local), exist_ok=True)
            shutil.move(path, destino_local)
            print(f"[LOCAL] Movido a: {rel}")
        except Exception as e:
            encolar(lambda: notificar("Sounduct — Error", f"Error al mover {nombre}", critico=True))
            return

    if pd_conectado():
        try:
            pd_copy(rel)
            encolar(lambda n=nombre: notificar("Sounduct", f"{n} copiado al PD"))
        except Exception as e:
            encolar(lambda: notificar("Sounduct — Error", f"Error al copiar al PD: {nombre}", critico=True))

# ──────────────────────────────────────────────
#  WATCHERS
# ──────────────────────────────────────────────
IGNORAR_EXT = {'.tmp', '.crdownload', '.part', '.download'}
_archivos_en_proceso = set()
_proceso_lock = threading.Lock()

def es_ignorable(path):
    return os.path.splitext(path)[1].lower() in IGNORAR_EXT

class ManejadorDescargas(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or es_ignorable(event.src_path):
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

    os.makedirs(get_biblioteca(), exist_ok=True)

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
    ico_path = os.path.join(BASE_DIR, "sounduct.ico")
    if os.path.exists(ico_path):
        _ICONO_AMBAR = QIcon(ico_path)
        _ICONO_GRIS  = QIcon(ico_path)
    else:
        _ICONO_AMBAR = _crear_icono("#f59e0b")
        _ICONO_GRIS  = _crear_icono("#6b7280")

def actualizar_icono_tray(conectado):
    if _tray is None: return
    if conectado:
        _tray.setIcon(_ICONO_AMBAR)
        _tray.setToolTip("Sounduct — PD conectado")
    else:
        _tray.setIcon(_ICONO_GRIS)
        _tray.setToolTip("Sounduct — PD desconectado")

def setup_tray(app):
    global _tray
    inicializar_iconos()
    _tray = QSystemTrayIcon(_ICONO_GRIS, app)
    _tray.setToolTip("Sounduct")

    menu = QMenu()

    accion_estado = menu.addAction("● Sounduct activo")
    accion_estado.setEnabled(False)
    menu.addSeparator()

    accion_settings = menu.addAction("Configuracion")
    def abrir_settings_desde_tray():
        def on_cerrado(guardo):
            if guardo:
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
        def on_primer_setup(guardo):
            if guardo:
                iniciar_app()
            else:
                print("[CONFIG] Cancelado. Usa el tray para configurar.")
                setup_tray(app)
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
