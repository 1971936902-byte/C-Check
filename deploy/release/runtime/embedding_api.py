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
torch.set_num_threads(4)
tokenizer = AutoTokenizer.from_pretrained(PATH, local_files_only=True)
tokenizer.padding_side = 'left'
model = AutoModel.from_pretrained(PATH, torch_dtype=torch.float32, local_files_only=True,
                                attn_implementation='sdpa').eval()
lock = Lock()
app = FastAPI()


class EmbeddingInput(BaseModel):
    model: str
    input: str | list[str]


@app.get('/health')
def health():
    return {'ready': True, 'model': NAME, 'device': 'cpu', 'dimension': 1536}


@app.post('/v1/embeddings')
def embeddings(body: EmbeddingInput, authorization: str | None = Header(None)):
    if not hmac.compare_digest(authorization or '', 'Bearer ' + KEY):
        raise HTTPException(401, 'Invalid API key')
    if body.model != NAME:
        raise HTTPException(400, 'Unknown embedding model')
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > 8 or any(not text or len(text) > 16000 for text in texts):
        raise HTTPException(400, 'Input or batch limit exceeded')
    result, tokens = [], 0
    with lock, torch.inference_mode():
        for text in texts:
            batch = tokenizer(text, return_tensors='pt', truncation=False)
            count = batch['input_ids'].shape[1]
            if count > 8192:
                raise HTTPException(400, 'Input exceeds 8192 tokens')
            hidden = model(**batch, use_cache=False).last_hidden_state[:, -1, :]
            vector = F.normalize(hidden.float(), p=2, dim=1)[0].tolist()
            if len(vector) != 1536:
                raise HTTPException(500, 'Embedding dimension mismatch')
            result.append({'object': 'embedding', 'index': len(result), 'embedding': vector})
            tokens += count
    return {'object': 'list', 'model': NAME, 'data': result,
            'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}
