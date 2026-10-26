import type { ModelNode } from '../types'

export function modelFormValues(row?: ModelNode) {
  return {
    display_name: row?.display_name || '',
    model_identifier: row?.model_identifier || '',
    base_url: row?.base_url || '',
    api_key: '',
    timeout_seconds: row?.timeout_seconds ?? 120,
    is_enabled: row?.is_enabled ?? true,
    description: row?.description || '',
  }
}

export function modelFormPayload(form: ReturnType<typeof modelFormValues>, clearApiKey: boolean) {
  return { ...form, api_key: clearApiKey ? null : form.api_key || undefined }
}
