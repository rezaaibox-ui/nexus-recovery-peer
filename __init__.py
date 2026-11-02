"""Nexus bounded independent diagnosis after deterministic tool loops or a failed delegated child.

This plugin never replays a tool or mutates the parent agent's model/provider. A second
provider diagnoses the failure through Hermes' permission-gated ctx.llm facade; its
advice is appended to the original result for the working agent to act on and verify.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger('plugins.nexus-recovery-peer')
_ctx: Any = None
_lock = threading.RLock()
_states: dict[str, dict[str, Any]] = {}
_READ_ONLY = frozenset({'read_file', 'search_files', 'web_search', 'web_extract',
                        'tool_search', 'tool_describe', 'session_search', 'browser_snapshot'})
_FAILURE_STATUSES = frozenset({'error', 'failed', 'blocked', 'rejected', 'max_iterations', 'timeout'})
_EXPLICIT_BLOCKED = re.compile(
    r"(?i)^(?:unable to\b|i (?:can't|cannot|couldn't|could not|am unable|was unable)\b|"
    r"blocked\b|نتوانستم\b|نمی(?:\u200c|-)?توانم\b|موفق نشدم\b)"
)
_REDACTIONS = (
    re.compile(r'(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)'),
    re.compile(r'''(?i)\b(?:authorization\s*:\s*)?bearer\s+[^\s,;"']+'''),
    re.compile(r'''(?i)\b(?:[a-z0-9]+[_-])*(?:password|api[_-]?key|secret|token)(?:[_-][a-z0-9]+)*\s*["']?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;"']+)'''),
    re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{25,}|ghp_[A-Za-z0-9_]{20,})\b'),
)


def _redact(text: Any, limit: int) -> str:
    output = str(text or '')
    for pat in _REDACTIONS:
        output = pat.sub('[REDACTED]', output)
    return output[:limit]



def _peer_route(model: str) -> tuple[str, str, str] | None:
    get_config = getattr(_ctx, 'get_config', None)
    routes = get_config('routes', default=[]) if callable(get_config) else []
    if not isinstance(routes, list):
        return None
    for route in routes:
        if not isinstance(route, dict):
            continue
        match = str(route.get('model_contains') or '').lower()
        origin = str(route.get('origin_provider') or '').strip()
        provider = str(route.get('peer_provider') or '').strip()
        peer_model = str(route.get('peer_model') or '').strip()
        if match and match in (model or '').lower() and origin and provider and peer_model and provider != origin:
            return provider, peer_model, origin
    return None


def _state(sid: str, turn_id: str = '') -> dict[str, Any]:
    with _lock:
        now = time.monotonic()
        if len(_states) > 128:
            for key, value in list(_states.items()):
                if now - value['touched'] > 1800:
                    _states.pop(key, None)
        state = _states.get(sid)
        if state is None or (turn_id and state.get('turn_id') and state['turn_id'] != turn_id):
            state = {'turn_id': turn_id, 'model': '', 'task': '', 'last_sig': '',
                     'streak': 0, 'peer_used': False, 'touched': now}
            _states[sid] = state
        if turn_id:
            state['turn_id'] = turn_id
        state['touched'] = now
        return state


def on_pre_llm_call(*, session_id: str = '', turn_id: str = '', model: str = '',
                    user_message: str = '', **_: Any) -> None:
    if not session_id:
        return
    state = _state(session_id, turn_id)
    with _lock:
        if model:
            state['model'] = model
        if user_message and not str(user_message).startswith('[hermes note:'):
            state['task'] = _redact(user_message, 1000)


def _delegate_failure(result: Any) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return None, ''
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        return None, ''
    for entry in data['results']:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get('status') or '').lower()
        if status in _FAILURE_STATUSES:
            return data, _redact(entry.get('error') or entry.get('summary') or status, 350)
        summary = str(entry.get('summary') or '').strip()
        if status == 'completed' and _EXPLICIT_BLOCKED.match(summary):
            return data, _redact(summary, 350)
    return None, ''


def _signature(name: str, args: Any, result: Any, status: str) -> str:
    serialized = json.dumps([name, args, result, status], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _ask_peer(state: dict[str, Any], *, tool: str, failure: str, task: str) -> dict[str, str] | None:
    ctx = _ctx
    llm = getattr(ctx, 'llm', None) if ctx is not None else None
    if llm is None:
        return None
    has_capability = getattr(ctx, 'has_capability', None)
    if not callable(has_capability) or not all(has_capability(cap) for cap in
            ('llm.provider_override', 'llm.model_override')):
        return None
    route = _peer_route(state.get('model', ''))
    if route is None:
        return None
    provider, model, origin = route
    prompt = ('Task: ' + _redact(task or state.get('task') or '(task not available)', 1000)
              + '\nTool: ' + tool
              + '\nObserved failure/no-progress: ' + _redact(failure, 350))
    try:
        answer = llm.complete(
            messages=[
                {'role': 'system', 'content': ('You are an independent recovery reviewer. Do not claim the task is solved. '
                    'Give a DIFFERENT concrete next step (at most two) and how to verify it. '
                    'Do not repeat the failed call unchanged. Never request or print credentials. '
                    'Do not take actions or propose irreversible actions without human approval.')},
                {'role': 'user', 'content': prompt},
            ], provider=provider, model=model, max_tokens=220, temperature=0.0, timeout=18,
        )
        actual = str(getattr(answer, 'provider', '') or '')
        suggestion = _redact(getattr(answer, 'text', ''), 1200).strip()
        if not suggestion or not actual or actual != provider or actual == origin:
            logger.warning('nexus-recovery-peer: no independent response (tool=%s, actual_provider=%s)', tool, actual)
            return None
        logger.info('nexus-recovery-peer: independent diagnostic tool=%s provider=%s model=%s', tool, actual, getattr(answer, 'model', model))
        return {'provider': actual, 'model': str(getattr(answer, 'model', model)),
                'suggestion': suggestion, 'verified': 'no; working agent must execute and check'}
    except Exception as exc:
        logger.warning('nexus-recovery-peer: diagnostic unavailable tool=%s error_type=%s', tool, type(exc).__name__)
        return None


def on_transform_tool_result(*, tool_name: str = '', args: Any = None, result: Any = None,
                             session_id: str = '', turn_id: str = '', status: str = '',
                             error_type: str | None = None, error_message: str | None = None,
                             **_: Any) -> str | None:
    """Observe one completed call; never replay it. Original error and status are retained."""
    if not session_id or not tool_name or result is None:
        return None
    state = _state(session_id, turn_id)
    delegate, child_error = _delegate_failure(result) if tool_name == 'delegate_task' else (None, '')
    is_error = status == 'error' or bool(error_type) or (isinstance(result, str) and result.startswith('Error'))
    if delegate is not None:
        category = 'delegated child failed'
        tasks = args.get('tasks') if isinstance(args, dict) else None
        child_goal = (tasks[0].get('goal') if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else '')
        task = str(child_goal or state.get('task') or '')
        failure = child_error
    elif is_error:
        category = 'repeated tool failure'
        task = state.get('task') or ''
        failure = _redact(error_message or result, 350)
    elif tool_name in _READ_ONLY:
        category = 'identical read-only result without progress'
        task = state.get('task') or ''
        failure = 'The same read-only call returned the same output repeatedly; change the query or use existing evidence.'
    else:
        with _lock:
            state['last_sig'], state['streak'] = '', 0
        return None
    signature = _signature(tool_name, args, result, category)
    with _lock:
        if signature == state['last_sig']:
            state['streak'] += 1
        else:
            state['last_sig'], state['streak'] = signature, 1
        threshold = 1 if delegate is not None else 3
        if state['peer_used'] or state['streak'] < threshold:
            return None
        state['peer_used'] = True  # reserve before network call to bound concurrent tool batches
        snapshot = {'model': state.get('model', ''), 'task': state.get('task', '')}
    review = _ask_peer(snapshot, tool=tool_name, failure=f'{category}: {failure}', task=task)
    if review is None:
        return None  # fail open; the original result and built-in guardrail remain intact
    if delegate is not None:
        delegate['peer_review'] = review
        if any(isinstance(entry, dict) and entry.get('status') == 'completed'
               and _EXPLICIT_BLOCKED.match(str(entry.get('summary') or '').strip())
               for entry in delegate['results']):
            delegate['recovery_notice'] = ('Child reported inability despite completed status; '
                                           'treat as unresolved until independently verified.')
        return json.dumps(delegate, ensure_ascii=False)
    if isinstance(result, str):
        return (result + '\n\n[Recovery peer suggestion (' + review['provider'] + '/' + review['model']
                + '): ' + review['suggestion'] + ' Verify before claiming success.]')
    if isinstance(result, dict):
        return json.dumps({**result, 'peer_review': review}, ensure_ascii=False, default=str)
    return str(result) + '\n\n[Recovery peer suggestion: ' + review['suggestion'] + ']'


def register(ctx: Any) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook('pre_llm_call', on_pre_llm_call)
    ctx.register_hook('transform_tool_result', on_transform_tool_result)
