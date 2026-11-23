"""Conservative monthly reservations using the supplied app_settings/api_usage."""
from datetime import timezone
from decimal import Decimal, InvalidOperation
from .contracts import APIError, obj, uid


def month_bounds(moment):
    start = moment.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def reserve(service, job):
    moment, s = service.clock(), service.settings
    try:
        maximum = Decimal(s.embedding_max_batch_cost_usd) if s.embedding_max_batch_cost_usd else None
    except InvalidOperation:
        maximum = None
    if maximum is not None and (not maximum.is_finite() or maximum < 0):
        maximum = None
    usage_id = uid()
    with service.db.transaction() as tx:
        setting = tx.one("SELECT * FROM app_settings WHERE setting_key='MONTHLY_API_BUDGET' FOR UPDATE")
        if setting and setting['hard_cap_enabled']:
            config = obj(setting['value'])
            try:
                limit, rate = Decimal(str(config['monthly_budget_krw'])), Decimal(str(config['usd_krw_rate']))
                if not limit.is_finite() or not rate.is_finite() or rate <= 0 or maximum is None or not s.embedding_price_version:
                    raise ValueError()
            except (KeyError, ValueError, InvalidOperation):
                raise APIError(409, 'BUDGET_EXCEEDED') from None
            start, end = month_bounds(moment)
            rows = tx.all("""SELECT currency,estimated_cost FROM api_usage WHERE created_at>=%s AND created_at<%s
                AND operation IN ('EMBEDDING','JUDGE_LLM','ANSWER','SUMMARY')""", (start, end))
            used = Decimal(0)
            for row in rows:
                if row['estimated_cost'] is None or row['currency'] not in ('USD', 'KRW'):
                    raise APIError(409, 'BUDGET_EXCEEDED')
                used += Decimal(str(row['estimated_cost'])) * (rate if row['currency'] == 'USD' else 1)
            if used + maximum * rate > limit or used >= limit:
                raise APIError(409, 'BUDGET_EXCEEDED')
        tx.execute("""INSERT INTO api_usage(usage_id,session_id,provider,model_name,operation,
            estimated_cost,currency,price_version,latency_ms,outcome,request_id)
            VALUES(%s,%s,'GOOGLE',%s,'EMBEDDING',%s,'USD',%s,0,'RESERVED',%s)""",
            (usage_id, job['session_id'], s.embedding_model or 'NOT_CONFIGURED', str(maximum) if maximum is not None else None,
             s.embedding_price_version or None, job['job_id']))
    return usage_id


def settle(service, usage_id, success, latency_ms):
    with service.db.transaction() as tx:
        tx.execute('UPDATE api_usage SET outcome=%s,latency_ms=%s WHERE usage_id=%s',
            ('SUCCESS' if success else 'ERROR', latency_ms, usage_id))
