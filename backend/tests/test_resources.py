from app.db.models import ModelNode
from app.services.resources import _model_metrics


def test_model_metrics_derives_throughput_from_token_counters(monkeypatch):
    samples = [
        """
vllm:num_requests_running{model_name="model"} 2.0
vllm:num_requests_waiting{model_name="model"} 1.0
vllm:gpu_cache_usage_perc{model_name="model"} 0.274
vllm:prompt_tokens_total{model_name="model"} 1000.0
vllm:generation_tokens_total{model_name="model"} 2000.0
""",
        """
vllm:num_requests_running{model_name="model"} 2.0
vllm:num_requests_waiting{model_name="model"} 1.0
vllm:gpu_cache_usage_perc{model_name="model"} 0.274
vllm:prompt_tokens_total{model_name="model"} 1150.0
vllm:generation_tokens_total{model_name="model"} 2300.0
""",
    ]
    monkeypatch.setattr("app.services.resources._fetch_metrics", lambda _node: samples.pop(0))
    monkeypatch.setattr("app.services.resources.time.sleep", lambda _seconds: None)
    times = iter([10.0, 11.0])
    monkeypatch.setattr("app.services.resources.time.monotonic", lambda: next(times))

    metrics = _model_metrics(
        ModelNode(
            id="node-1",
            display_name="Qwen",
            model_identifier="model",
            base_url="http://127.0.0.1:8001",
        )
    )

    assert metrics.metrics_available is True
    assert metrics.prompt_throughput_tps == 150.0
    assert metrics.generation_throughput_tps == 300.0
    assert metrics.running_requests == 2
    assert metrics.pending_requests == 1
    assert metrics.gpu_kv_cache_usage_percent == 27.4
