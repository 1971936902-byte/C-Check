"""Check the pinned alternative decoder against unmodified review schemas."""
import importlib.util
import json
import sys
from pathlib import Path
from time import perf_counter

from transformers import AutoTokenizer
from vllm.model_executor.guided_decoding.guidance_decoding import (
    get_local_guidance_guided_decoding_logits_processor,
)
from vllm.sampling_params import GuidedDecodingParams


def main():
    path = Path('/opt/assistant/review_response_schema.py')
    spec = importlib.util.spec_from_file_location('review_response_schema', path)
    schemas = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = schemas
    spec.loader.exec_module(schemas)
    tokenizer = AutoTokenizer.from_pretrained('/models/qwen', local_files_only=True)
    for name in ('FormattedFindingsResponse', 'CompactModelReviewResponse', 'ModelReviewResponse'):
        schema = getattr(schemas, name).model_json_schema()
        original = json.dumps(schema, sort_keys=True)
        started = perf_counter()
        processor = get_local_guidance_guided_decoding_logits_processor(
            GuidedDecodingParams(json=schema, backend='guidance'), tokenizer)
        assert processor is not None
        processor._initialize()
        assert not processor.ll_matcher.get_error(), processor.ll_matcher.get_error()
        assert json.dumps(schema, sort_keys=True) == original
        print(json.dumps({'schema': name, 'seconds': round(perf_counter() - started, 3),
                          'schema_unchanged': True,
                          'warnings': processor.ll_matcher.get_grammar_warnings()}), flush=True)


if __name__ == '__main__':
    main()
