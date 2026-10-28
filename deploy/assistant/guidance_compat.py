"""Adapt the pinned V0 guidance dispatcher without changing JSON constraints."""
import inspect


def install(module):
    original = module.get_local_guidance_guided_decoding_logits_processor
    if list(inspect.signature(original).parameters) != ['guided_params', 'tokenizer']:
        raise RuntimeError('unexpected pinned vLLM guidance interface')

    def build(guided_params, tokenizer, reasoner=None):
        # vLLM 0.8.5.post1 dispatches three arguments to its two-argument
        # guidance implementation. This deployment does not enable reasoning.
        if reasoner is not None:
            raise ValueError('assistant guidance does not support a reasoning parser')
        return original(guided_params, tokenizer)

    module.get_local_guidance_guided_decoding_logits_processor = build
