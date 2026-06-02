import { describe, expect, it } from 'vitest'
import { validateModel, validateNewUser, validatePasswordChange, validatePrompt } from './form-validation'

describe('form validation', () => {
  it('rejects invalid admin user forms before submission', () => {
    expect(validateNewUser({ username: '', password: '', role: 'user' })).toBe('请输入用户名')
    expect(validateNewUser({ username: 'demo', password: 'short', role: 'user' })).toBe('初始密码至少 12 位')
  })

  it('requires the essential model connection fields', () => {
    expect(validateModel({ display_name: '', model_identifier: '', base_url: '' })).toBe('请输入模型展示名称')
    expect(validateModel({ display_name: 'Qwen', model_identifier: 'qwen', base_url: '' })).toBe('请输入 VLLM 服务地址')
  })

  it('rejects empty prompt versions', () => {
    expect(validatePrompt('   ')).toBe('请输入提示词内容')
  })

  it('requires the current password and matching valid new password', () => {
    expect(validatePasswordChange('', '123456789012', '123456789012')).toBe('请输入当前密码')
    expect(validatePasswordChange('old-password', 'short', 'short')).toBe('新密码至少 12 位')
    expect(validatePasswordChange('old-password', '123456789012', '123456789013')).toBe('两次输入的新密码不一致')
  })
})
