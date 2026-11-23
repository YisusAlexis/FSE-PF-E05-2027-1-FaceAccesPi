"""
cripto.py - Cifrado de los datos biométricos y personales con AES-256-GCM.

Qué se cifra (ver db.py):
  - el vector SFace de cada persona (dato biométrico)
  - el nombre de cada persona
  - el detalle de la auditoría (puede contener nombres)
Los eventos de acceso guardan solo el id numérico de la persona, la hora y la similitud.

Claves: un archivo de 32 bytes aleatorios (por defecto /etc/faceaccess/clave.bin) con
permisos 0400 y dueño el usuario del servicio. De él se derivan dos subclaves con HMAC-SHA256:
  - K_cifrado: AES-256-GCM (nonce aleatorio de 12 bytes por registro; etiqueta de 16 bytes)
  - K_indice:  HMAC del nombre, para buscar y garantizar nombres únicos sin guardarlos en claro
Cada texto cifrado va ligado a su contexto con datos asociados (AAD): no se puede copiar el
vector de una persona a la fila de otra sin que el descifrado falle.

Si se pierde el archivo de clave, los datos cifrados no se pueden recuperar.
"""
import hashlib
import hmac
import os
import secrets
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TAM_CLAVE = 32
TAM_NONCE = 12


class ErrorCripto(Exception):
    pass


def generar_clave(ruta):
    """Crea el archivo de clave con permisos 0400. No sobrescribe una clave existente."""
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        os.write(fd, secrets.token_bytes(TAM_CLAVE))
    finally:
        os.close(fd)


class Cripto:
    def __init__(self, ruta_clave, log=None):
        try:
            info = os.stat(ruta_clave)
            with open(ruta_clave, 'rb') as f:
                maestra = f.read()
        except OSError as e:
            raise ErrorCripto(f'No se pudo leer la clave {ruta_clave}: {e.strerror}. '
                              f'Ejecuta: sudo bash scripts/instalar.sh') from e
        if len(maestra) != TAM_CLAVE:
            raise ErrorCripto(f'La clave {ruta_clave} debe medir {TAM_CLAVE} bytes')
        if log and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning(f'La clave {ruta_clave} tiene permisos {oct(info.st_mode & 0o777)}; '
                        f'deberían ser 0400 (solo el dueño)')
        self._cifrador = AESGCM(hmac.new(maestra, b'faceaccess:cifrado', hashlib.sha256).digest())
        self._k_indice = hmac.new(maestra, b'faceaccess:indice', hashlib.sha256).digest()

    def cifrar(self, datos, contexto):
        """Devuelve nonce (12 B) + texto cifrado + etiqueta (16 B)."""
        nonce = secrets.token_bytes(TAM_NONCE)
        return nonce + self._cifrador.encrypt(nonce, datos, contexto.encode())

    def descifrar(self, blob, contexto):
        try:
            return self._cifrador.decrypt(blob[:TAM_NONCE], blob[TAM_NONCE:], contexto.encode())
        except (InvalidTag, ValueError) as e:
            raise ErrorCripto(f'Dato alterado o clave incorrecta (contexto {contexto})') from e

    def cifrar_texto(self, texto, contexto):
        return self.cifrar(texto.encode('utf-8'), contexto)

    def descifrar_texto(self, blob, contexto):
        return self.descifrar(blob, contexto).decode('utf-8')

    def indice(self, nombre):
        """Identificador estable del nombre (HMAC); no permite recuperar el nombre."""
        return hmac.new(self._k_indice, nombre.encode('utf-8'), hashlib.sha256).hexdigest()
