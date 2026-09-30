"""Rate limiting and abuse protection for Telegram integration."""
import time
import logging

_logger = logging.getLogger(__name__)

# Simple in-memory sliding window (process-local)
# Key: chat_id -> list of timestamps (float)
_RATE_WINDOW = {}
# Key: chat_id -> count of concurrent jobs (via session state)
_CONCURRENT = {}

DEFAULT_MAX_PER_MINUTE = 20
DEFAULT_MAX_CONCURRENT = 5


def _get_windows():
    return _RATE_WINDOW

def _prune(chat_id, window_seconds=60):
    now = time.time()
    lst = _RATE_WINDOW.get(chat_id, [])
    lst = [t for t in lst if now - t < window_seconds]
    _RATE_WINDOW[chat_id] = lst
    return lst

def check_rate_limit(chat_id, max_per_minute=None, env=None):
    """Check if chat_id is within rate limit.

    Uses DB query when env is available to avoid cross-test in-memory
    accumulation (TransactionCase rolls back DB but not memory). Falls back
    to in-memory sliding window when env is not available.

    :return: (allowed: bool, remaining: int)
    """
    if env is not None:
        try:
            val = env['ir.config_parameter'].sudo().get_param(
                'telegram_odoo_integration.rate_limit_per_minute', '')
            if val:
                max_per_minute = int(val)
        except Exception:
            pass
    limit = max_per_minute or DEFAULT_MAX_PER_MINUTE

    # Prefer DB-based counting when env is available – avoids
    # cross-test pollution from in-memory global.
    if env is not None:
        try:
            from datetime import datetime, timedelta
            cutoff = datetime.now() - timedelta(minutes=1)
            count = env['telegram.message'].sudo().search_count([
                ('chat_id', '=', str(chat_id)),
                ('create_date', '>=', cutoff),
            ])
            allowed = count < limit
            if not allowed:
                _logger.warning('Rate limit hit for chat %s (%d/min) db_count=%d', chat_id, limit, count)
            return allowed, max(0, limit - count - (1 if allowed else 0))
        except Exception:
            pass
    # Fallback to in-memory
    lst = _prune(chat_id)
    allowed = len(lst) < limit
    if allowed:
        lst.append(time.time())
        _RATE_WINDOW[chat_id] = lst
    else:
        _logger.warning('Rate limit hit for chat %s (%d/min)', chat_id, limit)
    return allowed, max(0, limit - len(lst))

def check_concurrent(chat_id, max_concurrent=None, env=None):
    """Check concurrent jobs via DB query if env provided, otherwise memory."""
    limit = max_concurrent
    if env is not None:
        try:
            val = env['ir.config_parameter'].sudo().get_param(
                'telegram_odoo_integration.max_concurrent_jobs', '')
            if val:
                limit = int(val)
            else:
                limit = DEFAULT_MAX_CONCURRENT
        except Exception:
            limit = DEFAULT_MAX_CONCURRENT
    else:
        limit = limit or DEFAULT_MAX_CONCURRENT

    if env is not None:
        try:
            count = env['telegram.order.session'].sudo().search_count([
                ('chat_id', '=', str(chat_id)),
                ('state', 'in', ('submitted', 'processing')),
            ])
            allowed = count < limit
            return allowed, count, limit
        except Exception:
            pass
    # fallback to memory
    cnt = _CONCURRENT.get(chat_id, 0)
    allowed = cnt < limit
    return allowed, cnt, limit

def reset(chat_id=None):
    """Reset counters (for testing)."""
    if chat_id:
        _RATE_WINDOW.pop(chat_id, None)
        _CONCURRENT.pop(chat_id, None)
    else:
        _RATE_WINDOW.clear()
        _CONCURRENT.clear()
