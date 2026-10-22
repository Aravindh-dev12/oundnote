from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
from os import PathLike
import secrets
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from local_meeting_ai.domain.entities import Job
from local_meeting_ai.domain.enums import JobStatus, JobType
from local_meeting_ai.domain.errors import (
    CapabilityUnavailableError,
    NotFoundError,
    ValidationError,
)
from local_meeting_ai.domain.webhooks import (
    WEBHOOK_EVENT_CATALOG,
    WEBHOOK_EVENT_IDS,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookEvent,
)
from local_meeting_ai.infrastructure.database.webhooks import WebhookRepository
from local_meeting_ai.infrastructure.database.repositories import (
    JobRepository,
    SettingsRepository,
    SummaryRepository,
    TranscriptionRepository,
)
from local_meeting_ai.infrastructure.webhook_secrets import WebhookSecretStore

logger = logging.getLogger(__name__)

LIVE_SEGMENT_TTL_SECONDS = 120
LIVE_SESSION_TTL_SECONDS = 300
MAX_LIVE_AGENT_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class WebhookHttpResult:
    status_code: int
    body: bytes
    duration_ms: int
    headers: dict[str, str] = field(default_factory=dict)


async def _http_sender(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    allow_private_network: bool,
) -> WebhookHttpResult:
    del allow_private_network  # The caller validates the destination before each send.
    started = time.perf_counter()
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.post(
            url,
            content=body,
            headers=headers,
        )
    return WebhookHttpResult(
        status_code=response.status_code,
        body=response.content[:MAX_LIVE_AGENT_RESPONSE_BYTES],
        duration_ms=round((time.perf_counter() - started) * 1000),
        headers={key.lower(): value for key, value in response.headers.items()},
    )


def _event_envelope(event: WebhookEvent, content_level: str) -> dict[str, Any]:
    """Build a versioned CloudEvents envelope with conservative content filtering."""
    data = _sanitize_event_data(event.data, content_level)
    event_id = str(event.event_type).replace("_", "-")
    return {
        "specversion": "1.0",
        "id": event.id,
        "source": "oundnote://local-instance",
        "type": f"com.oundnote.{event_id}.v1",
        "time": event.occurred_at,
        "subject": event.subject,
        "datacontenttype": "application/json",
        "data": data,
    }


def _sanitize_event_data(value: Any, content_level: str) -> Any:
    if content_level not in {"metadata", "segments", "full"}:
        content_level = "metadata"

    def clean(item: Any, *, parent_key: str = "") -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                name = str(key)
                lowered = name.lower()
                if lowered in {
                    "local_path",
                    "filepath",
                    "file_path",
                    "secret",
                    "signing_secret",
                    "api_key",
                    "authorization",
                    "headers",
                }:
                    continue
                if lowered in {"content_markdown", "summary_markdown", "transcript_text"}:
                    if content_level != "full":
                        continue
                if lowered == "segments":
                    if content_level == "metadata":
                        result["segment_count"] = len(nested) if isinstance(nested, list) else 0
                        continue
                    if not isinstance(nested, list):
                        continue
                if lowered == "text" and content_level == "metadata" and parent_key in {
                    "segments",
                    "transcript",
                    "transcription",
                    "summary",
                    "notes",
                }:
                    continue
                if lowered in {"transcript", "transcription"} and content_level == "metadata":
                    if isinstance(nested, dict):
                        result[name] = {
                            k: clean(v, parent_key=name)
                            for k, v in nested.items()
                            if k in {"id", "segment_count", "status", "language"}
                        }
                        if (
                            "segment_count" not in result[name]
                            and isinstance(nested.get("segments"), list)
                        ):
                            result[name]["segment_count"] = len(nested["segments"])
                    else:
                        result[name] = clean(nested, parent_key=name)
                    continue
                if lowered in {"summary", "notes"} and content_level != "full":
                    if isinstance(nested, dict):
                        result[name] = {
                            k: clean(v, parent_key=name)
                            for k, v in nested.items()
                            if k in {"id", "status", "title", "template"}
                        }
                    elif isinstance(nested, str):
                        result[name] = {}
                    else:
                        result[name] = clean(nested, parent_key=name)
                    continue
                result[name] = clean(nested, parent_key=name)
            return result
        if isinstance(item, list):
            return [clean(nested, parent_key=parent_key) for nested in item]
        if isinstance(item, tuple):
            return [clean(nested, parent_key=parent_key) for nested in item]
        if isinstance(item, PathLike):
            return str(item)
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        if hasattr(item, "value"):
            return clean(item.value, parent_key=parent_key)
        return str(item)

    return clean(value)


class WebhookService:
    """Durable outbound webhook dispatcher.

    Publishing only appends an event and delivery rows to SQLite. Network I/O is
    isolated in a cancellable worker; a receiver cannot block recording or ASR.
    """

    def __init__(
        self,
        *,
        repository: WebhookRepository,
        preferences: SettingsRepository,
        secrets_store: WebhookSecretStore,
        meetings: Any,
        transcriptions: TranscriptionRepository,
        summaries: SummaryRepository,
        jobs: JobRepository,
        poll_interval: float = 1.0,
    ) -> None:
        self.repository = repository
        self.preferences = preferences
        self.secrets_store = secrets_store
        self.meetings = meetings
        self.transcriptions = transcriptions
        self.summaries = summaries
        self.jobs = jobs
        self.poll_interval = max(0.02, float(poll_interval))
        self.sender = _http_sender
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._delivery_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def settings(self) -> dict[str, Any]:
        saved = self.preferences.get_all().get("webhooks")
        saved = saved if isinstance(saved, dict) else {}
        return {
            "enabled": bool(saved.get("enabled", False)),
            "retention_days": max(1, min(365, int(saved.get("retention_days", 30)))),
            "max_concurrency": max(1, min(16, int(saved.get("max_concurrency", 4)))),
        }

    def catalog(self) -> dict[str, Any]:
        return {
            "settings": self.settings(),
            "secure_storage_available": self.secrets_store.available(),
            "event_catalog": [dict(item) for item in WEBHOOK_EVENT_CATALOG],
            "endpoints": [
                self.endpoint_payload(endpoint)
                for endpoint in self.repository.endpoints()
            ],
        }

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        clean = {
            "enabled": bool(values.get("enabled", False)),
            "retention_days": max(1, min(365, int(values.get("retention_days", 30)))),
            "max_concurrency": max(1, min(16, int(values.get("max_concurrency", 4)))),
        }
        self.preferences.update({"webhooks": clean})
        self.wake()
        return clean

    def endpoint_payload(self, endpoint: WebhookEndpoint) -> dict[str, Any]:
        payload = asdict(endpoint)
        payload["events"] = list(endpoint.events)
        payload["secret_configured"] = bool(self.secrets_store.get(endpoint.id))
        return payload

    def create_endpoint(self, values: dict[str, Any]) -> tuple[WebhookEndpoint, str]:
        clean = self._validate_endpoint_values(values)
        if not self.secrets_store.available():
            raise CapabilityUnavailableError(
                "Secure OS credential storage is unavailable for webhook signing"
            )
        endpoint = self.repository.create_endpoint(clean)
        signing_secret = secrets.token_urlsafe(36)
        try:
            self.secrets_store.set(endpoint.id, signing_secret)
        except Exception:
            self.repository.delete_endpoint(endpoint.id)
            raise
        self.wake()
        return endpoint, signing_secret

    def update_endpoint(self, endpoint_id: str, values: dict[str, Any]) -> WebhookEndpoint:
        existing = self.repository.endpoint(endpoint_id)
        if existing is None:
            raise NotFoundError("Webhook endpoint not found")
        clean = self._validate_endpoint_values(values, existing=existing)
        updated = self.repository.update_endpoint(endpoint_id, clean)
        if updated is None:
            raise NotFoundError("Webhook endpoint not found")
        self.wake()
        return updated

    def delete_endpoint(self, endpoint_id: str) -> None:
        if self.repository.endpoint(endpoint_id) is None:
            raise NotFoundError("Webhook endpoint not found")
        self.repository.delete_endpoint(endpoint_id)
        self.secrets_store.delete(endpoint_id)

    def rotate_secret(self, endpoint_id: str) -> str:
        if self.repository.endpoint(endpoint_id) is None:
            raise NotFoundError("Webhook endpoint not found")
        if not self.secrets_store.available():
            raise CapabilityUnavailableError("Secure OS credential storage is unavailable")
        signing_secret = secrets.token_urlsafe(36)
        self.secrets_store.set(endpoint_id, signing_secret)
        return signing_secret

    def test_endpoint(self, endpoint_id: str) -> WebhookEvent:
        endpoint = self.repository.endpoint(endpoint_id)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found")
        return self._enqueue(
            "webhook.test",
            {"test": True, "endpoint": {"id": endpoint.id, "name": endpoint.name}},
            endpoint_ids=[endpoint.id],
            subject=f"endpoint/{endpoint.id}",
            meeting_id=None,
            transcription_id=None,
            expires_seconds=300,
            allow_unlisted=True,
        )

    def publish(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        meeting_id: int | None = None,
        transcription_id: int | None = None,
        subject: str | None = None,
    ) -> WebhookEvent | None:
        if event_type not in WEBHOOK_EVENT_IDS:
            return None
        if not self.settings()["enabled"]:
            return None
        endpoints = self.repository.subscribed_endpoints(event_type)
        if not endpoints:
            return None
        inferred_meeting_id = meeting_id
        meeting_data = data.get("meeting") if isinstance(data, dict) else None
        if inferred_meeting_id is None and isinstance(meeting_data, dict):
            candidate = meeting_data.get("id")
            if isinstance(candidate, int):
                inferred_meeting_id = candidate
        transcript_data = data.get("transcription") if isinstance(data, dict) else None
        if transcription_id is None and isinstance(transcript_data, dict):
            candidate = transcript_data.get("id")
            if isinstance(candidate, int):
                transcription_id = candidate
        event_subject = subject or (
            f"meeting/{inferred_meeting_id}" if inferred_meeting_id is not None else "system"
        )
        ttl = LIVE_SEGMENT_TTL_SECONDS if event_type == "live.segment.batch" else (
            LIVE_SESSION_TTL_SECONDS if event_type.startswith("live.session.") else None
        )
        event = self._enqueue(
            event_type,
            data,
            endpoint_ids=[endpoint.id for endpoint in endpoints],
            subject=event_subject,
            meeting_id=inferred_meeting_id,
            transcription_id=transcription_id,
            expires_seconds=ttl,
        )
        return event

    def publish_live_session(self, event_type: str, session: Any) -> WebhookEvent | None:
        source_items = getattr(session, "sources", ()) or (getattr(session, "source", None),)
        sources = [
            {
                "id": getattr(item, "id", None),
                "name": getattr(item, "name", None),
                "kind": getattr(item, "kind", None),
            }
            for item in source_items
            if item is not None
        ]
        payload = {
            "meeting": {
                "id": getattr(session, "meeting_id", None),
                "title": getattr(session, "title", None),
            },
            "transcription": {"id": getattr(session, "transcription_id", None)},
            "session": {
                "id": getattr(session, "session_id", None),
                "state": getattr(session, "state", None),
                "elapsed_ms": getattr(session, "elapsed_ms", None),
                "status": getattr(session, "realtime_status", None),
                "error": getattr(session, "capture_error", None),
                "sources": sources,
            },
        }
        return self.publish(
            event_type,
            payload,
            meeting_id=getattr(session, "meeting_id", None),
            transcription_id=getattr(session, "transcription_id", None),
        )

    def publish_live_segments(
        self,
        *,
        meeting_id: int,
        transcription_id: int,
        meeting_title: str,
        segments: list[Any],
        sequence: int,
    ) -> WebhookEvent | None:
        payload = {
            "meeting": {"id": meeting_id, "title": meeting_title},
            "transcription": {"id": transcription_id},
            "sequence": sequence,
            "segments": [
                {
                    "id": f"live-{transcription_id}-{getattr(segment, 'index', index)}",
                    "index": getattr(segment, "index", index),
                    "start_ms": getattr(segment, "start_ms", 0),
                    "end_ms": getattr(segment, "end_ms", 0),
                    "text": getattr(segment, "text", ""),
                    "confidence": getattr(segment, "confidence", None),
                }
                for index, segment in enumerate(segments)
            ],
        }
        return self.publish(
            "live.segment.batch",
            payload,
            meeting_id=meeting_id,
            transcription_id=transcription_id,
        )

    def publish_job_terminal(self, job: Job, status: JobStatus) -> None:
        terminal = status.value if hasattr(status, "value") else str(status)
        if terminal not in {"completed", "failed", "cancelled"}:
            return
        event_type: str | None = None
        if job.job_type == JobType.IMPORT_MEDIA:
            event_type = "recording.ready" if terminal == "completed" else "recording.failed"
        elif job.job_type == JobType.TRANSCRIBE:
            event_type = f"transcription.final.{terminal}"
        elif job.job_type == JobType.DIARIZE:
            event_type = (
                "diarization.completed"
                if terminal == "completed"
                else f"diarization.{terminal}"
            )
        elif job.job_type == JobType.SUMMARIZE:
            event_type = "summary.completed" if terminal == "completed" else f"summary.{terminal}"
        if event_type not in WEBHOOK_EVENT_IDS:
            return
        meeting_id = job.meeting_id
        meeting = self.meetings.get(meeting_id) if meeting_id is not None else None
        transcription_id = job.payload.get("transcription_id")
        transcription_id = transcription_id if isinstance(transcription_id, int) else None
        meeting_payload = (
            {"id": meeting.id, "title": meeting.title, "status": str(meeting.status)}
            if meeting is not None else {"id": meeting_id}
        )
        payload: dict[str, Any] = {
            "meeting": meeting_payload,
            "job": {
                "id": job.uuid,
                "type": job.job_type.value,
                "status": terminal,
                "progress": job.progress,
                "message": job.message,
                "error": job.error_text,
                "result": job.result,
            },
        }
        if transcription_id is not None:
            transcript = self.transcriptions.get(transcription_id)
            if transcript is not None:
                payload["transcription"] = {
                    "id": transcript.id,
                    "title": transcript.title,
                    "status": transcript.status,
                    "language": transcript.language,
                }
                if terminal == "completed" and event_type == "transcription.final.completed":
                    payload["transcript"] = {
                        "id": transcript.id,
                        "segments": [
                            {
                                "id": segment.id,
                                "index": segment.segment_index,
                                "start_ms": segment.start_ms,
                                "end_ms": segment.end_ms,
                                "text": segment.text,
                            }
                            for segment in self.transcriptions.segments(transcription_id)
                        ],
                    }
        self.publish(
            event_type,
            payload,
            meeting_id=meeting_id,
            transcription_id=transcription_id,
        )

    def publish_pipeline_finished(
        self,
        *,
        meeting_id: int | None,
        transcription_id: int,
        pipeline_id: str | None,
        status: str,
        options: dict[str, Any],
    ) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            return
        meeting = self.meetings.get(meeting_id) if meeting_id is not None else None
        payload = {
            "meeting": {"id": meeting.id, "title": meeting.title, "status": str(meeting.status)}
            if meeting is not None else {"id": meeting_id},
            "transcription": {"id": transcription_id},
            "processing": {
                "pipeline_id": pipeline_id,
                "status": status,
                "options": options,
            },
        }
        self.publish(
            "meeting.processing.completed",
            payload,
            meeting_id=meeting_id,
            transcription_id=transcription_id,
        )

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self.repository.recover_delivering()
        self._wake_event = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self._dispatch_loop(), name="oundnote-webhook-dispatcher"
        )
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        worker = self._worker_task
        self._worker_task = None
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        pending = list(self._delivery_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def wake(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake_event.set)

    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            try:
                settings = self.settings()
                if settings["enabled"]:
                    self.repository.purge(settings["retention_days"])
                    capacity = max(0, settings["max_concurrency"] - len(self._delivery_tasks))
                    if capacity:
                        due = self.repository.claim_due(limit=capacity)
                        for delivery in due:
                            task = asyncio.create_task(
                                self._deliver(delivery),
                                name=f"oundnote-webhook-{delivery.id}",
                            )
                            self._delivery_tasks.add(task)
                            task.add_done_callback(self._delivery_tasks.discard)
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    pass
                self._wake_event.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Webhook dispatcher iteration failed")
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    pass
                self._wake_event.clear()

    async def _deliver(self, delivery: WebhookDelivery) -> None:
        event = self.repository.event(delivery.event_id)
        endpoint = self.repository.endpoint(delivery.endpoint_id)
        if event is None or endpoint is None:
            self.repository.finish_delivery(
                delivery.id, status="failed", status_code=None,
                error="Webhook event or endpoint no longer exists",
                duration_ms=0, response_excerpt=None,
            )
            return
        secret = self.secrets_store.get(endpoint.id)
        if not secret:
            self.repository.finish_delivery(
                delivery.id, status="failed", status_code=None,
                error="Webhook signing secret is unavailable",
                duration_ms=0, response_excerpt=None,
            )
            return

        body = json.dumps(
            _event_envelope(event, endpoint.content_level),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/cloudevents+json",
            "X-Oundnote-Event": event.event_type,
            "X-Oundnote-Delivery": delivery.id,
            "X-Oundnote-Timestamp": timestamp,
            "X-Oundnote-Signature-256": f"sha256={signature}",
        }
        started = time.perf_counter()
        try:
            await _assert_safe_destination(endpoint.url, endpoint.allow_private_network)
            result = await self.sender(
                endpoint.url,
                body,
                headers,
                endpoint.timeout_seconds,
                endpoint.allow_private_network,
            )
            status_code = result.status_code
            excerpt = result.body[:4000].decode("utf-8", errors="replace")
            duration = result.duration_ms or round((time.perf_counter() - started) * 1000)
            if 200 <= status_code < 300:
                self.repository.finish_delivery(
                    delivery.id, status="delivered", status_code=status_code,
                    error=None, duration_ms=duration, response_excerpt=excerpt,
                )
                if endpoint.mode == "live_agent" and event.event_type == "live.segment.batch":
                    await self._store_live_agent_suggestions(endpoint, event, result.body)
                return
            retryable = status_code in {408, 425, 429} or 500 <= status_code <= 599
            self._finish_failed_attempt(
                delivery,
                endpoint,
                status_code=status_code,
                error=f"Remote returned HTTP {status_code}",
                duration_ms=duration, excerpt=excerpt, retryable=retryable,
                headers=result.headers,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            duration = round((time.perf_counter() - started) * 1000)
            self._finish_failed_attempt(
                delivery, endpoint, status_code=None,
                error=str(error) or type(error).__name__,
                duration_ms=duration, excerpt=None, retryable=True,
            )

    def _finish_failed_attempt(
        self,
        delivery: WebhookDelivery,
        endpoint: WebhookEndpoint,
        *,
        status_code: int | None,
        error: str,
        duration_ms: int,
        excerpt: str | None,
        retryable: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        if retryable and delivery.attempt_count < endpoint.max_attempts:
            retry_after = _retry_after_seconds((headers or {}).get("retry-after"))
            delay = retry_after if retry_after is not None else min(
                3600,
                2 ** max(0, delivery.attempt_count - 1) * 5,
            )
            next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            self.repository.finish_delivery(
                delivery.id, status="retry", status_code=status_code,
                error=error, duration_ms=duration_ms, response_excerpt=excerpt,
                next_attempt_at=next_attempt,
            )
        else:
            self.repository.finish_delivery(
                delivery.id, status="failed", status_code=status_code,
                error=error, duration_ms=duration_ms, response_excerpt=excerpt,
            )

    async def _store_live_agent_suggestions(
        self,
        endpoint: WebhookEndpoint,
        event: WebhookEvent,
        body: bytes,
    ) -> None:
        if len(body) > MAX_LIVE_AGENT_RESPONSE_BYTES:
            logger.warning("Webhook live-agent response from %s exceeded size limit", endpoint.id)
            return
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Webhook live-agent response from %s was not valid JSON", endpoint.id)
            return
        if not isinstance(parsed, dict):
            return
        suggestions = parsed.get("suggestions")
        if not isinstance(suggestions, list):
            return
        accepted: list[dict[str, Any]] = []
        for suggestion in suggestions[:10]:
            if not isinstance(suggestion, dict):
                continue
            text_value = str(suggestion.get("text") or "").strip()
            if not text_value:
                continue
            kind = str(suggestion.get("kind") or suggestion.get("type") or "insight")
            confidence = suggestion.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                confidence = None
            related = suggestion.get("related_segment_ids")
            accepted.append({
                "type": kind[:40],
                "text": text_value[:2000],
                "confidence": (
                    max(0.0, min(float(confidence), 1.0))
                    if confidence is not None
                    else None
                ),
                "related_segment_ids": (
                    [str(value) for value in related[:20]]
                    if isinstance(related, list)
                    else []
                ),
            })
        if accepted:
            self.repository.add_insights(endpoint=endpoint, event=event, suggestions=accepted)

    def _enqueue(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        endpoint_ids: list[str],
        subject: str,
        meeting_id: int | None,
        transcription_id: int | None,
        expires_seconds: int | None,
        allow_unlisted: bool = False,
    ) -> WebhookEvent:
        if not allow_unlisted and event_type not in WEBHOOK_EVENT_IDS:
            raise ValidationError(f"Unknown webhook event: {event_type}")
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=expires_seconds)).isoformat()
            if expires_seconds is not None else None
        )
        event = self.repository.enqueue_event(
            event_type=event_type,
            subject=subject,
            meeting_id=meeting_id,
            transcription_id=transcription_id,
            data=data,
            endpoint_ids=endpoint_ids,
            expires_at=expires_at,
        )
        if event is None:
            raise ValidationError("No webhook destination subscribed to this event")
        self.wake()
        return event

    def _validate_endpoint_values(
        self,
        values: dict[str, Any],
        *,
        existing: WebhookEndpoint | None = None,
    ) -> dict[str, Any]:
        merged = {
            "name": existing.name if existing else "",
            "url": existing.url if existing else "",
            "enabled": existing.enabled if existing else False,
            "mode": existing.mode if existing else "notification",
            "events": list(existing.events) if existing else [],
            "content_level": existing.content_level if existing else "metadata",
            "timeout_seconds": existing.timeout_seconds if existing else 10,
            "max_attempts": existing.max_attempts if existing else 4,
            "allow_private_network": existing.allow_private_network if existing else False,
            **values,
        }
        name = str(merged["name"]).strip()
        if not name:
            raise ValidationError("Webhook endpoint name is required")
        url = str(merged["url"]).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValidationError("Webhook URL must be an absolute HTTP or HTTPS URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValidationError("Webhook URLs cannot contain credentials or fragments")
        allow_private = bool(merged["allow_private_network"])
        hostname = parsed.hostname.lower().rstrip(".")
        is_loopback_name = hostname == "localhost" or hostname.endswith(".localhost")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        is_loopback = is_loopback_name or bool(address and address.is_loopback)
        if parsed.scheme != "https" and not is_loopback and not allow_private:
            raise ValidationError("Public webhook URLs must use HTTPS")
        events = merged["events"]
        if not isinstance(events, (list, tuple)) or not events:
            raise ValidationError("Subscribe to at least one webhook event")
        clean_events = sorted({str(item) for item in events})
        for event_name in clean_events:
            if event_name not in WEBHOOK_EVENT_IDS:
                raise ValidationError(f"Unknown webhook event: {event_name}")
        if merged["mode"] not in {"notification", "live_agent"}:
            raise ValidationError("Invalid webhook mode")
        if merged["content_level"] not in {"metadata", "segments", "full"}:
            raise ValidationError("Invalid webhook content level")
        return {
            "name": name,
            "url": url,
            "enabled": bool(merged["enabled"]),
            "mode": merged["mode"],
            "events": clean_events,
            "content_level": merged["content_level"],
            "timeout_seconds": max(1.0, min(30.0, float(merged["timeout_seconds"]))),
            "max_attempts": max(1, min(10, int(merged["max_attempts"]))),
            "allow_private_network": allow_private,
        }


async def _assert_safe_destination(url: str, allow_private_network: bool) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise ValidationError("Webhook URL has no hostname")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            0,
            socket.SOCK_STREAM,
        )
        addresses = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
    if not addresses:
        raise ValidationError("Webhook hostname did not resolve to an address")
    for address in addresses:
        if address.is_loopback:
            continue
        if not allow_private_network and (
            address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValidationError("Webhook destination resolves to a private or reserved address")


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, min(3600, int(value.strip())))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            delta = (parsed - datetime.now(UTC)).total_seconds()
            return max(0, min(3600, int(delta)))
        except (TypeError, ValueError, OverflowError):
            return None
