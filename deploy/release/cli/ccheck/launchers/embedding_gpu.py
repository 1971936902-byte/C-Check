"""GPU last-token embedding; bounded microbatches, no silent CPU fallback."""
import hmac
import os
from pathlib import Path
from threading import Lock

import torch
import torch.nn.functional as F
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

NAME = 'jina-code-embeddings-1.5b'
KEY = Path(os.environ['RAG_EMBEDDING_API_KEY_FILE']).read_text().rstrip('\r\n')
PATH = os.environ['MODEL_PATH']
LIMIT = int(os.environ.get('EMBEDDING_BATCH_SIZE', '1'))
TOKEN_BUDGET = int(os.environ.get('EMBEDDING_TOKEN_BUDGET', '8192'))
if not 1 <= LIMIT <= 2 or not 512 <= TOKEN_BUDGET <= 8192:
    raise RuntimeError('invalid embedding microbatch budget')
if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
    raise RuntimeError('exactly one CUDA GPU is required; CPU fallback is disabled')
torch.set_num_threads(4)
tokenizer = AutoTokenizer.from_pretrained(PATH, local_files_only=True)
tokenizer.padding_side = 'left'
if tokenizer.pad_token_id is None:
    tokenizer.pad_token = tokenizer.eos_token
model = AutoModel.from_pretrained(PATH, torch_dtype=torch.bfloat16,
    local_files_only=True, attn_implementation='sdpa').eval().to('cuda')
lock = Lock()
app = FastAPI()


class EmbeddingInput(BaseModel):
    model: str
    input: str | list[str]


@app.get('/health')
def health():
    return {'ready': True, 'model': NAME, 'device': str(next(model.parameters()).device),
            'dtype': str(next(model.parameters()).dtype), 'dimension': 1536,
            'batch_size': LIMIT, 'token_budget': TOKEN_BUDGET}


@app.post('/v1/embeddings')
def embeddings(body: EmbeddingInput, authorization: str | None = Header(None)):
    if not hmac.compare_digest(authorization or '', 'Bearer ' + KEY):
        raise HTTPException(401, 'Invalid API key')
    if body.model != NAME:
        raise HTTPException(400, 'Unknown embedding model')
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > 8 or any(not text or len(text) > 16000 for text in texts):
        raise HTTPException(400, 'Input or batch limit exceeded')
    with lock, torch.inference_mode():
        encoded = [tokenizer(text, return_tensors='pt', truncation=False) for text in texts]
        counts = [item['input_ids'].shape[1] for item in encoded]
        if any(count > 8192 for count in counts):
            raise HTTPException(400, 'Input exceeds 8192 tokens')
        result = []
        start = 0
        try:
            while start < len(texts):
                end = start + 1
                while (end < len(texts) and end - start < LIMIT
                       and max(counts[start:end + 1]) * (end + 1 - start) <= TOKEN_BUDGET):
                    end += 1
                # A long single item is never truncated to fit a microbatch budget.
                batch = tokenizer(texts[start:end], return_tensors='pt', padding=True,
                                  truncation=False).to('cuda')
                hidden = model(**batch, use_cache=False).last_hidden_state[:, -1, :]
                vectors = F.normalize(hidden.float(), p=2, dim=1).cpu()
                if vectors.shape[1] != 1536 or not bool(torch.isfinite(vectors).all()):
                    raise HTTPException(500, 'Invalid embedding output')
                for vector in vectors.tolist():
                    result.append({'object': 'embedding', 'index': len(result), 'embedding': vector})
                del batch, hidden, vectors
                start = end
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            raise HTTPException(503, 'GPU embedding budget exhausted; reduce concurrency or reserve more GPU memory') from None
    tokens = sum(counts)
    return {'object': 'list', 'model': NAME, 'data': result,
            'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}
