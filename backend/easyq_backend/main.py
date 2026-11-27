"""backend container entry point.  uvicorn --factory easyq_backend.main:create_app"""
from contextlib import asynccontextmanager
import asyncio

from easyq_common.contracts import APIError, uuid_value
from easyq_common.http import build_app

from . import admin, deletion, notices, questions, reports, training
from .live import EventHub, LiveConnection, StarletteSocket

ROUTES = [
    ('POST', '/me/consents', 'API-11', '계정 동의'),
    ('GET', '/sessions', 'API-14', '내 강연 · 보관함 강연 목록'),
    ('POST', '/sessions', 'API-13', '강연 생성'),
    ('GET', '/sessions/{sid}', 'API-15', '상태와 현재 권한'),
    ('PATCH', '/sessions/{sid}', 'API-16', '강연 수정/시작/종료'),
    ('POST', '/sessions/{sid}/join-links', 'API-17', '입장 QR 재발급'),
    ('GET', '/join-codes/{code}', 'API-18', '입장 전 강연 확인'),
    ('POST', '/participations', 'API-19', '회원/게스트 입장'),
    ('POST', '/sessions/{sid}/consents', 'API-20', '강연 동의'),
    ('POST', '/sessions/{sid}/documents', 'API-21', '교안 접수'),
    ('GET', '/sessions/{sid}/documents', 'API-22', '교안 처리 상태'),
    ('POST', '/sessions/{sid}/documents/{did}/retries', 'API-23', '교안 처리 재시도'),
    ('PUT', '/sessions/{sid}/terms', 'API-25', '전문용어 사전 저장'),
    ('GET', '/jobs/{job_id}', 'API-41', '작업 상태'),
]
# Domain modules list their own routes (OPENAPI) next to the handlers; Swagger shows them in API number order.
ROUTES += questions.OPENAPI + reports.OPENAPI + deletion.OPENAPI + notices.OPENAPI + admin.OPENAPI + training.OPENAPI
ROUTES.sort(key=lambda route: int(route[2].split('-')[1]))


def create_app(service=None, stt_factory=None):
    if service is None:
        from easyq_common.auth_client import AuthClient
        from easyq_common.config import Settings
        from easyq_common.db import Database
        from easyq_common.storage import storage_from
        from .service import BackendService
        settings = Settings.from_env()
        service = BackendService(Database(settings.database_url), settings, storage_from(settings), AuthClient(settings))
    if stt_factory is None:
        from easyq_common.replay import RecordedSTT, store_from
        from .stt import GeminiLiveSTT
        stt_factory = GeminiLiveSTT(service.settings)
        store = store_from(service.settings)   # PROVIDER_MODE=record|replay (easyq_common.replay)
        if store is not None:
            stt_factory = RecordedSTT(stt_factory, store, service.settings.provider_stt_recording)
    hub = EventHub(service)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(hub.run())
        yield
        task.cancel()

    def configure(app):
        from fastapi import WebSocket

        @app.websocket('/api/v1/ws/sessions/{sid}')
        async def live(websocket: WebSocket, sid: str):
            # 7.3.10: same-site HttpOnly cookie, Origin, lecture permission are checked before accept.
            try:
                sid = uuid_value(sid)
            except APIError:
                await websocket.close(1008)
                return
            headers = {k.lower(): v for k, v in websocket.headers.items()}
            await LiveConnection(service, hub, stt_factory, StarletteSocket(websocket), sid, headers,
                                 dict(websocket.cookies)).run()

    return build_app('backend', service, ROUTES, lifespan=lifespan, configure=configure)
