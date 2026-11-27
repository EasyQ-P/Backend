"""Claude adapter (LLM_ONLY judgment, answer generation, summary) and api_usage reservation for LLM calls (8.2.31 · 8.2.34).

The anthropic SDK is imported lazily so the worker image and the tests do not need it until a call is made. Tests inject a
fake with the same two methods (complete / stream). Prompts, answers and transcripts are never logged.
"""
from decimal import Decimal, InvalidOperation
import time

from easyq_common.contracts import APIError, obj, uid

from .budget import month_bounds


class LLMResult(dict):
    """text, model, input_tokens, output_tokens, stop_reason."""


class AnthropicLLM:
    """Messages API through the official SDK. Low effort keeps first-token latency small for live questions."""

    def __init__(self, settings):
        self.settings = settings
        self._client = None

    def client(self):
        if not self.settings.anthropic_api_key:
            raise APIError(503, 'PROVIDER_FAILURE')
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key, max_retries=1, timeout=60)
        return self._client

    @staticmethod
    def _result(message, text):
        usage = getattr(message, 'usage', None)
        return LLMResult(text=text, model=getattr(message, 'model', None), stop_reason=getattr(message, 'stop_reason', None),
                         input_tokens=getattr(usage, 'input_tokens', None), output_tokens=getattr(usage, 'output_tokens', None))

    def complete(self, model, system, prompt, max_tokens):
        message = self.client().messages.create(model=model, max_tokens=max_tokens, system=system,
            output_config={'effort': 'low'}, messages=[{'role': 'user', 'content': prompt}])
        if message.stop_reason == 'refusal':
            raise APIError(503, 'PROVIDER_FAILURE')
        return self._result(message, ''.join(b.text for b in message.content if b.type == 'text'))

    def stream(self, model, system, prompt, max_tokens, on_text):
        """Call on_text(chunk) for each text delta; on_text returns False to stop early (insufficient evidence)."""
        parts = []
        with self.client().messages.stream(model=model, max_tokens=max_tokens, system=system,
                output_config={'effort': 'low'}, messages=[{'role': 'user', 'content': prompt}]) as stream:
            for chunk in stream.text_stream:
                parts.append(chunk)
                if on_text(chunk) is False:
                    return LLMResult(text=''.join(parts), model=model, stop_reason='stopped', input_tokens=None, output_tokens=None)
            message = stream.get_final_message()
        if message.stop_reason == 'refusal':
            raise APIError(503, 'PROVIDER_FAILURE')
        return self._result(message, ''.join(parts))


def _decimal(value):
    try:
        number = Decimal(str(value)) if value not in (None, '') else None
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() and number >= 0 else None


def llm_cost(settings, input_tokens, output_tokens):
    """USD estimate from the configured price table; None when prices are unknown."""
    price_in, price_out = _decimal(settings.llm_input_usd_per_mtok), _decimal(settings.llm_output_usd_per_mtok)
    if price_in is None or price_out is None or input_tokens is None or output_tokens is None:
        return None
    return (price_in * input_tokens + price_out * output_tokens) / Decimal(1_000_000)


def reserve_llm(service, operation, model, prompt_chars, max_tokens, session_id=None, question_id=None, request_id=None):
    """8.2.34: lock MONTHLY_API_BUDGET, check used + reserved + this call's maximum against the cap, then record a
    RESERVED row. Same rule as budget.reserve (embeddings); prompt characters bound the input tokens from above."""
    settings = service.settings
    maximum = llm_cost(settings, prompt_chars + 1000, max_tokens)
    usage_id = uid()
    with service.db.transaction() as tx:
        setting = tx.one("SELECT * FROM app_settings WHERE setting_key='MONTHLY_API_BUDGET' FOR UPDATE")
        if setting and setting['hard_cap_enabled']:
            try:
                config = obj(setting['value'])
                limit, rate = Decimal(str(config['monthly_budget_krw'])), Decimal(str(config['usd_krw_rate']))
                if not limit.is_finite() or not rate.is_finite() or rate <= 0 or maximum is None or not settings.llm_price_version:
                    raise ValueError()
            except (KeyError, ValueError, TypeError, InvalidOperation):
                raise APIError(409, 'BUDGET_EXCEEDED') from None
            start, end = month_bounds(service.clock())
            rows = tx.all("""SELECT currency,estimated_cost FROM api_usage WHERE created_at>=%s AND created_at<%s
                AND operation IN ('EMBEDDING','JUDGE_LLM','ANSWER','SUMMARY')""", (start, end))
            used = Decimal(0)
            for row in rows:
                if row['estimated_cost'] is None or row['currency'] not in ('USD', 'KRW'):
                    raise APIError(409, 'BUDGET_EXCEEDED')
                used += Decimal(str(row['estimated_cost'])) * (rate if row['currency'] == 'USD' else 1)
            if used >= limit or used + maximum * rate > limit:
                raise APIError(409, 'BUDGET_EXCEEDED')
        tx.execute("""INSERT INTO api_usage(usage_id,session_id,question_id,provider,model_name,operation,
            estimated_cost,currency,price_version,latency_ms,outcome,request_id)
            VALUES(%s,%s,%s,'ANTHROPIC',%s,%s,%s,'USD',%s,0,'RESERVED',%s)""",
            (usage_id, session_id, question_id, model or 'NOT_CONFIGURED', operation,
             str(maximum) if maximum is not None else None, settings.llm_price_version or None, request_id or uid()))
    return usage_id


def settle_llm(service, usage_id, outcome, began, result=None):
    """SUCCESS stores the provider-reported tokens and the actual estimate; failures keep the reservation (8.2.34)."""
    latency = max(0, int((time.monotonic() - began) * 1000))
    with service.db.transaction() as tx:
        if outcome == 'SUCCESS' and result is not None:
            cost = llm_cost(service.settings, result.get('input_tokens'), result.get('output_tokens'))
            tx.execute("""UPDATE api_usage SET outcome='SUCCESS',latency_ms=%s,input_tokens=%s,output_tokens=%s,
                estimated_cost=COALESCE(%s,estimated_cost),model_name=COALESCE(%s,model_name) WHERE usage_id=%s""",
                (latency, result.get('input_tokens'), result.get('output_tokens'), str(cost) if cost is not None else None,
                 result.get('model'), usage_id))
        else:
            tx.execute('UPDATE api_usage SET outcome=%s,latency_ms=%s WHERE usage_id=%s', (outcome, latency, usage_id))


def call_llm(service, llm, operation, model, system, prompt, max_tokens, session_id=None, question_id=None,
             request_id=None, on_text=None):
    """Reserve → call (streaming when on_text is given) → settle. BUDGET_EXCEEDED is raised before any call."""
    usage_id = reserve_llm(service, operation, model, len(system) + len(prompt), max_tokens, session_id, question_id, request_id)
    began = time.monotonic()
    try:
        if on_text is None:
            result = llm.complete(model, system, prompt, max_tokens)
        else:
            result = llm.stream(model, system, prompt, max_tokens, on_text)
    except APIError as exc:
        settle_llm(service, usage_id, 'ERROR', began)
        if exc.code == 'LEASE_LOST':
            raise
        raise APIError(503, 'PROVIDER_FAILURE') from None
    except Exception as exc:
        settle_llm(service, usage_id, 'TIMEOUT' if 'timeout' in type(exc).__name__.lower() else 'ERROR', began)
        raise APIError(503, 'PROVIDER_FAILURE') from None
    settle_llm(service, usage_id, 'SUCCESS', began, result)
    return result
