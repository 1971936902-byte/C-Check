import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


root = Path(__file__).resolve().parents[2] / 'deploy/assistant'


def load(name):
    spec = importlib.util.spec_from_file_location(name, root / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guidance_dispatch_preserves_parameters_and_rejects_reasoning():
    compat = load('guidance_compat')
    calls = []
    def original(guided_params, tokenizer):
        calls.append((guided_params, tokenizer))
        return 'processor'
    module = SimpleNamespace(get_local_guidance_guided_decoding_logits_processor=original)
    compat.install(module)
    schema = {'type': 'string', 'minLength': 1, 'maxLength': 120}
    assert module.get_local_guidance_guided_decoding_logits_processor(schema, 'tokenizer', None) == 'processor'
    assert calls == [(schema, 'tokenizer')]
    assert calls[0][0] is schema
    with pytest.raises(ValueError, match='reasoning parser'):
        module.get_local_guidance_guided_decoding_logits_processor(schema, 'tokenizer', object())
    assert len(calls) == 1


def test_unknown_guidance_interface_fails_closed():
    with pytest.raises(RuntimeError, match='unexpected pinned'):
        load('guidance_compat').install(SimpleNamespace(
            get_local_guidance_guided_decoding_logits_processor=lambda changed: None))


def test_assistant_launcher_selects_guidance_without_changing_model():
    args = load('qwen_assistant_gpu').arguments({
        'MODEL_PATH': '/models/qwen', 'GPU_MEMORY_UTILIZATION': '.88',
        'MAX_NUM_SEQS': '1', 'MAX_NUM_BATCHED_TOKENS': '1024'})
    assert args[args.index('--guided-decoding-backend') + 1] == 'guidance'
    assert args[args.index('--served-model-name') + 1] == 'qwen2.5-coder-32b-instruct-fp8'
    assert '--disable-frontend-multiprocessing' in args
