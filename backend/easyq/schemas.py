"""OpenAPI (Swagger) request/response models for the frontend, following design 7.3.1 / 7.3.2 / 7.3.3.

Request handling still validates in contracts.py/service.py; these models document the same contract and the
tests check real responses against them so the two cannot drift silently. Covered first (frontend request):
auth API-01~09 and entry API-18 · 19. API-10 (계정 수정) is a v0.9 feature and not implemented yet.
"""
from datetime import datetime
from typing import Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


# ---------------------------------------------------------------- common ----
class ConsentValue(Model):
    policy_version: str = Field(max_length=30, examples=['service-v1'])
    granted: bool


class AccountConsents(Model):
    SERVICE: Optional[ConsentValue] = None
    TRAINING: Optional[ConsentValue] = None


class User(Model):
    """7.3.1 User."""
    user_id: UUID
    email: str
    display_name: str
    role: Literal['MEMBER', 'ADMIN']
    status: Literal['PENDING_VERIFICATION', 'ACTIVE', 'SUSPENDED', 'DELETING']
    email_verified_at: Optional[datetime]
    consents: AccountConsents


class ErrorBody(Model):
    code: str = Field(description='8.4.2 error code, e.g. VALIDATION_ERROR, FORBIDDEN')
    message: str
    details: dict = Field(description='e.g. ACCOUNT_SUSPENDED → {"suspension_reason": "..."}')
    retryable: bool


class ErrorResponse(Model):
    request_id: UUID
    error: ErrorBody


class EmptyData(Model):
    pass


# ------------------------------------------------------------ API-01~09 ----
class SignupConsents(Model):
    SERVICE: ConsentValue = Field(description='필수, granted=true')
    TRAINING: Optional[ConsentValue] = Field(None, description='선택, 없으면 false로 기록')


class SignupRequest(Model):
    """API-01."""
    email: str = Field(max_length=254, examples=['member@example.com'])
    password: str = Field(min_length=8, max_length=20, description='8~20자, 영문 · 숫자 각 1자 이상')
    display_name: str = Field(min_length=1, max_length=50)
    consents: SignupConsents


class SignupData(Model):
    user_id: UUID
    status: Literal['PENDING_VERIFICATION']
    resend_available_at: datetime


class EmailRequest(Model):
    """API-02 · API-07."""
    email: str = Field(max_length=254)


class ResendData(Model):
    message: str


class VerifyRequest(Model):
    """API-03."""
    email: str = Field(max_length=254)
    code: str = Field(pattern=r'^[0-9]{6}$', examples=['482913'])


class VerifyData(Model):
    email_verified: Literal[True]
    next_action: Literal['LOGIN']


class LoginRequest(Model):
    """API-04."""
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AuthData(Model):
    """7.3.1 Auth. The session token travels only in the HttpOnly easyq_session cookie, never in the body."""
    user: User
    expires_at: datetime
    csrf_token: str = Field(description='메모리에만 두고 변경 요청의 X-CSRF-Token 헤더로 보낸다')


class CsrfData(Model):
    """API-06."""
    csrf_token: str


class PasswordResetRequest(Model):
    """API-08."""
    token: str = Field(min_length=1, max_length=200, description='메일 링크의 #token= 값')
    new_password: str = Field(min_length=8, max_length=20)


# --------------------------------------------------------- API-18 · 19 ----
class JoinPreview(Model):
    """7.3.1 JoinPreview (API-18)."""
    title: str
    host_display_name: str
    scheduled_at: datetime
    description: Optional[str]
    status: Literal['DRAFT', 'LIVE']
    participant_count: int
    capacity: int = Field(examples=[50])
    is_full: bool
    already_joined: bool = Field(description='로그인한 회원 · 기존 게스트면 true')


class ParticipationRequest(Model):
    """API-19. 회원은 join_code만 보낸다(계정 SERVICE 동의 사용). 게스트는 nickname과 service_consent가 필수다.
    동의는 true/false 또는 {policy_version, granted} 둘 다 받는다."""
    join_code: str = Field(min_length=5, max_length=5, description='대소문자 무관', examples=['K7Q2M'])
    nickname: Optional[str] = Field(None, min_length=1, max_length=20, description='게스트 필수')
    service_consent: Optional[Union[bool, ConsentValue]] = Field(None, description='게스트 필수(true). 이 강연의 SERVICE 동의를 철회한 회원이 다시 들어올 때도 필요')
    training_consent: Optional[Union[bool, ConsentValue]] = None


class Participation(Model):
    """7.3.1 Participation."""
    participant_id: UUID
    session_id: UUID
    kind: Literal['MEMBER', 'GUEST']
    nickname: Optional[str] = Field(description='게스트 닉네임, 회원은 display_name')
    session_status: Literal['DRAFT', 'LIVE']
    expires_at: datetime
    csrf_token: Optional[str] = Field(None, description='게스트 첫 입장에만(Set-Cookie와 함께)')


# --------------------------------------------------------- operation table ----
COOKIE = 'easyq_session 쿠키(HttpOnly)'
SPECS = {
    'API-01': {'request': SignupRequest, 'ok': {201: (SignupData, '접수(메일 도착은 보장하지 않음)')},
               'errors': {409: 'EMAIL_ALREADY_EXISTS', 422: 'VALIDATION_ERROR · CONSENT_REQUIRED', 429: 'RATE_LIMITED'}},
    'API-02': {'request': EmailRequest, 'ok': {202: (ResendData, '계정 유무와 관계없이 같은 응답')},
               'errors': {422: 'VALIDATION_ERROR', 429: 'RATE_LIMITED'}},
    'API-03': {'request': VerifyRequest, 'ok': {200: (VerifyData, '인증 완료(자동 로그인 아님)')},
               'errors': {422: 'EMAIL_VERIFICATION_INVALID · VALIDATION_ERROR', 429: 'EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED · RATE_LIMITED'}},
    'API-04': {'request': LoginRequest, 'ok': {201: (AuthData, '로그인. Set-Cookie: easyq_session')},
               'errors': {401: 'AUTH_INVALID_CREDENTIALS', 403: 'EMAIL_NOT_VERIFIED · ACCOUNT_SUSPENDED(details.suspension_reason)',
                          422: 'VALIDATION_ERROR', 429: 'RATE_LIMITED'}},
    'API-05': {'cookie': True, 'csrf': True, 'ok': {204: (None, '로그아웃. Set-Cookie로 쿠키 만료(이미 만료된 세션도 204)')},
               'errors': {403: 'FORBIDDEN(Origin · CSRF)'}},
    'API-06': {'cookie': True, 'client_header': True, 'ok': {200: (CsrfData, '새 CSRF 토큰')},
               'errors': {401: 'AUTH_REQUIRED', 403: 'FORBIDDEN(Origin · X-EasyQ-Client)'}},
    'API-07': {'request': EmailRequest, 'ok': {202: (EmptyData, '가입 여부와 관계없이 같은 응답')},
               'errors': {422: 'VALIDATION_ERROR', 429: 'RATE_LIMITED'}},
    'API-08': {'request': PasswordResetRequest, 'ok': {204: (None, '변경 완료, 그 계정의 모든 세션 폐기')},
               'errors': {422: 'RESET_TOKEN_INVALID · VALIDATION_ERROR', 429: 'RATE_LIMITED'}},
    'API-09': {'cookie': True, 'ok': {200: (User, '현재 회원과 계정 동의')},
               'errors': {401: 'AUTH_REQUIRED', 403: 'FORBIDDEN(게스트)'}},
    'API-18': {'ok': {200: (JoinPreview, '입장 전 미리보기. 참가를 만들지 않음')},
               'errors': {404: 'JOIN_CODE_INVALID', 410: 'JOIN_CODE_REVOKED', 409: 'SESSION_ENDED', 429: 'RATE_LIMITED'}},
    'API-19': {'request': ParticipationRequest, 'cookie_optional': True,
               'ok': {201: (Participation, '새 참가. 게스트는 Set-Cookie: easyq_session'), 200: (Participation, '기존 참가 재입장(정원과 무관)')},
               'errors': {403: 'CONSENT_REQUIRED', 404: 'JOIN_CODE_INVALID', 409: 'SESSION_ENDED · SESSION_FULL · SESSION_STATE_CONFLICT(자기 강연)',
                          410: 'JOIN_CODE_REVOKED', 422: 'VALIDATION_ERROR', 429: 'RATE_LIMITED',
                          503: 'AUTH_SERVICE_UNAVAILABLE(같은 요청을 다시 보내면 같은 참가자 재사용)'}},
}
MODELS = [ErrorResponse, User, SignupRequest, SignupData, EmailRequest, ResendData, VerifyRequest, VerifyData, LoginRequest,
          AuthData, CsrfData, EmptyData, PasswordResetRequest, JoinPreview, ParticipationRequest, Participation]


def components():
    """JSON Schemas for #/components/schemas, nested models included."""
    from pydantic.json_schema import models_json_schema
    _, schema = models_json_schema([(m, 'validation') for m in MODELS], ref_template='#/components/schemas/{model}')
    return schema.get('$defs', {})


def envelope(model):
    data = {'$ref': f'#/components/schemas/{model.__name__}'}
    return {'type': 'object', 'required': ['request_id', 'data'],
            'properties': {'request_id': {'type': 'string', 'format': 'uuid'}, 'data': data}}


def operation(aid, method):
    """OpenAPI operation fields for a documented API ID, or None to keep the generic description."""
    spec = SPECS.get(aid)
    if not spec:
        return None
    parameters = []
    if method != 'GET':
        parameters.append({'name': 'Origin', 'in': 'header', 'required': True, 'schema': {'type': 'string'},
                           'description': 'PUBLIC_ORIGIN과 정확히 같아야 함'})
    if spec.get('csrf'):
        parameters.append({'name': 'X-CSRF-Token', 'in': 'header', 'required': False, 'schema': {'type': 'string'},
                           'description': '유효한 세션이 있으면 필수'})
    if spec.get('client_header'):
        parameters.append({'name': 'X-EasyQ-Client', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'enum': ['web']}})
    result = {'parameters': parameters, 'responses': {}}
    if spec.get('cookie'):
        result['security'] = [{'cookieAuth': []}]
    elif spec.get('cookie_optional'):
        result['security'] = [{}, {'cookieAuth': []}]
    if spec.get('request'):
        result['requestBody'] = {'required': True, 'content': {'application/json': {
            'schema': {'$ref': f"#/components/schemas/{spec['request'].__name__}"}}}}
    for status, (model, description) in spec['ok'].items():
        result['responses'][str(status)] = {'description': description}
        if model is not None:
            result['responses'][str(status)]['content'] = {'application/json': {'schema': envelope(model)}}
    for status, codes in spec['errors'].items():
        result['responses'][str(status)] = {'description': codes, 'content': {'application/json': {
            'schema': {'$ref': '#/components/schemas/ErrorResponse'}}}}
    result['responses'].setdefault('503', {'description': 'SERVICE_UNAVAILABLE', 'content': {'application/json': {
        'schema': {'$ref': '#/components/schemas/ErrorResponse'}}}})
    return result
