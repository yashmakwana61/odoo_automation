"""Client for communicating with the independent AI Parser application.

This module handles sending order packages (messages + attachments) from
the Telegram session to the external AI Parser and processing responses.

The AI Parser is a SEPARATE APPLICATION. This client only handles HTTP
communication — no AI/OCR logic belongs here.
"""
import logging
import time

import requests

_logger = logging.getLogger(__name__)

CONFIG_AI_PARSER_URL = 'telegram_odoo_integration.ai_parser_url'
CONFIG_AI_PARSER_API_KEY = 'telegram_odoo_integration.ai_parser_api_key'

ATTACHMENT_SERVE_BASE = '/ai_parser/attachment/'
ATTACHMENT_TOKEN_TTL_SECONDS = 900  # 15 minutes


class AIParserClient:
    """Communicates with the external AI Parser application."""

    def __init__(self, env):
        self.env = env

    def get_parser_url(self):
        """Return the AI Parser base URL."""
        return self.env['ir.config_parameter'].sudo().get_param(
            CONFIG_AI_PARSER_URL, '').strip().rstrip('/')

    def get_api_key(self):
        """Return the API key for the AI Parser."""
        return self.env['ir.config_parameter'].sudo().get_param(
            CONFIG_AI_PARSER_API_KEY, '').strip()

    def _get_attachment_base_url(self):
        """Return the base URL for serving attachments to the AI Parser."""
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '').strip().rstrip('/')
        return base_url

    def submit_order(self, session):
        """Send a complete order package to the AI Parser.

        Supports new job API (POST /jobs or POST /parse) with idempotency,
        falls back to legacy /api/v1/parse-order.
        :param session: telegram.order.session recordset (single record)
        :return: dict with 'status'/'job_id' keys at minimum
        :raises: requests.RequestException on network errors
        """
        parser_url = self.get_parser_url()
        if not parser_url:
            raise ValueError(
                'AI Parser URL is not configured. '
                'Set it in Settings > Telegram > AI Parser URL.'
            )

        api_key = self.get_api_key()
        base_url = self._get_attachment_base_url()

        payload = self._build_request_payload(session, base_url)

        headers = {
            'Content-Type': 'application/json',
        }
        if api_key:
            headers['Authorization'] = 'Bearer %s' % api_key
        # Idempotency header using session idempotency_key or request_id
        idem_key = getattr(session, 'idempotency_key', None) or session.request_id
        if idem_key:
            headers['Idempotency-Key'] = str(idem_key)
            headers['X-Idempotency-Key'] = str(idem_key)

        # Try new job endpoints first: /jobs, /parse, then legacy
        endpoints = [
            '%s/jobs' % parser_url,
            '%s/parse' % parser_url,
            '%s/api/v1/parse-order' % parser_url,
            '%s/api/parse' % parser_url,
        ]
        last_exc = None
        for endpoint in endpoints:
            try:
                _logger.info(
                    'Submitting session %s to AI Parser at %s '
                    '(request_id=%s, job_id_hint=%s, messages=%d, attachments=%d)',
                    session.session_id, endpoint, payload.get('request_id'),
                    payload.get('idempotency_key'), len(payload['messages']), len(payload['attachments']))
                response = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=120,
                )
                if response.status_code == 404:
                    _logger.debug('Endpoint %s not found, trying next', endpoint)
                    continue
                response.raise_for_status()
                data = response.json()
                # Normalize response shape
                if isinstance(data, dict):
                    # Ensure status key exists
                    if 'status' not in data and 'state' in data:
                        data['status'] = data['state']
                    if 'job_id' not in data and 'id' in data:
                        data['job_id'] = data['id']
                    if 'job_id' not in data and 'jobId' in data:
                        data['job_id'] = data['jobId']
                return data
            except requests.RequestException as exc:
                # If 404, try next endpoint; otherwise raise or retry
                resp = getattr(exc, 'response', None)
                status = getattr(resp, 'status_code', None) if resp is not None else None
                if status == 404:
                    last_exc = exc
                    continue
                # Transient? retry once for 5xx
                if status and 500 <= status < 600:
                    _logger.warning('Transient error on %s: %s, retrying once', endpoint, exc)
                    try:
                        time.sleep(1)
                        response = requests.post(endpoint, json=payload, headers=headers, timeout=120)
                        response.raise_for_status()
                        return response.json()
                    except Exception:
                        raise
                raise
        if last_exc:
            raise last_exc
        raise ValueError('No AI Parser endpoint available at %s' % parser_url)

    def query_status(self, request_id):
        """Query the AI Parser for the status of a previous request/job.

        Tries GET /jobs/{id}, then legacy /api/v1/status/{id}.
        :param request_id: job_id or request_id
        :return: dict with 'status' key
        """
        parser_url = self.get_parser_url()
        if not parser_url:
            raise ValueError('AI Parser URL is not configured.')

        api_key = self.get_api_key()
        headers = {}
        if api_key:
            headers['Authorization'] = 'Bearer %s' % api_key

        endpoints = [
            '%s/jobs/%s' % (parser_url, request_id),
            '%s/jobs/%s/status' % (parser_url, request_id),
            '%s/api/v1/status/%s' % (parser_url, request_id),
            '%s/parse/%s' % (parser_url, request_id),
        ]
        last_exc = None
        for endpoint in endpoints:
            try:
                response = requests.get(
                    endpoint, headers=headers, timeout=30)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    if 'status' not in data and 'state' in data:
                        data['status'] = data['state']
                    if 'job_id' not in data and 'id' in data:
                        data['job_id'] = data['id']
                return data
            except requests.RequestException as exc:
                resp = getattr(exc, 'response', None)
                status = getattr(resp, 'status_code', None) if resp is not None else None
                if status == 404:
                    last_exc = exc
                    continue
                raise
        if last_exc:
            raise last_exc
        raise ValueError('Job %s not found on AI Parser at %s' % (request_id, parser_url))

    def check_health(self):
        """Check parser health/readiness. Returns dict with status."""
        parser_url = self.get_parser_url()
        if not parser_url:
            return {'status': 'not_configured'}
        api_key = self.get_api_key()
        headers = {}
        if api_key:
            headers['Authorization'] = 'Bearer %s' % api_key
        for path in ['/health', '/readiness', '/healthz', '/api/health', '/metrics']:
            try:
                resp = requests.get('%s%s' % (parser_url, path), headers=headers, timeout=10)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception:
                        return {'status': 'ok', 'path': path}
            except Exception:
                continue
        return {'status': 'unknown'}

    def _build_request_payload(self, session, base_url):
        """Build the JSON payload for the AI Parser."""
        messages = []
        for msg in session.message_ids.sorted(lambda m: m.sequence):
            messages.append({
                'text': msg.message_text,
                'sequence': msg.sequence,
            })

        attachments = []
        for att in session.attachment_ids.sorted(lambda a: a.sequence):
            secure_url = '%s%s%s' % (
                base_url, ATTACHMENT_SERVE_BASE, att.secure_token)
            attachments.append({
                'filename': att.filename,
                'mime_type': att.mime_type,
                'file_size': att.file_size,
                'file_hash': att.file_hash,
                'download_url': secure_url,
                'secure_token': att.secure_token,
            })

        return {
            'request_id': session.request_id,
            'idempotency_key': getattr(session, 'idempotency_key', None) or session.request_id,
            'job_id': getattr(session, 'parser_job_id', None),
            'session_id': session.session_id,
            'staff_id': session.staff_id.id,
            'staff_name': session.telegram_username or session.staff_id.name,
            'channel': 'telegram',
            'chat_id': session.chat_id,
            'telegram_username': session.telegram_username,
            'messages': messages,
            'attachments': attachments,
            'metadata': {
                'source': 'telegram',
                'telegram_username': session.telegram_username,
                'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            },
        }
