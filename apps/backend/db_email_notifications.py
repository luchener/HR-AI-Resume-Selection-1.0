"""Transactional SQL adapter for administrator email notification routes.

The adapter owns the broadcast and recipient snapshot before invoking an
injected SMTP sender.  Provider calls are deliberately outside SQL
transactions; every result is then recorded idempotently in PostgreSQL.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Callable, Iterable, Mapping
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

import email_templates
from db_audit import append_audit
from db_email_state import email_broadcasts, email_images, email_recipients
from db_models import User

_EXTENSIONS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_MAX_IMAGE_BYTES = 2 * 1024 * 1024


class EmailNotificationError(Exception):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail, self.status = detail, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value, label="id") -> UUID:
    try:
        result = value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise EmailNotificationError(f"Invalid {label}") from None
    if not result.int:
        raise EmailNotificationError(f"Invalid {label}")
    return result


def _text(value, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise EmailNotificationError(f"{name} must be text")
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise EmailNotificationError(f"Invalid {name}")
    return value


def _magic(content: bytes, ext: str) -> bool:
    return ((ext == "png" and content[:8] == b"\x89PNG\r\n\x1a\n") or
            (ext in {"jpg", "jpeg"} and content[:3] == b"\xff\xd8\xff") or
            (ext == "webp" and content[:4] == b"RIFF" and content[8:12] == b"WEBP"))


def _safe_snapshot(payload: Mapping, image_rows: Mapping[UUID, Mapping]) -> dict:
    subject = _text(payload.get("subject") or "", "subject", email_templates.MAX_SUBJECT)
    body = _text(payload.get("body") or "", "body", email_templates.MAX_BODY)
    template_id = payload.get("template_id") or "notice"
    if template_id not in email_templates.TEMPLATES:
        template_id = "notice"
    raw_images = payload.get("images") or []
    if not isinstance(raw_images, list):
        raise EmailNotificationError("Invalid images")
    images = []
    for item in raw_images[:email_templates.MAX_IMAGES]:
        if not isinstance(item, Mapping) or not item.get("image_id"):
            continue
        identity = _uuid(item["image_id"], "image_id")
        if identity not in image_rows:
            raise EmailNotificationError("Image not found", 404)
        try:
            width = max(60, min(800, int(item.get("width") or 360)))
        except (TypeError, ValueError, OverflowError):
            width = 360
        images.append({"image_id": str(identity), "width": width})
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        raise EmailNotificationError("Invalid items")
    items = [str(value).strip()[:email_templates.MAX_ITEM_LEN] for value in raw_items
             if str(value).strip()][:email_templates.MAX_ITEMS]
    accent = payload.get("accent") if isinstance(payload.get("accent"), Mapping) else {}
    accent = {"enabled": bool(accent.get("enabled")),
              "text": str(accent.get("text") or "").strip()[:email_templates.MAX_ACCENT]}
    button = payload.get("button") if isinstance(payload.get("button"), Mapping) else {}
    url = str(button.get("url") or "").strip()[:email_templates.MAX_BUTTON_URL]
    button = {"enabled": bool(button.get("enabled")),
              "url": url,
              "text": str(button.get("text") or "前往工作台").strip()[:email_templates.MAX_BUTTON_TEXT]}
    theme = email_templates.sanitize_theme(payload.get("theme"))
    target = payload.get("target") or "all"
    if target not in {"all", "selected"}:
        raise EmailNotificationError("Invalid target")
    ids = payload.get("user_ids") or []
    if not isinstance(ids, list):
        raise EmailNotificationError("Invalid user_ids")
    clean_ids = []
    seen = set()
    for value in ids[:200]:
        identity = _uuid(value, "user_id")
        if identity not in seen:
            clean_ids.append(str(identity))
            seen.add(identity)
    return {"subject": subject, "template_id": template_id, "body": body,
            "items": items, "images": images, "accent": accent, "button": button,
            "theme": theme, "target": target, "user_ids": clean_ids}


def _digest(value: Mapping) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class EmailNotificationService:
    def __init__(self, session_factory: Callable, *, admin_emails: Iterable[str] = (), send_email: Callable):
        if not callable(session_factory) or not callable(send_email):
            raise TypeError("Explicit session factory and email sender required")
        if isinstance(admin_emails, (str, bytes)):
            raise TypeError("admin_emails must be an iterable")
        self.sessions = session_factory
        self.admin_emails = frozenset(str(v).strip().lower() for v in admin_emails if str(v).strip())
        self.send_email = send_email

    def _actor(self, session, actor_id, password_version):
        identity = _uuid(actor_id, "actor_id")
        row = session.execute(sa.select(User).where(User.id == identity).with_for_update()).scalar_one_or_none()
        if (row is None or row.status != "active" or row.deleted_at is not None or
                type(password_version) is not int or row.password_version != password_version or
                not (row.is_admin or (row.email or "").strip().lower() in self.admin_emails)):
            raise EmailNotificationError("Administrator required", 403)
        return row

    @staticmethod
    def _audit(session, actor, action, target_id, request_id):
        append_audit(session, actor_user_id=actor.id, action="notification." + action,
                     target_type="notification", target_id=str(target_id), request_id=request_id)

    def save_image(self, actor_id, content: bytes, filename: str, *, password_version, request_id):
        if not isinstance(content, bytes) or not content or len(content) > _MAX_IMAGE_BYTES:
            raise EmailNotificationError("Invalid image size")
        name = PurePath(str(filename or "")).name.strip()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _EXTENSIONS or not _magic(content, ext):
            raise EmailNotificationError("Only real PNG/JPG/WebP images are accepted")
        if len(name) > 512 or not name:
            raise EmailNotificationError("Invalid image name")
        identity = uuid4()
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            session.execute(email_images.insert().values(
                id=identity, uploaded_by_id=actor.id, original_name=name, extension=ext,
                mime_type=_EXTENSIONS[ext], content=content, size_bytes=len(content)))
            self._audit(session, actor, "image_upload", identity, request_id)
        return {"image_id": str(identity), "name": name, "ext": ext, "size": len(content)}

    def list_images(self, actor_id, *, password_version):
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            rows = session.execute(sa.select(email_images).where(email_images.c.deleted_at.is_(None))
                                   .order_by(email_images.c.created_at.desc(), email_images.c.id.desc())).mappings().all()
        return [{"image_id": str(row["id"]), "name": row["original_name"],
                 "ext": row["extension"], "size": row["size_bytes"]} for row in rows]

    def image_data_uri(self, actor_id, image_id, *, password_version):
        identity = _uuid(image_id, "image_id")
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            row = session.execute(sa.select(email_images.c.mime_type, email_images.c.content)
                                  .where(email_images.c.id == identity, email_images.c.deleted_at.is_(None))).mappings().one_or_none()
        if row is None:
            raise EmailNotificationError("Image not found", 404)
        return "data:{};base64,{}".format(row["mime_type"], base64.b64encode(row["content"]).decode("ascii"))

    def delete_image(self, actor_id, image_id, *, password_version, request_id):
        identity = _uuid(image_id, "image_id")
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            row = session.execute(sa.select(email_images).where(email_images.c.id == identity).with_for_update()).mappings().one_or_none()
            if row is None or row["deleted_at"] is not None:
                raise EmailNotificationError("Image not found", 404)
            session.execute(email_images.update().where(email_images.c.id == identity).values(deleted_at=_now()))
            self._audit(session, actor, "image_delete", identity, request_id)
        return {"message": "Image deleted"}

    def recipients(self, actor_id, *, password_version, keyword=""):
        if not isinstance(keyword, str) or len(keyword) > 200:
            raise EmailNotificationError("Invalid keyword")
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            statement = sa.select(User.id, User.username, User.email).where(User.status == "active", User.deleted_at.is_(None))
            clean = keyword.strip()
            if clean:
                pattern = "%" + clean.replace("%", "\\%").replace("_", "\\_") + "%"
                statement = statement.where(sa.or_(User.username.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\")))
            rows = session.execute(statement.order_by(User.username).limit(1000)).mappings().all()
        items = [{"user_id": str(row["id"]), "username": row["username"], "email": row["email"] or ""} for row in rows]
        return {"total_users": len(items), "with_email": sum(bool(item["email"].strip()) for item in items), "items": items}

    def _recipient_rows(self, session, snapshot):
        statement = sa.select(User.id, User.email, User.username).where(User.status == "active", User.deleted_at.is_(None), User.email.is_not(None), User.email != "")
        if snapshot["target"] == "selected":
            ids = [_uuid(value, "user_id") for value in snapshot["user_ids"]]
            if not ids:
                return []
            statement = statement.where(User.id.in_(ids))
        return session.execute(statement.order_by(User.id)).mappings().all()

    def _image_rows(self, session, snapshot):
        ids = [_uuid(item["image_id"], "image_id") for item in snapshot["images"]]
        if not ids:
            return {}
        return {row["id"]: row for row in session.execute(sa.select(email_images).where(email_images.c.id.in_(ids), email_images.c.deleted_at.is_(None))).mappings().all()}

    def preview(self, actor_id, payload, *, password_version):
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            image_rows = self._image_rows(session, {"images": payload.get("images") or []})
            snapshot = _safe_snapshot(payload, image_rows)
        html, _plain = self._render(snapshot, image_rows)
        return html

    def _render(self, snapshot, image_rows):
        def src(image_id, _width):
            row = image_rows.get(_uuid(image_id, "image_id"))
            if not row:
                return ""
            return "data:{};base64,{}".format(row["mime_type"], base64.b64encode(row["content"]).decode("ascii"))
        compose = {key: snapshot[key] for key in ("subject", "template_id", "body", "images", "items", "accent", "button", "theme")}
        html = email_templates.render_broadcast_email(image_src=src, **compose)
        plain = email_templates.render_broadcast_plain(**{key: snapshot[key] for key in ("subject", "template_id", "body", "items", "accent", "button")})
        return html, plain

    def send(self, actor_id, payload, *, password_version, request_id, idempotency_key=None):
        idem = idempotency_key or request_id
        if not isinstance(idem, str) or not _IDEMPOTENCY.fullmatch(idem):
            raise EmailNotificationError("Invalid idempotency key")
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            image_rows = self._image_rows(session, {"images": payload.get("images") or []})
            snapshot = _safe_snapshot(payload, image_rows)
            recipients = self._recipient_rows(session, snapshot)
            identity = uuid4()
            values = dict(id=identity, idempotency_key=idem, created_by_id=actor.id, created_by_name=actor.username,
                          subject=snapshot["subject"], template_id=snapshot["template_id"], compose_snapshot=snapshot,
                          target=snapshot["target"], target_count=len(recipients))
            created = session.execute(insert(email_broadcasts).values(**values)
                                      .on_conflict_do_nothing(index_elements=[email_broadcasts.c.idempotency_key])
                                      .returning(email_broadcasts.c.id)).scalar_one_or_none()
            if created is None:
                existing = session.execute(sa.select(email_broadcasts).where(email_broadcasts.c.idempotency_key == idem).with_for_update()).mappings().one()
                if _digest(existing["compose_snapshot"]) != _digest(snapshot):
                    raise EmailNotificationError("Idempotency key belongs to a different broadcast", 409)
                return self._summary_from_row(existing, replayed=True)
            if recipients:
                session.execute(email_recipients.insert(), [dict(broadcast_id=identity, user_id=row["id"], email=row["email"].strip()) for row in recipients])
            self._audit(session, actor, "broadcast_create", identity, request_id)
        # Provider calls happen only after the snapshot commit.
        self._deliver(identity, snapshot, image_rows)
        return self._summary_by_id(identity)

    def _deliver(self, identity, snapshot, image_rows):
        html, plain = self._render_send(snapshot)
        with self.sessions() as session, session.begin():
            rows = session.execute(sa.select(email_recipients).where(email_recipients.c.broadcast_id == identity,
                                                                      email_recipients.c.status == "pending").order_by(email_recipients.c.user_id).with_for_update()).mappings().all()
        image_parts = [{"image_id": str(row["id"]), "content": row["content"], "mime_subtype": "jpeg" if row["extension"] == "jpg" else row["extension"]}
                       for row in image_rows.values()]
        for row in rows:
            try:
                self.send_email(row["email"], snapshot["subject"], plain, html, image_parts)
            except Exception as exc:  # provider errors are persisted, never leaked
                self._record_delivery(identity, row["user_id"], "failed", str(exc)[:200])
            else:
                self._record_delivery(identity, row["user_id"], "sent", None)

    def _render_send(self, snapshot):
        def src(image_id, _width):
            return "cid:img-" + str(image_id)
        compose = {key: snapshot[key] for key in ("subject", "template_id", "body", "images", "items", "accent", "button", "theme")}
        html = email_templates.render_broadcast_email(image_src=src, **compose)
        plain = email_templates.render_broadcast_plain(**{key: snapshot[key] for key in ("subject", "template_id", "body", "items", "accent", "button")})
        return html, plain

    def _record_delivery(self, broadcast_id, user_id, status, error):
        with self.sessions() as session, session.begin():
            row = session.execute(sa.select(email_recipients).where(email_recipients.c.broadcast_id == broadcast_id,
                                                                     email_recipients.c.user_id == user_id).with_for_update()).mappings().one_or_none()
            if row is None or row["status"] != "pending":
                return
            session.execute(email_recipients.update().where(email_recipients.c.broadcast_id == broadcast_id,
                                                            email_recipients.c.user_id == user_id).values(
                status=status, error_message=error, attempts=row["attempts"] + 1,
                sent_at=_now() if status == "sent" else None))
            counts = dict(session.execute(sa.select(email_recipients.c.status, sa.func.count().label("n")).where(
                email_recipients.c.broadcast_id == broadcast_id).group_by(email_recipients.c.status)).all())
            session.execute(email_broadcasts.update().where(email_broadcasts.c.id == broadcast_id).values(
                sent=counts.get("sent", 0), skipped=counts.get("skipped", 0), failed=counts.get("failed", 0)))

    @staticmethod
    def _summary_from_row(row, *, replayed=False):
        return {"email_log_id": str(row["id"]), "subject": row["subject"], "target": row["target"],
                "target_count": row["target_count"], "sent": row["sent"], "skipped": row["skipped"],
                "failed": row["failed"], "errors": [], "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "replayed": replayed}

    def _summary_by_id(self, identity):
        with self.sessions() as session, session.begin():
            row = session.execute(sa.select(email_broadcasts).where(email_broadcasts.c.id == identity)).mappings().one()
            errors = [dict(email=item["email"], error=item["error_message"] or "") for item in session.execute(
                sa.select(email_recipients).where(email_recipients.c.broadcast_id == identity,
                                                 email_recipients.c.status == "failed")).mappings()]
            return self._summary_from_row(row) | {"errors": errors}

    def list_logs(self, actor_id, *, password_version, page=1, size=10):
        try:
            page, size = int(page), int(size)
        except (TypeError, ValueError):
            raise EmailNotificationError("Invalid pagination") from None
        if page < 1 or size < 1 or size > 100:
            raise EmailNotificationError("Invalid pagination")
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            base = sa.select(email_broadcasts).order_by(email_broadcasts.c.created_at.desc(), email_broadcasts.c.id.desc())
            total = session.scalar(sa.select(sa.func.count()).select_from(email_broadcasts)) or 0
            rows = session.execute(base.offset((page - 1) * size).limit(size)).mappings().all()
            result = [self._summary_from_row(row) for row in rows]
        return {"total": int(total), "items": result}

    def get_log(self, log_id, actor_id, *, password_version):
        identity = _uuid(log_id, "log_id")
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
            row = session.execute(sa.select(email_broadcasts).where(email_broadcasts.c.id == identity)).mappings().one_or_none()
            if row is None:
                raise EmailNotificationError("Sending record not found", 404)
            snapshot = row["compose_snapshot"]
            image_rows = self._image_rows(session, snapshot)
            errors = [dict(email=item["email"], error=item["error_message"] or "") for item in session.execute(
                sa.select(email_recipients).where(email_recipients.c.broadcast_id == identity, email_recipients.c.status == "failed")).mappings()]
            record = self._summary_from_row(row) | {"errors": errors, "compose_snapshot": snapshot,
                "created_by_id": str(row["created_by_id"]), "created_by_name": row["created_by_name"]}
        html, _plain = self._render(snapshot, image_rows)
        return {"record": record, "html": html}
