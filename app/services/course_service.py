import os
import time
import uuid
import json

from supabase import Client

import os
import uuid
import json

from app.config import get_settings
from app.exceptions import CourseServiceError
from app.models import Course, CourseMaterial, CourseMaterialCreate, CourseCreate
from app.services.supabase_client import get_service_client

# Allowed upload extensions -> MIME types
ALLOWED_MIME_TYPES: set[str] = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/msword",  # .doc
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
}

ALLOWED_EXTENSIONS: set[str] = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".txt", ".md", ".doc", ".docx", ".pptx",
}

STORAGE_BUCKET = "material"  # Supabase Storage bucket name
MAX_TEXT_PREVIEW = 5000  # Store first 5000 chars of txt/md in DB


class CourseService:
    def __init__(self, client: Client | None = None):
        self.client = client or get_service_client()

    @staticmethod
    def _is_transient_upload_error(exc: Exception) -> bool:
        message = str(exc).lower()
        transient_markers = (
            "ssl",
            "bad record mac",
            "connection reset",
            "connection aborted",
            "timeout",
            "timed out",
            "remote end closed",
            "eof occurred",
            "temporarily unavailable",
        )
        return any(marker in message for marker in transient_markers)

    def _upload_with_retry(self, storage_path: str, file_bytes: bytes, mime_type: str | None = None) -> None:
        max_attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                file_options: dict = {}
                if mime_type:
                    file_options["content-type"] = mime_type

                # Use a fresh client each retry to avoid reusing a broken TLS session.
                retry_client = get_service_client()
                retry_client.storage.from_(STORAGE_BUCKET).upload(
                    storage_path,
                    file_bytes,
                    file_options=file_options,
                )
                return
            except Exception as exc:
                last_error = exc
                should_retry = self._is_transient_upload_error(exc) and attempt < max_attempts
                if not should_retry:
                    raise
                time.sleep(0.6 * attempt)

        if last_error is not None:
            raise last_error

    @staticmethod
    def _normalize_storage_url(url: str) -> str:
        if not url:
            return url

        if url.startswith("http://") or url.startswith("https://"):
            return url

        settings = get_settings()
        base = settings.supabase_url.rstrip("/")

        if url.startswith("/storage/v1/"):
            return f"{base}{url}"
        if url.startswith("/object/"):
            return f"{base}/storage/v1{url}"
        if url.startswith("object/"):
            return f"{base}/storage/v1/{url}"
        if url.startswith("/"):
            return f"{base}{url}"
        return f"{base}/{url}"

    def _build_storage_url(self, storage_path: str) -> str:
        signed = self.client.storage.from_(STORAGE_BUCKET).create_signed_url(storage_path, 60 * 60 * 24 * 365)

        signed_url = None
        if isinstance(signed, dict):
            signed_url = (
                signed.get("signedURL")
                or signed.get("signedUrl")
                or signed.get("signed_url")
                or ((signed.get("data") or {}).get("signedURL") if isinstance(signed.get("data"), dict) else None)
            )
        elif isinstance(signed, str):
            signed_url = signed
        else:
            signed_url = (
                getattr(signed, "signedURL", None)
                or getattr(signed, "signedUrl", None)
                or getattr(signed, "signed_url", None)
            )

        if signed_url:
            return self._normalize_storage_url(signed_url)

        public_url = self.client.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
        return self._normalize_storage_url(public_url)

    # ── courses table ─────────────────────────────────────────────────────────

    def create_course(self, user_id: str, payload: CourseCreate) -> Course:
        record = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": payload.name,
            "details": payload.details,
        }
        try:
            response = self.client.table("courses").insert(record).execute()
        except Exception as exc:
            raise CourseServiceError(500, "COURSE_CREATE_FAILED", f"Failed to create course: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "COURSE_CREATE_FAILED", "Course creation returned no data")
        return self._serialize_course(data[0])

    def list_courses(self, user_id: str) -> list[Course]:
        response = (
            self.client.table("courses")
            .select("id, user_id, name, details, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [self._serialize_course(row) for row in (response.data or [])]

    def get_course(self, user_id: str, course_id: str) -> Course:
        response = (
            self.client.table("courses")
            .select("id, user_id, name, details, created_at")
            .eq("id", course_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        data = response.data or []
        if not data:
            raise CourseServiceError(404, "COURSE_NOT_FOUND", "Course not found")
        return self._serialize_course(data[0])

    # ── course_materials table ────────────────────────────────────────────────

    def add_text_material(self, user_id: str, course_id: str, payload: CourseMaterialCreate) -> CourseMaterial:
        self._assert_course_owner(user_id, course_id)
        record = {
            "id": str(uuid.uuid4()),
            "course_id": course_id,
            "user_id": user_id,
            "is_text": True,
            "filename": "text_entry",
            "mime_type": "text/plain",
            "text_material": payload.text_material,
            "storage_url": None,  # No file uploaded for manual text entry
        }
        try:
            response = self.client.table("course_materials").insert(record).execute()
        except Exception as exc:
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", f"Failed to save material: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", "Material creation returned no data")
        return self._serialize_material(data[0])

    def add_file_material(
        self,
        user_id: str,
        course_id: str,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
    ) -> CourseMaterial:
        self._assert_course_owner(user_id, course_id)

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS and mime_type not in ALLOWED_MIME_TYPES:
            raise CourseServiceError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                f"File type '{ext or mime_type}' is not allowed. "
                f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )

        # Upload to Supabase Storage
        material_id = str(uuid.uuid4())
        storage_path = f"{course_id}/{material_id}{ext}"

        try:
            self._upload_with_retry(storage_path, file_bytes, mime_type)
        except Exception as exc:
            raise CourseServiceError(
                500,
                "STORAGE_UPLOAD_FAILED",
                f"Failed to upload file to storage: {exc}",
            ) from exc

        try:
            storage_url = self._build_storage_url(storage_path)
        except Exception as exc:
            raise CourseServiceError(
                500,
                "STORAGE_URL_FAILED",
                f"Failed to generate public URL: {exc}",
            ) from exc

        # For text files, also store decoded content in DB for quick preview/search
        text_preview = None
        is_text_type = mime_type in {"text/plain", "text/markdown"} or ext in {".txt", ".md"}
        if is_text_type:
            try:
                text_preview = file_bytes.decode("utf-8")[:MAX_TEXT_PREVIEW]
            except UnicodeDecodeError:
                text_preview = file_bytes.decode("latin-1")[:MAX_TEXT_PREVIEW]

        record = {
            "id": material_id,
            "course_id": course_id,
            "user_id": user_id,
            "is_text": is_text_type,
            "material": storage_url,
            "text_material": text_preview,
        }

        try:
            response = self.client.table("course_materials").insert(record).execute()
        except Exception as exc:
            try:
                self.client.storage.from_(STORAGE_BUCKET).remove([storage_path])
            except Exception:
                pass
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", f"Failed to save material: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", "File upload returned no data")
        return self._serialize_material(data[0])

    def add_study_file_material(
        self,
        user_id: str,
        course_id: str,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
    ) -> CourseMaterial:
        """Store study uploads in Supabase Storage and save metadata in course_materials."""
        self._assert_course_owner(user_id, course_id)

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS and mime_type not in ALLOWED_MIME_TYPES:
            raise CourseServiceError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                f"File type '{ext or mime_type}' is not allowed. "
                f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )

        # Upload to Supabase Storage
        material_id = str(uuid.uuid4())
        storage_path = f"{course_id}/{material_id}{ext}"

        try:
            self._upload_with_retry(storage_path, file_bytes, mime_type)
        except Exception as exc:
            raise CourseServiceError(
                500,
                "STORAGE_UPLOAD_FAILED",
                f"Failed to upload file to storage: {exc}",
            ) from exc

        try:
            storage_url = self._build_storage_url(storage_path)
        except Exception as exc:
            raise CourseServiceError(
                500,
                "STORAGE_URL_FAILED",
                f"Failed to generate public URL: {exc}",
            ) from exc

        text_material = self._build_study_text_material(file_bytes, filename, mime_type, ext)
        is_text_type = mime_type in {"text/plain", "text/markdown"} or ext in {".txt", ".md"}

        record = {
            "id": material_id,
            "course_id": course_id,
            "user_id": user_id,
            "material": storage_url,
            "text_material": text_material,
            "is_text": is_text_type,
        }

        try:
            response = self.client.table("course_materials").insert(record).execute()
        except Exception as exc:
            try:
                self.client.storage.from_(STORAGE_BUCKET).remove([storage_path])
            except Exception:
                pass
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", f"Failed to save study file: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", "Study file upload returned no data")
        return self._serialize_material(data[0])

    def presign_study_upload(self, user_id: str, course_id: str, filename: str, mime_type: str) -> dict:
        """Return a signed upload URL so the browser can upload directly to Supabase Storage."""
        self._assert_course_owner(user_id, course_id)

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS and mime_type not in ALLOWED_MIME_TYPES:
            raise CourseServiceError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                f"File type '{ext or mime_type}' is not allowed. "
                f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )

        material_id = str(uuid.uuid4())
        storage_path = f"{course_id}/{material_id}{ext}"

        try:
            result = get_service_client().storage.from_(STORAGE_BUCKET).create_signed_upload_url(storage_path)
        except Exception as exc:
            raise CourseServiceError(500, "PRESIGN_FAILED", f"Could not generate upload URL: {exc}") from exc

        # Parse the signed URL from the various response shapes Supabase may return.
        signed_url: str | None = None
        if isinstance(result, dict):
            signed_url = (
                result.get("signedURL")
                or result.get("signedUrl")
                or result.get("signed_url")
            )
            if signed_url is None and isinstance(result.get("data"), dict):
                signed_url = (
                    result["data"].get("signedURL")
                    or result["data"].get("signedUrl")
                    or result["data"].get("signed_url")
                )
        elif isinstance(result, str):
            signed_url = result
        else:
            signed_url = (
                getattr(result, "signedURL", None)
                or getattr(result, "signedUrl", None)
                or getattr(result, "signed_url", None)
            )

        if not signed_url:
            raise CourseServiceError(500, "PRESIGN_FAILED", f"Unexpected response from storage: {result}")

        return {"storage_path": storage_path, "signed_url": self._normalize_storage_url(signed_url)}

    def confirm_study_upload(
        self,
        user_id: str,
        course_id: str,
        storage_path: str,
        filename: str,
        mime_type: str,
    ) -> CourseMaterial:
        """Record a browser-uploaded file in the DB (file is already in Supabase Storage)."""
        self._assert_course_owner(user_id, course_id)

        ext = os.path.splitext(filename)[1].lower()
        is_text_type = mime_type in {"text/plain", "text/markdown"} or ext in {".txt", ".md"}

        # The storage_path format is "{course_id}/{material_uuid}{ext}"; reuse that UUID as the DB id.
        basename = os.path.basename(storage_path)
        material_id = os.path.splitext(basename)[0]

        try:
            storage_url = self._build_storage_url(storage_path)
        except Exception as exc:
            raise CourseServiceError(500, "STORAGE_URL_FAILED", f"Failed to generate storage URL: {exc}") from exc

        text_material = f"filename={filename};mime={mime_type};note=binary_content_in_material"

        record = {
            "id": material_id,
            "course_id": course_id,
            "user_id": user_id,
            "material": storage_url,
            "text_material": text_material,
            "is_text": is_text_type,
        }

        try:
            response = self.client.table("course_materials").insert(record).execute()
        except Exception as exc:
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", f"Failed to save material: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "MATERIAL_CREATE_FAILED", "Material creation returned no data")
        return self._serialize_material(data[0])

    def add_practice_problem_file(
        self,
        user_id: str,
        course_id: str,
        question_bytes: bytes,
        question_filename: str,
        question_mime_type: str,
        answer_bytes: bytes | None = None,
        answer_filename: str | None = None,
        answer_mime_type: str | None = None,
    ) -> dict:
        """Create one past_problems row: question file required, answer file optional."""
        self._assert_course_owner(user_id, course_id)

        q_ext = os.path.splitext(question_filename)[1].lower()
        if q_ext not in ALLOWED_EXTENSIONS and question_mime_type not in ALLOWED_MIME_TYPES:
            raise CourseServiceError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                f"Question file type '{q_ext or question_mime_type}' is not allowed.",
            )

        problem_id = str(uuid.uuid4())
        question_storage_path = f"practice/{course_id}/{problem_id}/question{q_ext or '.bin'}"
        try:
            self._upload_with_retry(question_storage_path, question_bytes, question_mime_type)
        except Exception as exc:
            raise CourseServiceError(500, "STORAGE_UPLOAD_FAILED", f"Failed to upload question file: {exc}") from exc

        question_url = self._build_storage_url(question_storage_path)

        answer_url = None
        answer_storage_path = None
        if answer_bytes is not None and answer_filename:
            a_ext = os.path.splitext(answer_filename)[1].lower()
            answer_mime = answer_mime_type or "application/octet-stream"
            if a_ext not in ALLOWED_EXTENSIONS and answer_mime not in ALLOWED_MIME_TYPES:
                raise CourseServiceError(
                    415,
                    "UNSUPPORTED_FILE_TYPE",
                    f"Answer file type '{a_ext or answer_mime}' is not allowed.",
                )

            answer_storage_path = f"practice/{course_id}/{problem_id}/answer{a_ext or '.bin'}"
            try:
                self._upload_with_retry(answer_storage_path, answer_bytes, answer_mime)
            except Exception as exc:
                # Cleanup question upload if answer upload fails.
                try:
                    self.client.storage.from_(STORAGE_BUCKET).remove([question_storage_path])
                except Exception:
                    pass
                raise CourseServiceError(500, "STORAGE_UPLOAD_FAILED", f"Failed to upload answer file: {exc}") from exc

            answer_url = self._build_storage_url(answer_storage_path)

        record = {
            "id": problem_id,
            "course_id": course_id,
            "user_id": user_id,
            "question": question_url,
            "answer": answer_url,
        }
        try:
            response = self.client.table("past_problems").insert(record).execute()
        except Exception as exc:
            # Cleanup uploaded objects if DB insert fails.
            remove_paths = [question_storage_path]
            if answer_storage_path:
                remove_paths.append(answer_storage_path)
            try:
                self.client.storage.from_(STORAGE_BUCKET).remove(remove_paths)
            except Exception:
                pass
            raise CourseServiceError(500, "PAST_PROBLEM_CREATE_FAILED", f"Failed to save past problem: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "PAST_PROBLEM_CREATE_FAILED", "Past problem creation returned no data")
        return data[0]

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int) -> str:
        parts = (text or "").strip().split()
        if not parts:
            return ""
        return " ".join(parts[:max_tokens])

    @staticmethod
    def _is_boilerplate_ai_greeting(text: str) -> bool:
        normalized = " ".join((text or "").strip().lower().split())
        return normalized.startswith(
            "hello! i'm ready to help you practice. please feel free to ask your question"
        )

    @staticmethod
    def _is_redundant_ai_system_reply(text: str) -> bool:
        normalized = " ".join((text or "").strip().lower().split())
        blocked_messages = {
            "tutor is temporarily rate-limited. please wait a moment and try again.",
            "tutor service is temporarily unavailable. please try again shortly.",
            "tutor configuration error. please contact support.",
        }
        return normalized in blocked_messages

    def append_practice_llm_conversation(
        self,
        user_id: str,
        course_id: str,
        user_message: str,
        ai_response: str,
    ) -> None:
        if not course_id:
            return

        response = (
            self.client.table("courses")
            .select("id, llm_conversation")
            .eq("id", course_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        rows = response.data or []
        if not rows:
            raise CourseServiceError(404, "COURSE_NOT_FOUND", "Course not found")

        existing = rows[0].get("llm_conversation")
        conversation: list[dict[str, str]] = []
        if isinstance(existing, str):
            text = existing.strip()
            if text:
                try:
                    loaded = json.loads(text)
                    if isinstance(loaded, list):
                        conversation = [item for item in loaded if isinstance(item, dict)]
                except Exception:
                    conversation = []

        if self._is_boilerplate_ai_greeting(ai_response) or self._is_redundant_ai_system_reply(ai_response):
            return

        conversation.append(
            {
                "role": "user",
                "content": self._truncate_to_tokens(user_message, 20),
            }
        )
        conversation.append(
            {
                "role": "ai",
                "content": self._truncate_to_tokens(ai_response, 30),
            }
        )

        json_text = json.dumps(conversation, ensure_ascii=True)
        try:
            (
                self.client.table("courses")
                .update({"llm_conversation": json_text})
                .eq("id", course_id)
                .eq("user_id", user_id)
                .execute()
            )
        except Exception as exc:
            raise CourseServiceError(
                500,
                "COURSE_UPDATE_FAILED",
                f"Failed to save course llm conversation: {exc}",
            ) from exc

    def get_course_llm_conversation(self, user_id: str, course_id: str) -> list[dict[str, str]]:
        if not course_id:
            return []

        response = (
            self.client.table("courses")
            .select("id, llm_conversation")
            .eq("id", course_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        rows = response.data or []
        if not rows:
            return []

        raw = rows[0].get("llm_conversation")
        if not isinstance(raw, str):
            return []

        text = raw.strip()
        if not text:
            return []

        try:
            loaded = json.loads(text)
        except Exception:
            return []

        if not isinstance(loaded, list):
            return []

        return [item for item in loaded if isinstance(item, dict)]

    def get_practice_problem(self, user_id: str, problem_id: str) -> dict:
        response = (
            self.client.table("past_problems")
            .select("id, user_id, course_id, question, answer, created_at")
            .eq("id", problem_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        rows = response.data or []
        if not rows:
            raise CourseServiceError(404, "PAST_PROBLEM_NOT_FOUND", "Past problem not found")
        return rows[0]

    def list_materials(self, user_id: str, course_id: str) -> list[CourseMaterial]:
        self._assert_course_owner(user_id, course_id)
        response = (
            self.client.table("course_materials")
            .select("id, course_id, user_id, is_text, text_material, created_at")
            .eq("course_id", course_id)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [self._serialize_material(row) for row in (response.data or [])]

    def list_user_learning_preference_rows(self, user_id: str) -> list[dict]:
        response = (
            self.client.table("learning_preferences")
            .select("id, user_id, preference, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [row for row in (response.data or []) if isinstance(row, dict)]

    def add_user_learning_preference(self, user_id: str, preference: str) -> dict:
        cleaned = (preference or "").strip()
        if not cleaned:
            raise CourseServiceError(400, "INVALID_PREFERENCE", "Learning preference cannot be empty")

        record = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "preference": cleaned,
        }
        try:
            response = self.client.table("learning_preferences").insert(record).execute()
        except Exception as exc:
            raise CourseServiceError(500, "PREFERENCE_CREATE_FAILED", f"Failed to save learning preference: {exc}") from exc

        data = response.data or []
        if not data:
            raise CourseServiceError(500, "PREFERENCE_CREATE_FAILED", "Learning preference creation returned no data")
        created = data[0]
        if not isinstance(created, dict):
            raise CourseServiceError(500, "PREFERENCE_CREATE_FAILED", "Unexpected learning preference response payload")
        return created

    def get_user_learning_preferences(self, user_id: str) -> list[str]:
        preferences: list[str] = []

        def append_preference(value: str | None) -> None:
            text = (value or "").strip()
            if not text:
                return
            if any(existing.lower() == text.lower() for existing in preferences):
                return
            preferences.append(text)

        # Pull all explicit learning preferences saved for this user.
        pref_rows = self.list_user_learning_preference_rows(user_id)
        for row in pref_rows:
            append_preference(str(row.get("preference") or ""))

        # Include profile learning_type as an additional preference when present.
        profile_resp = (
            self.client.table("users")
            .select("id, learning_type")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        profile_rows = profile_resp.data or []
        if profile_rows:
            append_preference(profile_rows[0].get("learning_type"))

        return preferences

    def get_user_learning_preferences_detailed(self, user_id: str) -> list[dict]:
        rows = self.list_user_learning_preference_rows(user_id)
        items: list[dict] = []
        seen: set[str] = set()

        for row in rows:
            preference = str(row.get("preference") or "").strip()
            if not preference:
                continue
            key = preference.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "id": str(row.get("id") or ""),
                    "preference": preference,
                    "created_at": row.get("created_at"),
                    "source": "table",
                }
            )

        profile_resp = (
            self.client.table("users")
            .select("id, learning_type")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        profile_rows = profile_resp.data or []
        if profile_rows:
            learning_type = str(profile_rows[0].get("learning_type") or "").strip()
            key = learning_type.lower()
            if learning_type and key not in seen:
                items.append(
                    {
                        "id": f"profile:{user_id}",
                        "preference": learning_type,
                        "created_at": None,
                        "source": "profile_learning_type",
                    }
                )

        return items

    def get_user_learning_preference(self, user_id: str) -> str | None:
        preferences = self.get_user_learning_preferences(user_id)
        return preferences[0] if preferences else None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _assert_course_owner(self, user_id: str, course_id: str) -> None:
        response = (
            self.client.table("courses")
            .select("id")
            .eq("id", course_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not (response.data or []):
            raise CourseServiceError(404, "COURSE_NOT_FOUND", "Course not found")

    @staticmethod
    def _serialize_course(row: dict) -> Course:
        return Course(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            name=row["name"],
            details=row.get("details"),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _serialize_material(row: dict) -> CourseMaterial:
        storage_url = row.get("storage_url") or row.get("material")

        filename = row.get("filename")
        mime_type = row.get("mime_type")

        # Backward compatibility: some rows store filename/mime inside text_material metadata.
        text_material = row.get("text_material")
        if isinstance(text_material, str) and text_material.startswith("filename="):
            parts = text_material.split(";")
            parsed: dict[str, str] = {}
            for part in parts:
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                parsed[key.strip()] = value.strip()

            if not filename:
                filename = parsed.get("filename")
            if not mime_type:
                mime_type = parsed.get("mime")

        return CourseMaterial(
            id=str(row["id"]),
            course_id=str(row["course_id"]),
            user_id=str(row["user_id"]),
            is_text=row["is_text"],
            filename=filename,
            mime_type=mime_type,
            storage_url=storage_url,
            text_material=text_material,  # Preview/full content for txt
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _build_study_text_material(file_bytes: bytes, filename: str, mime_type: str, ext: str) -> str:
        """Return extracted text for text formats, otherwise store lightweight metadata."""
        is_text_type = mime_type in {"text/plain", "text/markdown"} or ext in {".txt", ".md"}
        if is_text_type:
            try:
                return file_bytes.decode("utf-8")[:MAX_TEXT_PREVIEW]
            except UnicodeDecodeError:
                return file_bytes.decode("latin-1")[:MAX_TEXT_PREVIEW]
        return f"filename={filename};mime={mime_type};note=binary_content_in_material"
