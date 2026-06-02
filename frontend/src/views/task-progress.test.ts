import { describe, expect, it } from 'vitest'
import type { ReviewTask } from '../types'
import { ALL_CHECK_TYPES, deriveReviewProgressSummary, taskDisplayName, taskSubmissionCountLabel } from './task-progress'

const task = (
  progress: number,
  status: ReviewTask['status'] = 'running',
  inputMode: ReviewTask['input_mode'] = 'archive',
): ReviewTask => ({
  id: 'review-demo',
  owner_id: 'user-admin',
  model_node_id: 'model-qwen',
  input_mode: inputMode,
  display_name: inputMode === 'text' ? 'snippet.c' : 'gateway.zip',
  status,
  progress,
  file_count: inputMode === 'text' ? 1 : 4,
  finding_count: 0,
  check_types: ALL_CHECK_TYPES.map((item) => item.value),
  files: inputMode === 'text' ? [{ id: 'file-snippet', relative_path: 'snippet.c', size_bytes: 120 }] : [
    { id: 'file-1', relative_path: 'src/main.c', size_bytes: 120 },
    { id: 'file-2', relative_path: 'src/parser.c', size_bytes: 220 },
    { id: 'file-3', relative_path: 'src/config.c', size_bytes: 180 },
    { id: 'file-4', relative_path: 'include/config.h', size_bytes: 90 },
  ],
  created_at: '2026-06-02T00:00:00.000Z',
  updated_at: '2026-06-02T00:00:00.000Z',
})

describe('deriveReviewProgressSummary', () => {
  it('shows the current archive file and only the files remaining after it', () => {
    expect(deriveReviewProgressSummary(task(52))).toEqual({
      currentLabel: 'src/config.c',
      remainingCount: 1,
      state: 'analyzing',
      stateLabel: '正在检查',
    })
  })

  it('uses a human-readable label for pasted code instead of a fake filename', () => {
    const pastedCodeTask = task(35, 'running', 'text')
    expect(taskDisplayName(pastedCodeTask)).toBe('粘贴代码片段')
    expect(taskSubmissionCountLabel(pastedCodeTask)).toBe('1 个代码片段')
    expect(deriveReviewProgressSummary(pastedCodeTask)).toEqual({
      currentLabel: '粘贴代码片段',
      remainingCount: 0,
      state: 'analyzing',
      stateLabel: '正在检查',
    })
  })

  it('shows an explicit completed state with no remaining files', () => {
    expect(deriveReviewProgressSummary(task(100, 'completed'))).toEqual({
      currentLabel: '全部检查完成',
      remainingCount: 0,
      state: 'completed',
      stateLabel: '检查完成',
    })
  })

  it('provides at least ten selectable C review dimensions', () => {
    expect(ALL_CHECK_TYPES).toHaveLength(12)
  })
})
