import { describe, expect, it } from 'vitest'
import { activeUpload, canSubmitReview, hasReviewInput } from './workspace-input'

describe('activeUpload', () => {
  const single = new File(['int main(void) {}'], 'main.c')
  const archive = new File(['zip'], 'project.zip')

  it('keeps the selected upload isolated by input mode', () => {
    expect(activeUpload('file', single, archive)).toBe(single)
    expect(activeUpload('archive', single, archive)).toBe(archive)
    expect(activeUpload('text', single, archive)).toBeUndefined()
  })

  it('detects whether the active input mode has review content', () => {
    expect(hasReviewInput('text', ' int main(void) {} ')).toBe(true)
    expect(hasReviewInput('text', '   ', single, archive)).toBe(false)
    expect(hasReviewInput('file', '', single, archive)).toBe(true)
    expect(hasReviewInput('archive', '', single, archive)).toBe(true)
    expect(hasReviewInput('file', '', undefined, archive)).toBe(false)
  })

  it('requires a model, check type, and content before submission', () => {
    expect(canSubmitReview({
      mode: 'file',
      selectedModel: 'model-1',
      sourceText: '',
      singleFile: single,
      checkTypes: ['memory_safety'],
    })).toBe(true)
    expect(canSubmitReview({
      mode: 'file',
      selectedModel: '',
      sourceText: '',
      singleFile: single,
      checkTypes: ['memory_safety'],
    })).toBe(false)
    expect(canSubmitReview({
      mode: 'text',
      selectedModel: 'model-1',
      sourceText: '',
      checkTypes: ['memory_safety'],
    })).toBe(false)
    expect(canSubmitReview({
      mode: 'text',
      selectedModel: 'model-1',
      sourceText: 'int main(void) {}',
      checkTypes: [],
    })).toBe(false)
  })
})
