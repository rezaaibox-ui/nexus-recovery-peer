"""Contract tests for the Nexus recovery-peer plugin."""
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
import yaml

PLUGIN = Path(__file__).resolve().parents[1] / '__init__.py'


ROUTES = [
    {'model_contains': 'gpt-6-', 'origin_provider': 'openai-codex',
     'peer_provider': 'claude-subscription-directsdk-experimental', 'peer_model': 'sonnet'},
    {'model_contains': 'deepseek', 'origin_provider': 'openrouter',
     'peer_provider': 'openai-codex', 'peer_model': 'gpt-6-sol'},
]


def peer(routes=None):
    spec = importlib.util.spec_from_file_location('nexus_recovery_peer_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class LLM:
        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='Try a different query, then verify the source.',
                                   provider=kwargs['provider'], model=kwargs['model'])

    selected = ROUTES if routes is None else routes
    module._ctx = SimpleNamespace(llm=LLM(), has_capability=lambda _: True,
                                  get_config=lambda key, default=None: selected if key == 'routes' else default)
    module._test_calls = calls
    return module


def prime(peer, sid='s', turn='t', model='gpt-6-sol', task='Investigate failed read'):
    peer.on_pre_llm_call(session_id=sid, turn_id=turn, model=model, user_message=task)


def transform(peer, result, name='read_file', args=None, sid='s', turn='t', status='error', error_type='FileNotFoundError'):
    return peer.on_transform_tool_result(tool_name=name, result=result, args=args or {'path': 'missing'},
                                         session_id=sid, turn_id=turn, status=status, error_type=error_type)


def test_identical_failures_call_independent_peer_once(peer):
    prime(peer)
    assert transform(peer, 'Error: not found') is None
    assert transform(peer, 'Error: not found') is None
    out = transform(peer, 'Error: not found')
    assert 'Error: not found' in out
    assert 'Try a different query' in out
    assert 'claude-subscription-directsdk-experimental' in out
    assert len(peer._test_calls) == 1
    assert peer._test_calls[0]['provider'] == 'claude-subscription-directsdk-experimental'
    assert transform(peer, 'Error: not found') is None
    assert len(peer._test_calls) == 1


def test_changed_args_or_success_resets_failure_streak(peer):
    prime(peer)
    assert transform(peer, 'Error: missing', args={'path': 'a'}) is None
    assert transform(peer, 'Error: missing', args={'path': 'b'}) is None
    assert transform(peer, '{"ok": true}', args={'path': 'b'}, status='ok', error_type=None) is None
    assert transform(peer, 'Error: missing', args={'path': 'a'}) is None
    assert not peer._test_calls


def test_repeated_read_only_no_progress_but_not_terminal_polling(peer):
    prime(peer)
    for _ in range(3):
        output = transform(peer, 'same text', status='ok', error_type=None)
    assert 'Try a different query' in output
    prime(peer, sid='other')
    for _ in range(4):
        assert transform(peer, '{"exit_code":0}', name='terminal', sid='other', status='ok', error_type=None) is None
    assert len(peer._test_calls) == 1


def test_failed_delegate_gets_second_opinion_without_replaying_writes(peer):
    prime(peer)
    raw = json.dumps({'results': [{'status': 'error', 'summary': '', 'error': 'unit test failure'}]})
    out = transform(peer, raw, name='delegate_task', args={'tasks': [{'goal': 'Fix code'}]}, status='ok', error_type=None)
    parsed = json.loads(out)
    assert parsed['results'][0]['status'] == 'error'
    assert parsed['peer_review']['provider'] == 'claude-subscription-directsdk-experimental'
    assert 'different query' in parsed['peer_review']['suggestion']
    assert not any(call['provider'] == 'openai-codex' for call in peer._test_calls)


def test_completed_or_interrupted_delegate_is_not_retried(peer):
    prime(peer)
    for status in ('completed', 'interrupted'):
        raw = json.dumps({'results': [{'status': status, 'summary': 'done'}]})
        assert transform(peer, raw, name='delegate_task', args={'tasks': [{'goal': 'x'}]}, status='ok', error_type=None) is None
    assert not peer._test_calls


def test_completed_child_explicitly_says_blocked_gets_review(peer):
    prime(peer)
    raw = json.dumps({'results': [{'status': 'completed',
                                   'summary': 'Unable to complete because the required fixture is absent.'}]})
    out = transform(peer, raw, name='delegate_task', args={'tasks': [{'goal': 'Read fixture'}]},
                    status='ok', error_type=None)
    parsed = json.loads(out)
    assert parsed['results'][0]['status'] == 'completed'  # preserve the child's claim
    assert parsed['peer_review']['provider'] == 'claude-subscription-directsdk-experimental'
    assert parsed['recovery_notice'].startswith('Child reported inability')


def test_child_uses_codex_peer_not_own_provider(peer):
    prime(peer, model='deepseek/deepseek-v4.1-flash')
    for _ in range(3):
        out = transform(peer, 'Error: no progress')
    assert 'openai-codex' in out
    assert peer._test_calls[0]['model'] == 'gpt-6-sol'


def test_unconfigured_route_never_sends_task_to_peer(_unused):
    unconfigured = peer(routes=[])
    prime(unconfigured)
    for _ in range(3):
        assert transform(unconfigured, 'Error: no route') is None
    assert unconfigured._test_calls == []


def test_manifest_exposes_opt_in_routes_setting(_unused):
    manifest = yaml.safe_load((PLUGIN.parent / 'plugin.yaml').read_text(encoding='utf-8'))
    assert 'config_schema' in manifest, 'route setting must be declared for Desktop users'
    routes = manifest['config_schema']['routes']
    assert routes['type'] == 'list'
    assert routes['default'] == []


def test_redacts_credentials_in_peer_prompt(peer):
    prime(peer, task='Investigate password=hunter2 and sk-abcdefghijklmnopqrstuvwxyz123456')
    for _ in range(3):
        transform(peer, 'Error: Authorization: Bearer topsecret1234567')
    prompt = '\n'.join(message['content'] for message in peer._test_calls[0]['messages'])
    assert 'hunter2' not in prompt
    assert 'topsecret1234567' not in prompt
    assert 'sk-abcdefghijklmnopqrstuvwxyz123456' not in prompt


def test_peer_failure_does_not_change_original_tool_result(peer):
    prime(peer)
    def fail(**kwargs):
        raise TimeoutError('no answer')
    peer._ctx.llm.complete = fail
    for _ in range(2):
        assert transform(peer, 'Error: missing') is None
    assert transform(peer, 'Error: missing') is None


def test_unidentified_or_unexpected_provider_is_not_called_independent(_unused):
    for actual in ('', 'unexpected-provider'):
        candidate = peer()
        candidate._ctx.llm.complete = lambda **kw: SimpleNamespace(
            text='Try a different query.', provider=actual, model=kw['model'])
        prime(candidate)
        for _ in range(3):
            assert transform(candidate, 'Error: missing') is None


def test_new_turn_rearms_but_recovery_is_bounded(peer):
    prime(peer)
    for _ in range(3):
        transform(peer, 'Error: missing')
    prime(peer, turn='next')
    for _ in range(3):
        out = transform(peer, 'Error: missing', turn='next')
    assert 'Try a different query' in out
    assert len(peer._test_calls) == 2


def test_prefixed_quoted_bearer_and_private_key_values_are_redacted(_unused):
    cases = [
        'GITHUB_TOKEN=fixture-value-alpha',
        'refresh_token=fixture-value-alpha',
        '{"client_secret": "fixture-value-alpha with spaces"}',
        'Bearer fixture-value-alpha',
        '-----BEGIN PRIVATE KEY-----\nfixture-value-alpha\n-----END PRIVATE KEY-----',
    ]
    for raw in cases:
        candidate = peer()
        prime(candidate, task=raw)
        for _ in range(3):
            transform(candidate, 'Error: ' + raw)
        text = candidate._test_calls[0]['messages'][1]['content']
        assert 'fixture-value-alpha' not in text, 'sensitive fixture reached prompt'
        assert '[REDACTED]' in text


def test_redaction_precedes_truncation(peer):
    synthetic = 'sk-' + ('A' * 40)
    assert 'sk-' not in peer._redact(synthetic, 10)


def test_manifest_declares_llm_overrides(_unused):
    manifest = yaml.safe_load((PLUGIN.parent / 'plugin.yaml').read_text(encoding='utf-8'))
    assert {'llm.provider_override', 'llm.model_override'} <= set(manifest.get('capabilities', []))


def test_missing_consent_never_sends_task(peer):
    peer._ctx.has_capability = lambda _: False
    prime(peer)
    for _ in range(3):
        assert transform(peer, 'Error: missing') is None
    assert peer._test_calls == []


def test_advice_label_does_not_claim_verified_independence(peer):
    prime(peer)
    for _ in range(3):
        output = transform(peer, 'Error: missing')
    assert 'Recovery peer suggestion' in output
    assert 'Independent recovery diagnosis' not in output


def load_tests(loader, tests, pattern):
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            tests.addTest(unittest.FunctionTestCase(lambda case=fn: case(peer()), description=name))
    return tests
