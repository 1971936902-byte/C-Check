import { describe, expect, it } from 'vitest'
import { activeUpload } from './workspace-input'

describe('activeUpload', () => {
  const single = new File(['int main(void) {}'], 'main.c')
  const archive = new File(['zip'], 'project.zip')

  it('keeps the selected upload isolated by input mode', () => {
    expect(activeUpload('file', single, archive)).toBe(single)
    expect(activeUpload('archive', single, archive)).toBe(archive)
    expect(activeUpload('text', single, archive)).toBeUndefined()
  })
})
