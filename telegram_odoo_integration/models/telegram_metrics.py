"""Metrics and structured logging for Telegram integration.

Uses simple counters via ir.config_parameter + Python logging.
If prometheus or other metrics backend exists, this can be extended.
"""
import logging
import time

_logger = logging.getLogger(__name__)

# In-memory counters (process-local). For persistent metrics, expose via DB.
_COUNTERS = {}


def inc(counter_name, amount=1):
    """Increment a named counter."""
    _COUNTERS[counter_name] = _COUNTERS.get(counter_name, 0) + amount
    _logger.debug('Metric %s = %s', counter_name, _COUNTERS[counter_name])
    return _COUNTERS[counter_name]


def get_counter(counter_name):
    return _COUNTERS.get(counter_name, 0)


def get_all_counters():
    return dict(_COUNTERS)


def log_event(event, **kwargs):
    """Structured log for observability.

    Logs fields: event, telegram_chat_id, telegram_user_id, message_id,
    job_id, odoo_order_id, result, timestamp
    Never logs tokens or secrets.
    """
    # Scrub sensitive keys
    scrub_keys = {'token', 'api_key', 'password', 'secret', 'authorization', 'bot_token'}
    safe_kwargs = {}
    for k, v in kwargs.items():
        if k.lower() in scrub_keys:
            safe_kwargs[k] = '***'
        else:
            safe_kwargs[k] = v
    _logger.info('telegram_event=%s data=%s timestamp=%s', event, safe_kwargs, time.strftime('%Y-%m-%dT%H:%M:%SZ'))


def observe_processing_seconds(start_time):
    """Return elapsed seconds since start_time."""
    return time.time() - start_time
