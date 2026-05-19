# crear_acceso_directo.py
# S27: crea un acceso directo de Sounduct en el escritorio del usuario.
# Ejecutar una vez después de compilar con PyInstaller.
#
# Uso:
#   python crear_acceso_directo.py
#
# Requiere: pip install pywin32

import os
import sys

def crear_acceso_directo():
    try:
        import win32com.client
    except ImportError:
        print("[ERROR] Falta pywin32. Instalar con: pip install pywin32")
        sys.exit(1)

    escritorio = os.path.join(os.path.expanduser("~"), "Desktop")
    acceso     = os.path.join(escritorio, "Sounduct.lnk")

    # Ruta al ejecutable — asume que está en la misma carpeta que este script
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Sounduct.exe")

    if not os.path.exists(exe):
        print(f"[ERROR] No se encontró Sounduct.exe en: {exe}")
        print("Compilar primero con: pyinstaller sounduct.spec")
        sys.exit(1)

    shell    = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(acceso)
    shortcut.Targetpath       = exe
    shortcut.WorkingDirectory = os.path.dirname(exe)
    shortcut.Description      = "Sounduct — Guide Your Sound"

    # Icono
    ico = os.path.join(os.path.dirname(exe), "sounduct.ico")
    if os.path.exists(ico):
        shortcut.IconLocation = ico

    shortcut.save()
    print(f"[OK] Acceso directo creado en: {acceso}")

if __name__ == "__main__":
    crear_acceso_directo()
