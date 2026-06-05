export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed'
export type Severity = 'high' | 'medium' | 'low' | 'suggestion'

export interface User { id: string; username: string; role: 'user' | 'admin'; is_enabled: boolean }
export interface ModelNode { id: string; display_name: string; model_identifier: string; base_url: string; api_key?: string | null; timeout_seconds: number; is_enabled: boolean; is_default: boolean; description?: string | null; created_at?: string }
export interface ReviewFile { id: string; relative_path: string; size_bytes: number }
export interface ReviewTask {
  id: string; owner_id: string; model_node_id: string; input_mode: string; display_name: string
  tester_name?: string
  status: TaskStatus; progress: number; error_message?: string | null; duration_ms?: number | null
  model_log?: string | null
  file_count: number; finding_count: number; started_at?: string | null; completed_at?: string | null
  created_at: string; updated_at: string; files?: ReviewFile[]; report_id?: string | null; check_types?: string[]
}
export type CodeLineKind = 'context' | 'removed' | 'added'
export interface CodeLine { line: number; content: string; kind: CodeLineKind }
export interface Finding { severity: Severity; category: string; title: string; description: string; file_path: string; line?: number | null; remediation: string; code_snippet?: CodeLine[]; fixed_snippet?: CodeLine[] }
export interface Report { id: string; task_id: string; summary: string; score: number; high_count: number; medium_count: number; low_count: number; suggestion_count: number; category_counts: Record<string, number>; result_json: { summary: string; score: number; findings: Finding[] } }
export interface Dashboard { users: number; enabled_users: number; models: number; enabled_models: number; tasks: number; queued_tasks: number; running_tasks: number; completed_tasks: number; failed_tasks: number }
export interface SystemResource { cpu_percent?: number | null; load_average_1m?: number | null; memory_total_bytes?: number | null; memory_used_bytes?: number | null; memory_percent?: number | null; disk_total_bytes?: number | null; disk_used_bytes?: number | null; disk_percent?: number | null }
export interface GpuDevice { index: number; name: string; utilization_percent?: number | null; memory_used_mb?: number | null; memory_total_mb?: number | null; memory_percent?: number | null; temperature_c?: number | null; power_w?: number | null }
export interface ModelRuntimeMetric { node_id: string; display_name: string; base_url: string; metrics_available: boolean; prompt_throughput_tps?: number | null; generation_throughput_tps?: number | null; running_requests?: number | null; pending_requests?: number | null; gpu_kv_cache_usage_percent?: number | null; error?: string | null }
export interface ResourceSnapshot { captured_at: string; system: SystemResource; gpus: GpuDevice[]; models: ModelRuntimeMetric[]; tasks: Dashboard }
export interface AdminUser extends User { created_at: string }
export interface Prompt { id: string; version: number; body: string; is_active: boolean; creator_id?: string | null; created_at: string }
export interface AdminTask { id: string; owner_id: string; model_node_id: string; display_name: string; status: TaskStatus; progress: number; finding_count: number; error_message?: string | null; created_at: string }
export interface ReviewTaskPage { items: ReviewTask[]; total: number }
