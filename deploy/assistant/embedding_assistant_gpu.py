"""Wrap the existing signed GPU launcher; do not load a second Jina model."""
import importlib.util
import os
from pathlib import Path

import uvicorn

from embedding_control import install


def main():
    source = Path('/opt/ccheck-runtime/ccheck_embedding.py')
    spec = importlib.util.spec_from_file_location('ccheck_existing_embedding', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = Path(os.environ['ASSISTANT_CONTROL_KEY_FILE']).read_text().rstrip('\r\n')
    install(module.app, original=module.embeddings, body_type=module.EmbeddingInput,
            synchronize=module.torch.cuda.synchronize, control_key=key)
    uvicorn.run(module.app, host='0.0.0.0', port=8002, workers=1)


if __name__ == '__main__':
    main()
