import type { ReviewFile, ReviewTask } from '../types'

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
export type FileProgress = ReviewFile & { status: FileProgressStatus }

export function deriveFileProgress(task: ReviewTask): FileProgress[] {
  const files = task.files || []
  if (task.status === 'completed') return files.map((file) => ({ ...file, status: 'completed' }))
  if (task.status !== 'running') return files.map((file) => ({ ...file, status: 'pending' }))
  const completed = Math.min(Math.floor((task.progress / 100) * files.length), Math.max(0, files.length - 1))
  return files.map((file, index) => ({ ...file, status: index < completed ? 'completed' : index === completed ? 'analyzing' : 'pending' }))
}
