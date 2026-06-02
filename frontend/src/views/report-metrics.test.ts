import { describe, expect, it } from 'vitest'
import { scoreTone } from './report-metrics'

describe('scoreTone', () => {
  it('maps report scores to semantic color tones', () => {
    expect(scoreTone(92)).toBe('good')
    expect(scoreTone(78)).toBe('warning')
    expect(scoreTone(48)).toBe('danger')
  })
})
