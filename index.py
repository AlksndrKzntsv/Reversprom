# Обработчик заявок с формы сайта "Реверспром" (Yandex Cloud Functions, runtime python312).
#
# Почему файлы идут через Object Storage:
#   запрос к функции ограничен 3,5 МБ, поэтому файлы до 10 МБ нельзя передать в теле запроса.
#   Браузер загружает их напрямую в закрытый бакет по временным ссылкам, а функция потом
#   забирает их оттуда и прикладывает к письму (на исходящее письмо лимит 3,5 МБ не действует).
#
# Протокол (POST, JSON):
#   1) { action: "init", files: [{ name, size, type }] }
#      → { ok, uploadId, uploads: [{ key, url }] }  — ссылки для PUT, живут UPLOAD_URL_TTL секунд.
#   2) { action: "submit", name, contact, comment, page, time, website, uploadId,
#        files: [{ key, filename }] }
#      → { ok }  — функция скачивает файлы из бакета, отправляет письмо и удаляет файлы.
#   Поле website — ловушка для ботов (скрыто на сайте); если оно заполнено, письмо не отправляется.
#
# Все настройки и секреты — ТОЛЬКО в переменных окружения функции (см. DEPLOY.md).
# Используется только стандартная библиотека Python — дополнительные зависимости не нужны.

import base64
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage

# --- Почта ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")          # почтовый ящик, от имени которого отправляем
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")  # пароль приложения (НЕ обычный пароль от почты)
TO_EMAILS_RAW = os.environ.get("TO_EMAILS", "")      # например: "info@company.ru,sales@company.ru"

# --- Object Storage (статический ключ доступа сервисного аккаунта) ---
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_HOST = os.environ.get("S3_HOST", "storage.yandexcloud.net")
S3_REGION = os.environ.get("S3_REGION", "ru-central1")

# --- CORS: адрес сайта, например "https://alksndrkzntsv.github.io" (без пути и слэша в конце).
# Можно указать несколько через запятую (например, ещё http://localhost:8080 для проверки).
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "").split(",") if o.strip()]

MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 10 МБ — тот же лимит, что и MAX_BYTES на сайте
MAX_FILES = 10                      # то же, что MAX_FILES на сайте
UPLOAD_URL_TTL = 600                # срок жизни ссылки на загрузку, секунд

# Те же расширения, что в атрибуте accept у поля файла на сайте
ALLOWED_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "tif", "tiff",
    "pdf", "zip", "rar", "7z", "step", "stp", "stl", "dwg", "dxf",
}

KEY_PREFIX = "leads/"
KEY_RE = re.compile(r"^leads/([0-9a-f]{32})/(\d{1,2})$")
CONTENT_TYPE_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


# ---------------------------------------------------------------------------
# HTTP-ответы
# ---------------------------------------------------------------------------

def _cors_origin(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin = headers.get("origin", "")
    if origin and origin in ALLOWED_ORIGINS:
        return origin
    return ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "*"


def _response(event, status_code, body_dict):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": _cors_origin(event),
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        },
        "body": json.dumps(body_dict, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Подпись ссылок для Object Storage (AWS Signature V4, query-параметры)
# ---------------------------------------------------------------------------

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def presign(method, host, path, access_key, secret_key, region, expires, now=None):
    """Возвращает подписанную ссылку https://host/path, действующую expires секунд."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    scope = f"{date}/{region}/s3/aws4_request"

    canonical_uri = urllib.parse.quote(path, safe="/~")
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(v, safe='~')}"
        for k, v in sorted(query.items())
    )
    canonical_request = "\n".join([
        method, canonical_uri, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, "s3")
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def _object_url(method, key, expires=UPLOAD_URL_TTL):
    return presign(method, S3_HOST, f"/{S3_BUCKET}/{key}", S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION, expires)


def _s3_get(key):
    """Скачивает объект. Возвращает (bytes, content_type) или None, если объекта нет."""
    req = urllib.request.Request(_object_url("GET", key, 60), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # читаем не больше лимита + 1 байт, чтобы не тянуть в память лишнее
            data = resp.read(MAX_TOTAL_BYTES + 1)
            return data, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _s3_delete(key):
    req = urllib.request.Request(_object_url("DELETE", key, 60), method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:  # не критично: объект удалит правило жизненного цикла бакета
        print(f"WARN: не удалось удалить {key}: {e}")


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _clean_line(value, limit):
    """Строка без переводов строк (защита заголовков письма) и ограниченной длины."""
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()[:limit]


def _clean_filename(value):
    name = _clean_line(value, 200)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


def _extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _content_type(filename, declared):
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if CONTENT_TYPE_RE.match(declared) and declared != "application/octet-stream":
        return declared
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _config_errors(need_s3):
    missing = []
    if not SMTP_USER:
        missing.append("SMTP_USER")
    if not SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if not TO_EMAILS_RAW.strip():
        missing.append("TO_EMAILS")
    if need_s3:
        for name, value in (("S3_BUCKET", S3_BUCKET), ("S3_ACCESS_KEY", S3_ACCESS_KEY), ("S3_SECRET_KEY", S3_SECRET_KEY)):
            if not value:
                missing.append(name)
    return missing


# ---------------------------------------------------------------------------
# Шаг 1: выдача ссылок на загрузку
# ---------------------------------------------------------------------------

def _handle_init(event, data):
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return _response(event, 400, {"ok": False, "error": "Не переданы файлы"})
    if len(files) > MAX_FILES:
        return _response(event, 400, {"ok": False, "error": f"Можно прикрепить не более {MAX_FILES} файлов"})

    total = 0
    for f in files:
        if not isinstance(f, dict):
            return _response(event, 400, {"ok": False, "error": "Некорректный формат списка файлов"})
        name = _clean_filename(f.get("name"))
        if _extension(name) not in ALLOWED_EXTENSIONS:
            return _response(event, 400, {"ok": False, "error": f"Недопустимый тип файла: {name}"})
        try:
            total += max(0, int(f.get("size") or 0))
        except (TypeError, ValueError):
            return _response(event, 400, {"ok": False, "error": "Некорректный размер файла"})
    if total > MAX_TOTAL_BYTES:
        return _response(event, 400, {"ok": False, "error": "Суммарный размер вложений превышает 10 МБ"})

    upload_id = uuid.uuid4().hex
    uploads = []
    for i in range(len(files)):
        key = f"{KEY_PREFIX}{upload_id}/{i}"
        uploads.append({"key": key, "url": _object_url("PUT", key)})
    return _response(event, 200, {"ok": True, "uploadId": upload_id, "uploads": uploads})


# ---------------------------------------------------------------------------
# Шаг 2: отправка письма
# ---------------------------------------------------------------------------

def _handle_submit(event, data):
    # Ловушка для ботов: человек это скрытое поле не видит и не заполняет.
    # Отвечаем "успехом", чтобы бот не понял, что его отсеяли.
    if (data.get("website") or "").strip():
        print("INFO: заявка отклонена ловушкой для ботов")
        return _response(event, 200, {"ok": True})

    name = _clean_line(data.get("name"), 200)
    contact = _clean_line(data.get("contact"), 200)
    comment = str(data.get("comment") or "").strip()[:5000]
    page = _clean_line(data.get("page"), 500)
    time_str = _clean_line(data.get("time"), 100)
    upload_id = str(data.get("uploadId") or "")
    files = data.get("files") or []

    if not name or not contact:
        return _response(event, 400, {"ok": False, "error": "Не заполнены обязательные поля: имя и контакт"})
    if not isinstance(files, list) or len(files) > MAX_FILES:
        return _response(event, 400, {"ok": False, "error": "Некорректный список вложений"})

    # Проверяем ключи: только объекты из "своей" папки leads/<uploadId>/, выданной на шаге init
    refs = []
    for f in files:
        key = str((f or {}).get("key") or "") if isinstance(f, dict) else ""
        m = KEY_RE.match(key)
        if not m or m.group(1) != upload_id:
            return _response(event, 400, {"ok": False, "error": "Некорректная ссылка на файл"})
        filename = _clean_filename(f.get("filename"))
        if _extension(filename) not in ALLOWED_EXTENSIONS:
            return _response(event, 400, {"ok": False, "error": f"Недопустимый тип файла: {filename}"})
        refs.append((key, filename))

    missing = _config_errors(need_s3=bool(refs))
    if missing:
        print(f"ERROR: не заданы переменные окружения: {', '.join(missing)}")
        return _response(event, 500, {"ok": False, "error": "Сервер заявок не настроен"})

    # Скачиваем вложения из бакета
    attachments = []
    total = 0
    try:
        for key, filename in refs:
            got = _s3_get(key)
            if got is None:
                return _response(event, 400, {"ok": False, "error": f"Файл не загрузился: {filename}. Попробуйте ещё раз"})
            raw, stored_type = got
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                return _response(event, 400, {"ok": False, "error": "Суммарный размер вложений превышает 10 МБ"})
            attachments.append((filename, _content_type(filename, stored_type), raw))
    except Exception as e:
        print(f"ERROR: не удалось скачать файлы из Object Storage: {e!r}")
        return _response(event, 502, {"ok": False, "error": "Не удалось получить загруженные файлы"})

    to_emails = [e.strip() for e in TO_EMAILS_RAW.split(",") if e.strip()]

    msg = EmailMessage()
    msg["Subject"] = f"Заявка с сайта Реверспром — {name}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(to_emails)
    # Чтобы можно было нажать "Ответить" и попасть сразу клиенту (если contact похож на email)
    if re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", contact):
        msg["Reply-To"] = contact
    msg.set_content("\n".join([
        "Новая заявка с лендинга Реверспром",
        "",
        f"Имя: {name}",
        f"Контакт: {contact}",
        f"Комментарий: {comment or '-'}",
        f"Вложений: {len(attachments)}" + (f" ({', '.join(fn for fn, _, _ in attachments)})" if attachments else ""),
        "",
        f"Страница: {page or '-'}",
        f"Время отправки: {time_str or '-'}",
    ]))
    for filename, content_type, raw in attachments:
        maintype, subtype = content_type.split("/", 1)
        msg.add_attachment(raw, maintype=maintype, subtype=subtype, filename=filename)

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ssl.create_default_context()) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg, from_addr=SMTP_USER, to_addrs=to_emails)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg, from_addr=SMTP_USER, to_addrs=to_emails)
    except Exception as e:
        # Подробности — только в логах функции, посетителю сайта их показывать незачем
        print(f"ERROR: ошибка отправки письма: {e!r}")
        return _response(event, 502, {"ok": False, "error": "Не удалось отправить письмо"})

    # Письмо ушло — файлы в бакете больше не нужны (персональные данные не храним дольше необходимого)
    for key, _ in refs:
        _s3_delete(key)

    print(f"INFO: заявка отправлена, вложений: {len(attachments)}, байт: {total}")
    return _response(event, 200, {"ok": True})


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def handler(event, context):
    method = (event.get("httpMethod") or "").upper()

    # Preflight-запрос браузера (CORS)
    if method == "OPTIONS":
        return _response(event, 200, {"ok": True})
    if method != "POST":
        return _response(event, 405, {"ok": False, "error": "Метод не поддерживается"})

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception:
            return _response(event, 400, {"ok": False, "error": "Некорректная кодировка запроса"})
    try:
        data = json.loads(raw_body)
    except Exception:
        return _response(event, 400, {"ok": False, "error": "Некорректный JSON в запросе"})
    if not isinstance(data, dict):
        return _response(event, 400, {"ok": False, "error": "Некорректный JSON в запросе"})

    action = data.get("action") or "submit"
    if action == "init":
        missing = _config_errors(need_s3=True)
        if missing:
            print(f"ERROR: не заданы переменные окружения: {', '.join(missing)}")
            return _response(event, 500, {"ok": False, "error": "Сервер заявок не настроен"})
        return _handle_init(event, data)
    if action == "submit":
        return _handle_submit(event, data)
    return _response(event, 400, {"ok": False, "error": "Неизвестное действие"})
