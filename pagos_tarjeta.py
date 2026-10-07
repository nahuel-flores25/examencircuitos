# pagos_tarjeta.py
# Interfaz visual (Tkinter) para leer tarjetas EMV y registrar pagos.
# Reutiliza las funciones de lector_tarjeta.py
import os
import csv
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

from smartcard.System import readers
from smartcard.util import toHexString

from lector_tarjeta import (
    leer_datos_tarjeta,
    identificar_marca,
    enmascarar_pan,
    formatear_pan,
)

ARCHIVO_PAGOS = "pagos.csv"


class AppPagos:
    def __init__(self, root):
        self.root = root
        self.root.title("Recbot - Pagos con tarjeta")
        self.root.geometry("560x420")

        self.datos = {"atr": None, "marca": None, "pan": None,
                      "venc": None, "nombre": None}

        # ----- Título -----
        ttk.Label(root, text="Lector de tarjetas Recbot",
                  font=("Segoe UI", 14, "bold")).pack(pady=8)

        # ----- Datos de la tarjeta -----
        marco = ttk.LabelFrame(root, text="Datos de la tarjeta")
        marco.pack(fill="x", padx=12, pady=6)

        self.var_marca = tk.StringVar(value="-")
        self.var_titular = tk.StringVar(value="-")
        self.var_numero = tk.StringVar(value="-")
        self.var_venc = tk.StringVar(value="-")

        for i, (etq, var) in enumerate([
            ("Marca:", self.var_marca),
            ("Titular:", self.var_titular),
            ("Número:", self.var_numero),
            ("Vencimiento:", self.var_venc),
        ]):
            ttk.Label(marco, text=etq).grid(row=i, column=0, sticky="e", padx=6, pady=3)
            ttk.Label(marco, textvariable=var, font=("Segoe UI", 10, "bold")).grid(
                row=i, column=1, sticky="w", padx=6, pady=3)

        # ----- Pago -----
        marco2 = ttk.LabelFrame(root, text="Pago")
        marco2.pack(fill="x", padx=12, pady=6)

        ttk.Label(marco2, text="Monto ($):").grid(row=0, column=0, sticky="e", padx=6, pady=3)
        self.entry_monto = ttk.Entry(marco2, width=15)
        self.entry_monto.grid(row=0, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(marco2, text="CVV (reverso):").grid(row=1, column=0, sticky="e", padx=6, pady=3)
        self.entry_cvv = ttk.Entry(marco2, width=8, show="*")
        self.entry_cvv.grid(row=1, column=1, sticky="w", padx=6, pady=3)

        # ----- Botones -----
        marco3 = ttk.Frame(root)
        marco3.pack(pady=10)
        ttk.Button(marco3, text="Leer tarjeta", command=self.leer_tarjeta).grid(row=0, column=0, padx=5)
        ttk.Button(marco3, text="Registrar pago", command=self.registrar_pago).grid(row=0, column=1, padx=5)
        ttk.Button(marco3, text="Salir", command=root.destroy).grid(row=0, column=2, padx=5)

        self.estado = ttk.Label(root, text="Conecta el lector e inserta una tarjeta.")
        self.estado.pack(pady=4)

    # ---------- Lectura de la tarjeta ----------
    def leer_tarjeta(self):
        self.estado.config(text="Leyendo tarjeta...")
        threading.Thread(target=self._leer_hilo, daemon=True).start()

    def _leer_hilo(self):
        try:
            lectores = readers()
            if not lectores:
                raise RuntimeError("No se detecta ningún lector PC/SC.")
            conexion = lectores[0].createConnection()
            conexion.connect()
            atr_hex = toHexString(conexion.getATR())
            pan, venc, nombre = leer_datos_tarjeta(conexion)
            conexion.disconnect()
            if not pan:
                raise RuntimeError("No se pudieron leer los datos de la tarjeta.")
            self.datos.update(atr=atr_hex, marca=identificar_marca(pan),
                              pan=pan, venc=venc, nombre=nombre)
            self.root.after(0, self._mostrar_datos)
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
            self.root.after(0, lambda: self.estado.config(text="Error al leer."))

    def _mostrar_datos(self):
        self.var_marca.set(self.datos["marca"])
        self.var_titular.set(self.datos["nombre"] or "no disponible")
        self.var_numero.set(formatear_pan(self.datos["pan"]))
        self.var_venc.set(self.datos["venc"] or "no disponible")
        self.estado.config(text="Tarjeta leída correctamente.")

    # ---------- Registro del pago ----------
    def registrar_pago(self):
        if not self.datos["pan"]:
            messagebox.showwarning("Aviso", "Primero lee una tarjeta.")
            return
        monto = self.entry_monto.get().strip()
        if not monto:
            messagebox.showwarning("Aviso", "Ingresa el monto.")
            return
        try:
            float(monto.replace(",", "."))
        except ValueError:
            messagebox.showerror("Error", "El monto debe ser numérico.")
            return
        cvv = self.entry_cvv.get().strip()
        if cvv and not cvv.isdigit():
            messagebox.showerror("Error", "El CVV debe ser numérico.")
            return

        nuevo = not os.path.exists(ARCHIVO_PAGOS)
        with open(ARCHIVO_PAGOS, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(["fecha_hora", "monto", "marca", "titular",
                            "numero_enmascarado", "vencimiento", "cvv"])
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                monto,
                self.datos["marca"],
                self.datos["nombre"] or "N/D",
                enmascarar_pan(self.datos["pan"]),
                self.datos["venc"] or "N/D",
                cvv or "N/D",
            ])
        self.estado.config(text=f"Pago de ${monto} registrado en {ARCHIVO_PAGOS}.")
        messagebox.showinfo("Listo", "Pago registrado correctamente.")
        self.entry_monto.delete(0, tk.END)
        self.entry_cvv.delete(0, tk.END)


if __name__ == "__main__":
    root = tk.Tk()
    AppPagos(root)
    root.mainloop()
