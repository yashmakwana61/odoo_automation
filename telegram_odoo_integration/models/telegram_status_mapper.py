"""Telegram-facing status mapping for AI Parser job states.

Maps internal parser states to user-friendly Telegram states.
Supports both new job-based API (RECEIVED ... COMPLETED) and
legacy session API (PROCESSING, SUCCESS, etc.).
"""

PARSER_TO_TELEGRAM_STATE = {
    # New job state machine
    'RECEIVED': 'processing',
    'QUEUED': 'processing',
    'PROCESSING': 'processing',
    'EXTRACTING': 'processing',
    'NORMALIZING': 'processing',
    'VALIDATING': 'processing',
    'RESOLVING': 'processing',
    'READY_FOR_ODOO': 'creating_order',
    'SENT_TO_ODOO': 'creating_order',
    'COMPLETED': 'completed',
    'NEEDS_REVIEW': 'needs_review',
    'RETRYING': 'retrying',
    'FAILED': 'failed',
    # Legacy states (backward compat)
    'SUCCESS': 'completed',
    'INCOMPLETE': 'needs_review',
    'REQUIRES_REVIEW': 'needs_review',
}

TELEGRAM_STATE_LABELS = {
    'processing': 'Processing',
    'creating_order': 'Creating order',
    'completed': 'Completed',
    'needs_review': 'Needs review',
    'retrying': 'Retrying',
    'failed': 'Failed',
    'received': 'Received',
}

TELEGRAM_STATE_EMOJI = {
    'processing': '\u23f3',
    'creating_order': '\U0001f504',
    'completed': '\u2705',
    'needs_review': '\u26a0\ufe0f',
    'retrying': '\u23f3',
    'failed': '\u274c',
    'received': '\u2705',
}

# User-friendly message templates
USER_MESSAGES = {
    'processing': '\u23f3 Processing your order...\nReference: {ref}',
    'creating_order': '\U0001f504 Creating your order in Odoo...\nReference: {ref}',
    'retrying': (
        '\u23f3 We\'re still processing your order.\n\n'
        'There was a temporary processing issue. '
        'The system is retrying automatically.\n\n'
        'Reference:\n{ref}'
    ),
}


def map_parser_state(parser_state):
    """Map a raw parser state to telegram state key."""
    if not parser_state:
        return 'processing'
    key = str(parser_state).strip().upper()
    return PARSER_TO_TELEGRAM_STATE.get(key, 'processing')


def get_telegram_label(parser_state):
    """Human-readable label for a parser state."""
    t_state = map_parser_state(parser_state)
    return TELEGRAM_STATE_LABELS.get(t_state, t_state)


def get_emoji(parser_state):
    """Emoji for a parser state."""
    t_state = map_parser_state(parser_state)
    return TELEGRAM_STATE_EMOJI.get(t_state, '\u23f3')


def is_terminal(parser_state):
    """Return True if parser state is terminal (no further polling needed)."""
    terminal = {'COMPLETED', 'FAILED', 'NEEDS_REVIEW',
                'SUCCESS', 'INCOMPLETE', 'REQUIRES_REVIEW'}
    return str(parser_state).upper() in terminal if parser_state else False


def format_ack_message(job_id, short_ref=None):
    """Format instant acknowledgement after job creation."""
    ref = short_ref or job_id[:8] if job_id else 'N/A'
    return (
        '\u2705 Order received.\n\n'
        'I\'m processing your order now.\n'
        'Reference: %s' % ref
    )


def format_completed(order_name, customer_name=None, item_count=None,
                     tally_status=None):
    """Format completed order message from Odoo data."""
    lines = ['\u2705 Order created successfully.', '']
    if order_name:
        lines.append('Order: %s' % order_name)
    if customer_name:
        lines.append('Customer: %s' % customer_name)
    if item_count is not None:
        lines.append('Items: %s' % item_count)
    lines.append('Status: Created')
    if tally_status:
        tally_map = {
            'not_ready': 'Not Ready',
            'ready': 'Ready',
            'syncing': 'Syncing',
            'synced': 'Synced',
            'failed': 'Failed',
            'pending': 'Pending',
        }
        display = tally_map.get(str(tally_status).lower(), str(tally_status))
        lines.append('\U0001f9fe Tally: %s' % display)
    return '\n'.join(lines)


def format_incomplete(order_name, missing_fields=None):
    """Format incomplete order (created but missing info)."""
    lines = ['\u2705 Order created.', '']
    if order_name:
        lines.append('Order: %s' % order_name)
        lines.append('')
    if missing_fields:
        if isinstance(missing_fields, (list, tuple)):
            fields_str = '\n'.join('\u2022 %s' % f for f in missing_fields)
        else:
            fields_str = str(missing_fields)
        lines.append('\u26a0\ufe0f The order is missing:')
        lines.append(fields_str)
        lines.append('')
        lines.append('Please update the order in Odoo before it is sent for downstream processing.')
    else:
        lines.append('\u26a0\ufe0f The order needs additional information. Please review in Odoo.')
    return '\n'.join(lines)


def format_needs_review(job_id, reason=None, suggested_customer=None,
                        short_ref=None):
    """Format NEEDS_REVIEW message."""
    ref = short_ref or (job_id[:8] if job_id else 'N/A')
    lines = [
        '\u26a0\ufe0f Your order was received, but it needs a quick review.',
        '',
    ]
    if reason:
        lines.append('Reason:')
        lines.append(reason)
        lines.append('')
    elif suggested_customer:
        lines.append('Reason:')
        lines.append("I couldn't confidently identify the customer.")
        lines.append('')
    else:
        lines.append('Reason:')
        lines.append('One or more items need review.')
        lines.append('')
    if suggested_customer:
        lines.append('Suggested customer:')
        lines.append(str(suggested_customer))
        lines.append('')
    lines.append('Please review the order in Odoo.')
    lines.append('')
    lines.append('Reference: %s' % ref)
    return '\n'.join(lines)


def format_failed(job_id, short_ref=None):
    """Format permanent failure."""
    ref = short_ref or (job_id[:8] if job_id else 'N/A')
    return (
        '\u274c I couldn\'t process this order.\n\n'
        'Please check that the order contains:\n'
        '\u2022 Customer name\n'
        '\u2022 Product\n'
        '\u2022 Quantity\n\n'
        'Then send it again.\n\n'
        'Reference:\n%s' % ref
    )


def format_status_list(jobs):
    """Format a list of jobs for /status display.

    jobs: list of dicts with keys: ref, state, order_name
    """
    if not jobs:
        return 'No recent orders found.'
    lines = ['\U0001f4cb Your recent orders', '']
    for j in jobs:
        ref = j.get('ref') or j.get('job_id', 'N/A')[:8]
        state = j.get('state', 'processing')
        t_state = map_parser_state(state)
        emoji = TELEGRAM_STATE_EMOJI.get(t_state, '\u23f3')
        label = TELEGRAM_STATE_LABELS.get(t_state, state)
        lines.append('%s' % ref)
        lines.append('%s %s' % (emoji, label))
        if j.get('order_name'):
            lines.append(j['order_name'])
        lines.append('')
    return '\n'.join(lines).strip()
