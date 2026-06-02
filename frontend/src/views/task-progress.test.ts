import { describe, expect, it } from 'vitest'
import type { ReviewTask } from '../types'
import { ALL_CHECK_TYPES, deriveFileProgress } from './task-progress'

const task = (progress: number, status: ReviewTask['status'] = 'running'): ReviewTask => ({
  id: 'review-demo',
  owner_id: 'user-admin',
  model_node_id: 'model-qwen',
  input_mode: 'archive',
  display_name: 'gateway.zip',
  status,
  progress,
  file_count: 4,
  finding_count: 0,
  check_types: ALL_CHECK_TYPES.map((item) => item.value),
  files: [
    { id: 'file-1', relative_path: 'src/main.c', size_bytes: 120 },
    { id: 'file-2', relative_path: 'src/parser.c', size_bytes: 220 },
    { id: 'file-3', relative_path: 'src/config.c', size_bytes: 180 },
    { id: 'file-4', relative_path: 'include/config.h', size_bytes: 90 },
  ],
  created_at: '2026-06-02T00:00:00.000Z',
  updated_at: '2026-06-02T00:00:00.000Z',
})

describe('deriveFileProgress', () => {
  it('shows completed, analyzing, and pending files while a task is running', () => {
    expect(deriveFileProgress(task(52)).map((file) => file.status)).toEqual([
      'completed',
      'completed',
      'analyzing',
      'pending',
    ])
  })

  it('marks every file completed when the task completes', () => {
    expect(deriveFileProgress(task(100, 'completed')).every((file) => file.status === 'completed')).toBe(true)
  })

  it('provides at least ten selectable C review dimensions', () => {
    expect(ALL_CHECK_TYPES).toHaveLength(12)
  })
})
