import type { ReviewTask } from '../types'

export const ALL_CHECK_TYPES = [
  { value: 'memory_safety', label: '内存安全' },
  { value: 'buffer_overflow', label: '缓冲区溢出' },
  { value: 'pointer_safety', label: '空指针与野指针' },
  { value: 'resource_leak', label: '资源泄漏' },
  { value: 'concurrency', label: '并发与线程安全' },
  { value: 'logic', label: '逻辑错误' },
  { value: 'input_validation', label: '输入校验' },
  { value: 'integer_safety', label: '整数溢出与类型转换' },
  { value: 'compatibility', label: '编译兼容性' },
  { value: 'portability', label: '跨平台可移植性' },
  { value: 'performance', label: '性能隐患' },
  { value: 'maintainability', label: '代码规范与可维护性' },
] as const

export type FileProgressStatus = 'pending' | 'analyzing' | 'completed'
export type ReviewProgressSummary = {
  currentLabel: string
  remainingCount: number
  state: FileProgressStatus | 'failed'
  stateLabel: string
}

export function taskDisplayName(task: ReviewTask) {
  return task.input_mode === 'text' ? '粘贴代码片段' : task.display_name
}

export function taskSubmissionCountLabel(task: ReviewTask) {
  return task.input_mode === 'text' ? '1 个代码片段' : `${task.file_count} 个文件`
}

export function deriveReviewProgressSummary(task: ReviewTask): ReviewProgressSummary {
  if (task.status === 'completed') {
    return { currentLabel: '全部检查完成', remainingCount: 0, state: 'completed', stateLabel: '检查完成' }
  }

  const files = task.files || []
  const total = Math.max(task.file_count, files.length, task.input_mode === 'text' ? 1 : 0)
  const completed = task.status === 'running'
    ? Math.min(Math.floor((task.progress / 100) * total), Math.max(0, total - 1))
    : 0
  const currentLabel = task.input_mode === 'text'
    ? '粘贴代码片段'
    : files[completed]?.relative_path || task.display_name

  if (task.status === 'failed') {
    return { currentLabel, remainingCount: Math.max(0, total - completed), state: 'failed', stateLabel: '检查中断' }
  }
  if (task.status === 'queued') {
    return { currentLabel, remainingCount: total, state: 'pending', stateLabel: '等待开始' }
  }
  return { currentLabel, remainingCount: Math.max(0, total - completed - 1), state: 'analyzing', stateLabel: '正在检查' }
}
