# lector_recbot.py
# Lee número (PAN) y fecha de vencimiento de una tarjeta con chip EMV vía PC/SC.
# Requiere: pip install pyscard colorama
from smartcard.System import readers
from smartcard.util import toHexString
import time
import csv
import os
from datetime import datetime

from colorama import init, Fore, Style
init(autoreset=True)

ARCHIVO_LOG = "lecturas_tarjetas.csv"


# ---------- Utilidades TLV (BER-TLV) ----------
def parse_tlv(data):
    """Devuelve lista de (tag, valor, es_constructed)."""
    resultado = []
    i = 0
    while i < len(data):
        if data[i] in (0x00, 0xFF):  # relleno
            i += 1
            continue
        primer = data[i]
        tag = data[i]
        i += 1
        if primer & 0x1F == 0x1F:  # tag de varios bytes
            while i < len(data):
                b = data[i]
                i += 1
                tag = (tag << 8) | b
                if not b & 0x80:
                    break
        if i >= len(data):
            break
        longitud = data[i]
        i += 1
        if longitud & 0x80:
            n = longitud & 0x7F
            longitud = int.from_bytes(bytes(data[i:i + n]), "big")
            i += n
        valor = data[i:i + longitud]
        i += longitud
        resultado.append((tag, valor, bool(primer & 0x20)))
    return resultado


def buscar_tag(data, objetivo):
    """Busca un tag de forma recursiva. Devuelve el valor o None."""
    for tag, valor, constructed in parse_tlv(data):
        if tag == objetivo:
            return valor
        if constructed:
            r = buscar_tag(valor, objetivo)
            if r is not None:
                return r
    return None


def buscar_todos(data, objetivo):
    encontrados = []
    for tag, valor, constructed in parse_tlv(data):
        if tag == objetivo:
            encontrados.append(valor)
        if constructed:
            encontrados.extend(buscar_todos(valor, objetivo))
    return encontrados


# ---------- Comunicación APDU ----------
def enviar(conexion, apdu):
    data, sw1, sw2 = conexion.transmit(apdu)
    if sw1 == 0x6C:  # longitud incorrecta, reintentar con Le correcto
        apdu = apdu[:-1] + [sw2]
        data, sw1, sw2 = conexion.transmit(apdu)
    while sw1 == 0x61:  # hay más datos
        extra, sw1, sw2 = conexion.transmit([0x00, 0xC0, 0x00, 0x00, sw2])
        data += extra
    return data, sw1, sw2


def select(conexion, nombre_bytes):
    apdu = [0x00, 0xA4, 0x04, 0x00, len(nombre_bytes)] + list(nombre_bytes) + [0x00]
    return enviar(conexion, apdu)


def construir_pdol(pdol):
    """Rellena con ceros los datos que pide el PDOL."""
    out = []
    i = 0
    while i < len(pdol):
        i_tag = pdol[i]
        i += 1
        if i_tag & 0x1F == 0x1F:
            while i < len(pdol):
                b = pdol[i]
                i += 1
                if not b & 0x80:
                    break
        longitud = pdol[i]
        i += 1
        out += [0x00] * longitud
    return out


# ---------- Lectura de la tarjeta ----------
def leer_datos_tarjeta(conexion):
    # 1) Intentar PPSE (contactless) y luego PSE (contacto)
    aids = []
    for nombre in (b"2PAY.SYS.DDF01", b"1PAY.SYS.DDF01"):
        data, sw1, sw2 = select(conexion, nombre)
        if (sw1, sw2) == (0x90, 0x00):
            aids = buscar_todos(data, 0x4F)
            if aids:
                break

    # Si no hay directorio, probar AIDs comunes
    if not aids:
        aids = [
            [0xA0, 0x00, 0x00, 0x00, 0x03, 0x10, 0x10],        # Visa
            [0xA0, 0x00, 0x00, 0x00, 0x04, 0x10, 0x10],        # Mastercard
            [0xA0, 0x00, 0x00, 0x00, 0x25, 0x01],              # Amex
        ]

    for aid in aids:
        data, sw1, sw2 = select(conexion, aid)
        if (sw1, sw2) != (0x90, 0x00):
            continue

        # 2) GET PROCESSING OPTIONS
        pdol = buscar_tag(data, 0x9F38)
        datos_pdol = construir_pdol(pdol) if pdol else []
        cuerpo = [0x83, len(datos_pdol)] + datos_pdol
        apdu = [0x80, 0xA8, 0x00, 0x00, len(cuerpo)] + cuerpo + [0x00]
        gpo, sw1, sw2 = enviar(conexion, apdu)
        if (sw1, sw2) != (0x90, 0x00):
            continue

        # 3) Obtener AFL (lista de registros a leer)
        if gpo[0] == 0x80:      # formato 1: AIP (2 bytes) + AFL
            afl = gpo[4:] if len(gpo) > 2 else []
            afl = list(gpo[1 + 1 + 2:]) if gpo[1] > 2 else []
        else:                   # formato 2 (tag 77)
            afl = buscar_tag(gpo, 0x94) or []

        pan = None
        vencimiento = None
        nombre = None

        for k in range(0, len(afl), 4):
            sfi = afl[k] >> 3
            primero, ultimo = afl[k + 1], afl[k + 2]
            for rec in range(primero, ultimo + 1):
                reg, sw1, sw2 = enviar(conexion, [0x00, 0xB2, rec, (sfi << 3) | 4, 0x00])
                if (sw1, sw2) != (0x90, 0x00):
                    continue

                v = buscar_tag(reg, 0x5A)  # PAN
                if v and not pan:
                    pan = "".join(f"{b:02X}" for b in v).rstrip("F")

                v = buscar_tag(reg, 0x5F24)  # vencimiento YYMMDD
                if v and not vencimiento:
                    h = "".join(f"{b:02X}" for b in v)
                    vencimiento = f"{h[2:4]}/{h[0:2]}"

                v = buscar_tag(reg, 0x57)  # Track 2 equivalente: PAN D YYMM ...
                if v and (not pan or not vencimiento):
                    h = "".join(f"{b:02X}" for b in v)
                    if "D" in h:
                        t_pan, resto = h.split("D", 1)
                        if not pan:
                            pan = t_pan
                        if not vencimiento and len(resto) >= 4:
                            vencimiento = f"{resto[2:4]}/{resto[0:2]}"

                v = buscar_tag(reg, 0x5F20)  # nombre del titular
                if v and not nombre:
                    try:
                        nombre = v.decode("latin-1").strip(" /\x00") or None
                    except Exception:
                        nombre = None

        if pan or vencimiento:
            return pan, vencimiento, nombre

    return None, None, None


def formatear_pan(pan):
    return " ".join(pan[i:i + 4] for i in range(0, len(pan), 4))


def enmascarar_pan(pan):
    """Muestra solo los primeros 6 y últimos 4 dígitos, el resto con *."""
    if len(pan) <= 10:
        return pan
    return pan[:6] + "*" * (len(pan) - 10) + pan[-4:]


# ---------- Identificación de marca (por rango del PAN) ----------
def identificar_marca(pan):
    if not pan:
        return "Desconocida"
    if pan.startswith("4"):
        return "Visa"
    if pan[:2] in {"51", "52", "53", "54", "55"} or (
        len(pan) >= 4 and 2221 <= int(pan[:4]) <= 2720
    ):
        return "Mastercard"
    if pan[:2] in {"34", "37"}:
        return "American Express"
    if pan[:2] == "60" or pan[:4] == "6011" or pan[:3] in {"644", "645", "646", "647", "648", "649"}:
        return "Discover"
    if pan[:3] in {"300", "301", "302", "303", "304", "305"} or pan[:2] in {"36", "38"}:
        return "Diners Club"
    if pan[:2] == "35":
        return "JCB"
    return "Desconocida"


# ---------- Registro en CSV ----------
def registrar_lectura(atr_hex, marca, pan, venc, cvv=None, nombre=None):
    nuevo = not os.path.exists(ARCHIVO_LOG)
    with open(ARCHIVO_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha_hora", "atr", "marca", "titular", "numero_enmascarado", "vencimiento", "cvv"])
        w.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            atr_hex,
            marca,
            nombre or "N/D",
            enmascarar_pan(pan) if pan else "N/D",
            venc or "N/D",
            cvv or "N/D",
        ])


# ---------- Presentación en consola ----------
ANCHO = 44


def imprimir_tabla(atr_hex, marca, pan, venc, cvv=None, nombre=None):
    print(Fore.GREEN + Style.BRIGHT + "┌" + "─" * ANCHO + "┐")
    print(Fore.GREEN + Style.BRIGHT + "│" + " TARJETA DETECTADA".ljust(ANCHO) + "│")
    print(Fore.GREEN + Style.BRIGHT + "├" + "─" * ANCHO + "┤")

    def fila(etiqueta, valor, color=Fore.WHITE):
        texto = f" {etiqueta:<12}: {valor}"
        print(Fore.GREEN + "│" + color + texto.ljust(ANCHO) + Fore.GREEN + "│")

    fila("ATR", atr_hex, Fore.CYAN)
    fila("Marca", marca, Fore.YELLOW)
    fila("Titular", nombre or "no disponible", Fore.WHITE)
    fila("Número", formatear_pan(pan) if pan else "no disponible", Fore.WHITE + Style.BRIGHT)
    fila("Vencimiento", venc or "no disponible", Fore.WHITE + Style.BRIGHT)
    fila("CVV", cvv or "no disponible", Fore.WHITE + Style.BRIGHT)

    print(Fore.GREEN + Style.BRIGHT + "└" + "─" * ANCHO + "┘")


def obtener_lector(lectores_previos=None):
    """Busca lectores PC/SC. Devuelve la lista actual (puede estar vacía)."""
    try:
        return readers()
    except Exception:
        return []


def main():
    print(Fore.CYAN + Style.BRIGHT + "=== Lector de tarjetas Recbot ===\n")

    lector_actual = None
    nombre_lector_actual = None
    tarjeta_presente = False

    try:
        while True:
            # --- Verificar/renovar lector (maneja desconexión del dispositivo) ---
            if lector_actual is None:
                lectores = obtener_lector()
                if not lectores:
                    print(Fore.RED + "⚠ No se detecta ningún lector PC/SC. "
                                      "Verifica que el Recbot esté conectado.", end="\r")
                    time.sleep(2)
                    continue

                lector_actual = lectores[0]
                nombre_lector_actual = str(lector_actual)
                print(Fore.CYAN + f"\nUsando lector: {nombre_lector_actual}")
                print(Fore.CYAN + "Esperando a que insertes una tarjeta... (Ctrl+C para salir)\n")
                tarjeta_presente = False

            try:
                conexion = lector_actual.createConnection()
                conexion.connect()

                if not tarjeta_presente:
                    tarjeta_presente = True
                    atr = conexion.getATR()
                    atr_hex = toHexString(atr)

                    pan, venc, nombre = leer_datos_tarjeta(conexion)
                    marca = identificar_marca(pan) if pan else "Desconocida"

                    # El CVV impreso en la parte trasera NO se puede leer del chip.
                    print(Fore.YELLOW + "\nEl código de seguridad (CVV/CVC) impreso en la parte trasera\n"
                          " no es legible electrónicamente. Escríbelo manualmente.\n"
                          "⚠ Aviso: almacenar el CVV es sensible; no compartir el CSV.")
                    cvv = input(Fore.CYAN + "CVV (Enter para omitir): " + Style.RESET_ALL).strip()
                    if cvv and not cvv.isdigit():
                        print(Fore.RED + "CVV inválido (debe ser numérico). Se guardará como N/D.")
                        cvv = None

                    imprimir_tabla(atr_hex, marca, pan, venc, cvv, nombre)
                    registrar_lectura(atr_hex, marca, pan, venc, cvv, nombre)
                    print(Fore.LIGHTBLACK_EX + f"(Registrado en {ARCHIVO_LOG})\n")

                conexion.disconnect()

            except Exception as e:
                msg = str(e).lower()
                # El lector físico se desconectó / ya no está disponible
                if "no smart card readers" in msg or "unable to list" in msg or "no service" in msg:
                    if lector_actual is not None:
                        print(Fore.RED + f"\n✖ Se perdió la conexión con el lector "
                                          f"({nombre_lector_actual}). Reintentando...\n")
                    lector_actual = None
                    tarjeta_presente = False
                else:
                    # Normalmente significa simplemente que no hay tarjeta insertada
                    if tarjeta_presente:
                        print(Fore.YELLOW + "Tarjeta removida. Esperando de nuevo...\n")
                    tarjeta_presente = False

            time.sleep(1)

    except KeyboardInterrupt:
        print(Fore.CYAN + "\nPrograma detenido.")


if __name__ == "__main__":
    main()