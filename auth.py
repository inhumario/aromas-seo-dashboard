"""Autenticación multi-usuario, gestión de cuentas y recuperación de contraseña.

Todo se persiste en Postgres (tablas `users` y `password_resets`, definidas en db.py).
No depende de librerías externas: hashing con PBKDF2 (stdlib) y email con smtplib.
"""
import os
import re
import hmac
import hashlib
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

import psycopg

import db


PBKDF2_ROUNDS = 240_000
RESET_TTL_MINUTES = 60
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PUBLIC_FIELDS = ("id", "username", "email", "full_name", "role",
                 "is_active", "must_change_password", "created_at", "last_login_at")


# ---------- Hashing de contraseñas ----------

def hash_password(password: str) -> str:
    """Devuelve un hash PBKDF2-SHA256 con sal aleatoria, formato `algo$rounds$salt$hash`."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def generate_password(length: int = 14) -> str:
    """Contraseña temporal segura y legible."""
    return secrets.token_urlsafe(length)[:length]


# ---------- Validación ----------

def validate_username(username: str):
    if not USERNAME_RE.match(username or ""):
        return False, "El usuario debe tener 3-32 caracteres: minúsculas, números, punto, guion o guion bajo."
    return True, ""


def validate_email(email: str):
    if not EMAIL_RE.match(email or ""):
        return False, "El email no es válido."
    return True, ""


def validate_password(password: str):
    if not password or len(password) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."
    return True, ""


# ---------- Lectura de usuarios ----------

def _public(row):
    if not row:
        return None
    return {k: row.get(k) for k in PUBLIC_FIELDS}


def count_users() -> int:
    with db.get_conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def count_active_admins() -> int:
    with db.get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=TRUE"
        ).fetchone()["n"]


def get_user_by_login(login: str):
    """Busca por username o email (insensible a mayúsculas). Devuelve la fila completa."""
    login = (login or "").strip().lower()
    if not login:
        return None
    with db.get_conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE lower(username)=%s OR lower(email)=%s LIMIT 1",
            (login, login),
        ).fetchone()


def get_user_by_id(user_id):
    with db.get_conn() as c:
        return c.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()


def list_users():
    with db.get_conn() as c:
        rows = c.execute(
            "SELECT * FROM users ORDER BY (role='admin') DESC, username"
        )
        return [_public(r) for r in rows]


# ---------- Autenticación ----------

def authenticate(login: str, password: str):
    """Devuelve el usuario público si las credenciales son correctas, None si no."""
    user = get_user_by_login(login)
    if not user or not user.get("is_active"):
        return None
    if not verify_password(password, user.get("password_hash") or ""):
        return None
    with db.get_conn() as c:
        c.execute("UPDATE users SET last_login_at=NOW() WHERE id=%s", (user["id"],))
    user["last_login_at"] = datetime.now(timezone.utc)
    return _public(user)


# ---------- Alta / gestión de usuarios ----------

def create_user(username, email, full_name, password, role="viewer", must_change=True):
    username = (username or "").strip().lower()
    email = (email or "").strip().lower()
    full_name = (full_name or "").strip()
    ok, msg = validate_username(username)
    if not ok:
        return False, msg
    ok, msg = validate_email(email)
    if not ok:
        return False, msg
    if not full_name:
        return False, "El nombre completo es obligatorio."
    if role not in ("admin", "viewer"):
        return False, "El rol no es válido."
    ok, msg = validate_password(password)
    if not ok:
        return False, msg
    try:
        with db.get_conn() as c:
            c.execute(
                """INSERT INTO users (username, email, full_name, password_hash, role, must_change_password)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (username, email, full_name, hash_password(password), role, must_change),
            )
        return True, "Usuario creado correctamente."
    except psycopg.errors.UniqueViolation:
        return False, "Ya existe un usuario con ese nombre o ese email."
    except Exception as e:
        return False, f"No se pudo crear el usuario: {e}"


def set_role(user_id, role, acting_user_id):
    if role not in ("admin", "viewer"):
        return False, "El rol no es válido."
    user = get_user_by_id(user_id)
    if not user:
        return False, "Usuario no encontrado."
    if role == "viewer" and user["role"] == "admin" and user["is_active"] and count_active_admins() <= 1:
        return False, "No puedes quitar el rol de administrador: es el último administrador activo."
    with db.get_conn() as c:
        c.execute("UPDATE users SET role=%s WHERE id=%s", (role, user_id))
    return True, "Rol actualizado."


def set_active(user_id, active, acting_user_id):
    if user_id == acting_user_id and not active:
        return False, "No puedes desactivar tu propia cuenta."
    user = get_user_by_id(user_id)
    if not user:
        return False, "Usuario no encontrado."
    if not active and user["role"] == "admin" and user["is_active"] and count_active_admins() <= 1:
        return False, "No puedes desactivar el último administrador activo."
    with db.get_conn() as c:
        c.execute("UPDATE users SET is_active=%s WHERE id=%s", (active, user_id))
    return True, ("Cuenta activada." if active else "Cuenta desactivada.")


def delete_user(user_id, acting_user_id):
    if user_id == acting_user_id:
        return False, "No puedes eliminar tu propia cuenta."
    user = get_user_by_id(user_id)
    if not user:
        return False, "Usuario no encontrado."
    if user["role"] == "admin" and user["is_active"] and count_active_admins() <= 1:
        return False, "No puedes eliminar el último administrador activo."
    with db.get_conn() as c:
        c.execute("DELETE FROM users WHERE id=%s", (user_id,))
    return True, "Usuario eliminado."


def change_own_password(user_id, current_password, new_password):
    user = get_user_by_id(user_id)
    if not user:
        return False, "Usuario no encontrado."
    if not verify_password(current_password, user.get("password_hash") or ""):
        return False, "La contraseña actual no es correcta."
    ok, msg = validate_password(new_password)
    if not ok:
        return False, msg
    if verify_password(new_password, user.get("password_hash") or ""):
        return False, "La nueva contraseña debe ser distinta de la actual."
    with db.get_conn() as c:
        c.execute(
            "UPDATE users SET password_hash=%s, must_change_password=FALSE WHERE id=%s",
            (hash_password(new_password), user_id),
        )
    return True, "Contraseña actualizada correctamente."


def force_set_password(user_id, new_password):
    """Cambio de contraseña sin pedir la actual (para el flujo de cambio obligatorio tras login)."""
    ok, msg = validate_password(new_password)
    if not ok:
        return False, msg
    with db.get_conn() as c:
        c.execute(
            "UPDATE users SET password_hash=%s, must_change_password=FALSE WHERE id=%s",
            (hash_password(new_password), user_id),
        )
    return True, "Contraseña actualizada correctamente."


# ---------- Recuperación de contraseña ----------

def _smtp_creds():
    return os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")


def _send_email(to_addr, subject, html):
    user, password = _smtp_creds()
    if not user or not password:
        return False, "SMTP no configurado (faltan GMAIL_USER / GMAIL_APP_PASSWORD)."
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Panel SEO Aromas <{user}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, password)
            s.sendmail(user, [to_addr], msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _reset_email_html(name, link, welcome):
    if welcome:
        intro = (f"Hola {name}, te damos la bienvenida al <strong>Panel SEO de Aromas de Té</strong>. "
                 "Se ha creado una cuenta para ti. Pulsa el botón para establecer tu contraseña "
                 "y empezar a usar el panel.")
        cta = "Crear mi contraseña"
        title = "Bienvenido al Panel SEO"
    else:
        intro = (f"Hola {name}, hemos recibido una solicitud para restablecer la contraseña de tu "
                 "cuenta del <strong>Panel SEO de Aromas de Té</strong>. Pulsa el botón para "
                 "elegir una nueva contraseña.")
        cta = "Restablecer mi contraseña"
        title = "Restablecer contraseña"
    return f"""<!DOCTYPE html>
<html lang="es"><body style="margin:0;background:#FBF8F3;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#3A3128;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FBF8F3;padding:32px 0;">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #ECE3D4;">
        <tr><td style="background:#C8654A;padding:26px 32px;color:#fff;">
          <div style="font-size:30px;line-height:1;">🍵</div>
          <div style="font-size:19px;font-weight:700;margin-top:8px;">{title}</div>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="font-size:15px;line-height:1.6;margin:0 0 22px;">{intro}</p>
          <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="border-radius:9px;background:#C8654A;">
            <a href="{link}" style="display:inline-block;padding:13px 26px;color:#ffffff;text-decoration:none;font-weight:700;font-size:15px;">{cta}</a>
          </td></tr></table>
          <p style="font-size:12px;color:#8A7E6C;line-height:1.6;margin:22px 0 0;">
            El enlace caduca en {RESET_TTL_MINUTES} minutos. Si el botón no funciona, copia esta dirección en tu navegador:<br>
            <a href="{link}" style="color:#C8654A;word-break:break-all;">{link}</a>
          </p>
          <p style="font-size:12px;color:#8A7E6C;line-height:1.6;margin:14px 0 0;">
            Si no esperabas este correo, puedes ignorarlo: tu contraseña no cambiará.
          </p>
        </td></tr>
        <tr><td style="padding:16px 32px;background:#F1EADE;font-size:11px;color:#8A7E6C;">
          Panel SEO · Aromas de Té · uso interno
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_reset_link(user_row, base_url, welcome=False):
    """Crea un token de un solo uso y envía el email. `user_row` es una fila completa de `users`.

    Cada enlace emitido es válido por sí mismo hasta que se usa o caduca: NO se invalidan
    los anteriores. Así, si se piden varios correos, cualquiera de los enlaces funciona
    (gana el primero que se usa). Evita el confuso "el enlace ya se ha usado" cuando en
    realidad solo había sido sustituido por una petición posterior.
    """
    if not user_row:
        return False, "Usuario no encontrado."
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_TTL_MINUTES)
    with db.get_conn() as c:
        # limpieza de tokens antiguos ya caducados (no afecta a ninguno vigente)
        c.execute("DELETE FROM password_resets WHERE created_at < NOW() - INTERVAL '7 days'")
        c.execute(
            "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user_row["id"], _hash_token(token), expires),
        )
    link = f"{base_url.rstrip('/')}/?reset_token={token}"
    subject = ("Bienvenido al Panel SEO de Aromas de Té" if welcome
               else "Restablece tu contraseña — Panel SEO Aromas")
    name = (user_row.get("full_name") or user_row.get("username") or "").split(" ")[0]
    html = _reset_email_html(name or "hola", link, welcome)
    return _send_email(user_row["email"], subject, html)


def request_password_reset_public(login, base_url):
    """Para el formulario público de recuperación: no revela si la cuenta existe."""
    try:
        user = get_user_by_login(login)
        if user and user.get("is_active"):
            send_reset_link(user, base_url, welcome=False)
    except Exception:
        pass


def check_reset_token(token):
    """Devuelve (usuario_publico, None) si el token es válido, o (None, motivo)."""
    th = _hash_token(token)
    with db.get_conn() as c:
        row = c.execute(
            """SELECT id, user_id, expires_at, used_at FROM password_resets
               WHERE token_hash=%s ORDER BY created_at DESC LIMIT 1""",
            (th,),
        ).fetchone()
    if not row:
        return None, ("Este enlace de recuperación no es válido. Puede que sea de un correo "
                      "antiguo. Solicita uno nuevo más abajo.")
    if row["used_at"]:
        return None, ("Este enlace ya se ha utilizado. Si fuiste tú quien cambió la contraseña, "
                       "tu contraseña nueva ya está activa: vuelve al inicio e inicia sesión. "
                       "Si no, solicita un enlace nuevo más abajo.")
    if row["expires_at"] < datetime.now(timezone.utc):
        return None, ("Este enlace ha caducado (los enlaces duran 60 minutos). "
                       "Solicita uno nuevo más abajo.")
    user = get_user_by_id(row["user_id"])
    if not user or not user.get("is_active"):
        return None, "La cuenta asociada no está disponible."
    return _public(user), None


def reset_password_with_token(token, new_password):
    user, reason = check_reset_token(token)
    if not user:
        return False, reason
    ok, msg = validate_password(new_password)
    if not ok:
        return False, msg
    with db.get_conn() as c:
        c.execute(
            "UPDATE users SET password_hash=%s, must_change_password=FALSE WHERE id=%s",
            (hash_password(new_password), user["id"]),
        )
        c.execute(
            "UPDATE password_resets SET used_at=NOW() WHERE token_hash=%s AND used_at IS NULL",
            (_hash_token(token),),
        )
    return True, "Contraseña actualizada. Ya puedes iniciar sesión."


# ---------- Bootstrap del primer administrador ----------

def init_auth():
    """Si la tabla `users` está vacía, crea el administrador inicial a partir del entorno.

    Usa DASHBOARD_PASSWORD (o ADMIN_PASSWORD) como contraseña semilla y marca
    `must_change_password=TRUE`, de modo que en el primer inicio de sesión se
    obliga a cambiarla y deja de depender de la variable de entorno.
    """
    if count_users() > 0:
        return
    pw = (os.environ.get("DASHBOARD_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or "").strip()
    if not pw:
        return
    username = (os.environ.get("ADMIN_USERNAME") or "mario").strip().lower()
    email = (os.environ.get("ADMIN_EMAIL") or "cuadrado.mario@aromasdete.com").strip().lower()
    full_name = (os.environ.get("ADMIN_FULL_NAME") or "Mario Cuadrado").strip()
    try:
        with db.get_conn() as c:
            c.execute(
                """INSERT INTO users (username, email, full_name, password_hash, role, must_change_password)
                   VALUES (%s, %s, %s, %s, 'admin', TRUE)""",
                (username, email, full_name, hash_password(pw)),
            )
    except Exception:
        pass
