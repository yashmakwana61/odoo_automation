"""Translation layer: internal errors -> user-friendly messages.

Never exposes stack traces, credentials, or internal codes to the user.
Logs preserve the original error for debugging.
"""
import logging

_logger = logging.getLogger(__name__)

# Map known internal error codes / substrings to friendly messages
ERROR_MAP = {
    'RES-002': "I couldn't identify one of the products in this order.",
    'RES-003': "I couldn't confidently identify the customer.",
    'OCR-001': "I couldn't read the uploaded document. Please upload a clearer copy.",
    'OCR-': "I couldn't read the uploaded document. Please try a clearer image or PDF.",
    'TIMEOUT': "Order processing is temporarily slow. Please wait a moment and check /status.",
    'UNAVAILABLE': "Order processing is temporarily unavailable. Please try again shortly.",
    'ODOO unavailable': "Order processing is temporarily unavailable. Please try again shortly.",
    'PARSER unavailable': "Order processing is temporarily unavailable. Please try again shortly.",
    'Connection': "Order processing is temporarily unavailable. Please try again shortly.",
    'Unauthorized': "This account is not authorized to submit orders.",
    'UNAUTHORIZED': "This account is not authorized to submit orders.",
    'RATE_LIMIT': "You're sending orders too quickly. Please wait a moment and try again.",
    'FILE_TOO_LARGE': "The file is too large. Please send a smaller file.",
    'UNSUPPORTED': "This file type is not supported. Please send JPEG, PNG, PDF, or Excel.",
    'EMPTY': "Your message was empty. Please send the order details.",
    'EMPTY_MESSAGE': "Your message was empty. Please send the order details.",
    'DUPLICATE': "This order was already received and is being processed.",
}

# Parser error classifications that are retryable (transient)
TRANSIENT_KEYWORDS = {
    'timeout', 'temporarily', 'unavailable', 'connection', '503', '502', '504',
    'retry', 'rate limit', '429',
}

PERMANENT_KEYWORDS = {
    'validation', 'invalid', 'unsupported', 'not found', '400', '422',
}


def translate_error(error_msg, error_code=None, log_context=None):
    """Translate an internal error into a user-friendly message.

    :param error_msg: raw internal error string
    :param error_code: optional structured error code (e.g. RES-002)
    :param log_context: dict with trace info for logging
    :return: user-friendly string
    """
    raw = str(error_msg or error_code or 'Unknown error')
    # Log original for observability
    if log_context:
        _logger.warning('Translating error [%s] context=%s', raw, log_context)
    else:
        _logger.warning('Translating error: %s', raw)

    # Direct code match
    if error_code and error_code in ERROR_MAP:
        return ERROR_MAP[error_code]
    # Substring match for known codes
    for key, friendly in ERROR_MAP.items():
        if key.lower() in raw.lower():
            return friendly
    # Generic fallback - never leak internal details
    return (
        "I couldn't process this order due to a temporary issue. "
        "Please try again shortly. If the problem persists, contact your administrator."
    )


def classify_retry(error_msg, error_code=None, http_status=None):
    """Classify error as transient (retry) vs permanent.

    :return: 'transient' or 'permanent'
    """
    raw = (str(error_msg or '') + ' ' + str(error_code or '')).lower()
    status = str(http_status or '')
    combined = raw + ' ' + status

    if any(k in combined for k in TRANSIENT_KEYWORDS):
        return 'transient'
    if any(k in combined for k in PERMANENT_KEYWORDS):
        return 'permanent'
    # 5xx => transient, 4xx => permanent
    if status.startswith('5'):
        return 'transient'
    if status.startswith('4'):
        return 'permanent'
    return 'permanent'


def unauthorized_message():
    return '\u26a0\ufe0f This Telegram account is not authorized to submit orders.\n\nPlease contact your administrator.'

def not_authorized_chat_message():
    return 'You are not authorized to place orders through this bot.'

def file_type_error_message(mime):
    return (
        'File type "%s" is not supported.\n'
        'Allowed: JPEG, PNG, WebP, PDF, Excel (.xlsx, .xls).' % mime
    )

def file_too_large_message(size_mb, max_mb):
    return (
        'File is too large (%.1f MB). Maximum allowed: %d MB.'
        % (size_mb, max_mb)
    )

def empty_message():
    return 'Please send your order details.'

def rate_limited_message():
    return (
        '\u23f3 You\'re sending messages too quickly. '
        'Please wait a moment and try again.'
    )

def too_many_jobs_message():
    return (
        '\u26a0\ufe0f You have too many orders processing at once. '
        'Please wait for existing orders to complete.'
    )
