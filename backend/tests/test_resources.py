from app.db.models import ModelNode
from app.services import resources
from app.services.resources import _model_metrics


def test_model_metrics_derives_throughput_from_token_counters_between_snapshots(monkeypatch):
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
    times = iter([10.0, 11.0])
    monkeypatch.setattr("app.services.resources.time.monotonic", lambda: next(times))
    resources._token_counter_samples.clear()
    node = ModelNode(
        id="node-1",
        display_name="Qwen",
        model_identifier="model",
        base_url="http://127.0.0.1:8001",
    )

    first_metrics = _model_metrics(node)
    metrics = _model_metrics(node)

    assert first_metrics.prompt_throughput_tps is None
    assert first_metrics.generation_throughput_tps is None
    assert metrics.metrics_available is True
    assert metrics.prompt_throughput_tps == 150.0
    assert metrics.generation_throughput_tps == 300.0
    assert metrics.running_requests == 2
    assert metrics.pending_requests == 1
    assert metrics.gpu_kv_cache_usage_percent == 27.4


def test_model_metrics_supports_vllm_v1_counter_names_and_resets(monkeypatch):
    samples = [
        """
vllm_num_prompt_tokens_total{model_name="model"} 5e2
vllm_num_generation_tokens_total{model_name="model"} 8e2
""",
        """
vllm_num_prompt_tokens_total{model_name="model"} 10.0
vllm_num_generation_tokens_total{model_name="model"} 20.0
""",
    ]
    monkeypatch.setattr("app.services.resources._fetch_metrics", lambda _node: samples.pop(0))
    times = iter([30.0, 40.0])
    monkeypatch.setattr("app.services.resources.time.monotonic", lambda: next(times))
    resources._token_counter_samples.clear()
    node = ModelNode(id="node-reset", display_name="Qwen", model_identifier="model", base_url="http://127.0.0.1:8001")

    _model_metrics(node)
    metrics = _model_metrics(node)

    assert metrics.prompt_throughput_tps is None
    assert metrics.generation_throughput_tps is None


def test_model_metrics_uses_counter_rates_when_average_metrics_are_zero(monkeypatch):
    samples = [
        """
vllm:avg_prompt_throughput_toks_per_s{model_name="model"} 0.0
vllm:avg_generation_throughput_toks_per_s{model_name="model"} 0.0
vllm:prompt_tokens_total{model_name="model"} 1000.0
vllm:generation_tokens_total{model_name="model"} 2000.0
""",
        """
vllm:avg_prompt_throughput_toks_per_s{model_name="model"} 0.0
vllm:avg_generation_throughput_toks_per_s{model_name="model"} 0.0
vllm:prompt_tokens_total{model_name="model"} 1200.0
vllm:generation_tokens_total{model_name="model"} 2600.0
""",
    ]
    monkeypatch.setattr("app.services.resources._fetch_metrics", lambda _node: samples.pop(0))
    times = iter([100.0, 104.0])
    monkeypatch.setattr("app.services.resources.time.monotonic", lambda: next(times))
    resources._token_counter_samples.clear()
    node = ModelNode(id="node-zero-average", display_name="Qwen", model_identifier="model", base_url="http://127.0.0.1:8001")

    _model_metrics(node)
    metrics = _model_metrics(node)

    assert metrics.prompt_throughput_tps == 50.0
    assert metrics.generation_throughput_tps == 150.0
