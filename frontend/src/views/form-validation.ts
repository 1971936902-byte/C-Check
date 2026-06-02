export function validateNewUser(form: { username: string; password: string; role?: string }) {
  if (!form.username.trim()) return '请输入用户名'
  if (form.password.length < 12) return '初始密码至少 12 位'
  return ''
}

export function validateModel(form: { display_name: string; model_identifier: string; base_url: string }) {
  if (!form.display_name.trim()) return '请输入模型展示名称'
  if (!form.model_identifier.trim()) return '请输入模型标识'
  if (!form.base_url.trim()) return '请输入 VLLM 服务地址'
  return ''
}

export function validatePrompt(body: string) {
  return body.trim() ? '' : '请输入提示词内容'
}

export function validatePasswordChange(current: string, next: string, confirm: string) {
  if (!current) return '请输入当前密码'
  if (next.length < 12) return '新密码至少 12 位'
  if (next !== confirm) return '两次输入的新密码不一致'
  return ''
}
