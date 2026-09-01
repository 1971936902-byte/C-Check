import type { ReviewTask } from '../types'

export const ALL_CHECK_TYPES = [
  { value: 'memory_safety', label: '释放后使用/内存破坏', description: 'UAF、double free、越界读写等严重内存问题' },
  { value: 'buffer_overflow', label: '缓冲区/数组越界', description: '外部长度、索引或拷贝导致的越界风险' },
  { value: 'pointer_safety', label: '野指针/悬空指针', description: '普通空指针未校验会自动归入低级问题' },
  { value: 'resource_leak', label: '资源泄漏', description: '文件、内存、句柄等 acquire/release 不闭环' },
  { value: 'integer_safety', label: '长度/索引整数风险', description: '整数溢出、下溢、截断影响长度或下标' },
  { value: 'logic', label: '严重状态机/协议逻辑', description: '协议状态、权限或关键流程绕过' },
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
