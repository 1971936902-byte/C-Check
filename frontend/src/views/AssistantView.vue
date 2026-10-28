<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Plus, Delete, Refresh, Upload, Position, Close, CopyDocument, ChatDotRound } from '@element-plus/icons-vue'
import { assistantApi, assistantReason, sendAssistantMessage, type AssistantState, type Conversation, type KnowledgeDocument, type Message } from '../api/assistant'
import { errorMessage } from '../api/client'
import { createLatestRequest } from './latest-request'

const route = useRoute(), router = useRouter()
const state = ref<AssistantState>({ enabled: false, available: false, reason: 'unavailable' })
const conversations = ref<Conversation[]>([]), documents = ref<KnowledgeDocument[]>([]), messages = ref<Message[]>([])
const mode = ref('chat'), draft = ref(''), error = ref(''), loading = ref(false), sending = ref(false), uploading = ref(false)
const newDialog = ref(false), title = ref('新对话'), rag = ref(true), creating = ref(false)
const fileInput = ref<HTMLInputElement>(), messageArea = ref<HTMLElement>()
const selectedId = computed(() => typeof route.query.conversation === 'string' ? route.query.conversation : '')
const selected = computed(() => conversations.value.find(item => item.id === selectedId.value))
const canSend = computed(() => state.value.available && selected.value?.status === 'ready' && !loading.value && !sending.value && !!draft.value.trim())
const historyRequest = createLatestRequest()
let controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined, alive = true, polling = false

async function refreshLists() {
  const [threads, files] = await Promise.all([assistantApi.conversations(), assistantApi.documents()])
  if (!alive) return
  conversations.value = threads.data
  documents.value = files.data
}
async function poll() {
  if (polling || !alive) return
  polling = true
  try {
    const result = await assistantApi.status()
    if (alive) state.value = result.data
    if (mode.value === 'documents' && alive) documents.value = (await assistantApi.documents()).data
  } catch { if (alive) state.value = { enabled: false, available: false, reason: 'unavailable' } }
  finally {
    polling = false
    if (alive) timer = setTimeout(poll, 2000)
  }
}
async function loadHistory() {
  controller?.abort()
  const current = historyRequest.begin(), id = selectedId.value
  messages.value = []; error.value = ''; loading.value = !!id
  if (!id) return
  try {
    const result = await assistantApi.history(id)
    if (current()) messages.value = result.data.messages.map(message => message.interrupted
      ? { ...message, interrupted: assistantReason(message.interrupted) } : message)
  } catch (cause) { if (current()) error.value = errorMessage(cause) }
  finally { if (current()) { loading.value = false; await scrollBottom() } }
}
async function scrollBottom() {
  await nextTick()
  messageArea.value?.scrollTo({ top: messageArea.value.scrollHeight, behavior: 'instant' })
}
function choose(id: string) { mode.value = 'chat'; void router.push({ path: '/assistant', query: { conversation: id } }) }
async function create() {
  if (!title.value.trim() || creating.value) return
  creating.value = true; error.value = ''
  try {
    const result = await assistantApi.create(title.value.trim(), rag.value)
    await refreshLists(); newDialog.value = false; choose(result.data.id)
  } catch (cause) { error.value = errorMessage(cause); await refreshLists().catch(() => undefined) }
  finally { creating.value = false }
}
async function prepare() {
  if (!selectedId.value) return
  try { await assistantApi.prepare(selectedId.value); await refreshLists(); await loadHistory() }
  catch (cause) { error.value = errorMessage(cause) }
}
async function remove() {
  const id = selectedId.value
  if (!id || sending.value) return
  try {
    await ElMessageBox.confirm('删除此会话及其聊天记录？知识文件不会删除。', '删除会话', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
    await assistantApi.remove(id); await refreshLists()
    if (selectedId.value === id) await router.push('/assistant')
  } catch (cause) { if (cause !== 'cancel' && cause !== 'close') error.value = errorMessage(cause) }
}
async function send() {
  if (!canSend.value) return
  const id = selectedId.value, text = draft.value.trim(), active = new AbortController()
  controller = active; sending.value = true; draft.value = ''; error.value = ''
  messages.value.push({ role: 'user', content: text }, { role: 'assistant', content: '', sources: [] })
  const answer = messages.value[messages.value.length - 1]!
  await scrollBottom()
  try {
    await sendAssistantMessage(id, text, active.signal, event => {
      if (!alive || selectedId.value !== id) return
      if (event.event === 'delta') answer.content += event.text
      if (event.event === 'sources') answer.sources = event.sources
      if (event.event === 'error') answer.interrupted = assistantReason(event.code)
      const area = messageArea.value
      if (area && area.scrollHeight - area.scrollTop - area.clientHeight < 150) void scrollBottom()
    })
  } catch (cause) {
    answer.interrupted = active.signal.aborted ? '已停止生成' : assistantReason(errorMessage(cause))
  } finally {
    if (controller === active) { sending.value = false; controller = undefined }
  }
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement, files = Array.from(input.files || [])
  input.value = ''
  if (!files.length || !state.value.available || uploading.value) return
  if (files.length > 20 || files.some(file => file.size > 10 * 1024 * 1024) || files.reduce((sum, file) => sum + file.size, 0) > 40 * 1024 * 1024) {
    error.value = '每批最多 20 个文件，单文件不超过 10 MB，总计不超过 40 MB'; return
  }
  uploading.value = true; error.value = ''
  try { await assistantApi.upload(files); await refreshLists() }
  catch (cause) { error.value = errorMessage(cause) }
  finally { uploading.value = false }
}
async function retry(id: string) {
  try { await assistantApi.retry(id); await refreshLists() }
  catch (cause) { error.value = assistantReason(errorMessage(cause)) }
}
async function copy(text: string) {
  try { await navigator.clipboard.writeText(text) }
  catch { error.value = '无法访问剪贴板，请手动选择文本' }
}
const fileStatus = (value: string) => ({ queued: '等待处理', processing: '解析与嵌入中', ready: '可检索', paused: '已暂停', error: '处理失败', recovery_required: '等待状态确认' } as Record<string, string>)[value] || value
watch(selectedId, loadHistory)
onMounted(async () => {
  void poll()
  try { await refreshLists(); await loadHistory() } catch (cause) { error.value = errorMessage(cause) }
})
onUnmounted(() => { alive = false; clearTimeout(timer); controller?.abort(); historyRequest.dispose() })
</script>

<template>
  <section class="assistant-page">
    <header class="page-header"><div><h1>AI 智能助手</h1><p>Qwen2.5-Coder-32B FP8 · Jina</p></div>
      <span class="availability" :class="{ available: state.available }" role="status">{{ assistantReason(state.reason) }}</span>
    </header>
    <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
    <div class="assistant-layout">
      <aside class="conversation-list" aria-label="会话列表">
        <div class="list-heading"><h2>我的会话</h2><el-button :icon="Plus" circle title="新建会话" aria-label="新建会话" :disabled="!state.enabled || sending" @click="newDialog = true" /></div>
        <p v-if="!conversations.length" class="empty-label">暂无会话</p>
        <button v-for="conversation in conversations" :key="conversation.id" class="conversation" :class="{ selected: selectedId === conversation.id }" :aria-current="selectedId === conversation.id ? 'true' : undefined" @click="choose(conversation.id)">
          <strong>{{ conversation.title }}</strong><span>{{ conversation.rag_enabled ? '知识检索增强' : '普通对话' }}<template v-if="conversation.status !== 'ready'"> · 待初始化</template></span>
        </button>
      </aside>
      <section class="assistant-main">
        <div class="assistant-toolbar"><el-radio-group v-model="mode" aria-label="助手视图"><el-radio-button value="chat">对话</el-radio-button><el-radio-button value="documents">知识文件</el-radio-button></el-radio-group>
          <el-button v-if="mode === 'chat' && selected" :icon="Delete" circle title="删除会话" aria-label="删除会话" :disabled="sending" @click="remove" />
        </div>
        <template v-if="mode === 'chat'">
          <div class="chat-heading"><h2>{{ selected?.title || '选择或新建会话' }}</h2><span v-if="selected">{{ selected.rag_enabled ? 'RAG 已开启' : 'RAG 已关闭' }}</span></div>
          <el-button v-if="selected && selected.status !== 'ready'" :icon="Refresh" :disabled="!state.enabled" @click="prepare">重新初始化会话</el-button>
          <div ref="messageArea" class="messages" :aria-busy="loading" aria-label="聊天记录">
            <p v-if="loading" class="empty-label">正在读取聊天记录…</p>
            <div v-else-if="!messages.length" class="chat-empty"><el-icon><ChatDotRound /></el-icon><span>{{ selected ? '暂无消息' : '尚未选择会话' }}</span></div>
            <article v-for="(message, index) in messages" :key="index" class="message" :class="message.role">
              <header><strong>{{ message.role === 'user' ? '你' : 'Qwen' }}</strong><el-button :icon="CopyDocument" text title="复制消息" aria-label="复制消息" :disabled="!message.content" @click="copy(message.content)" /></header>
              <div class="message-content">{{ message.content || (sending && index === messages.length - 1 ? '正在生成…' : '') }}</div>
              <p v-if="message.interrupted" class="interrupted" role="status">{{ message.interrupted }}</p>
              <details v-if="message.sources?.length" class="sources"><summary>引用来源 · {{ message.sources.length }}</summary><div v-for="(source, n) in message.sources" :key="n"><strong>{{ source.title }}</strong><p>{{ source.text || source.pageContent }}</p></div></details>
            </article>
          </div>
          <form class="composer" @submit.prevent="send">
            <label for="assistant-message">消息</label>
            <el-input id="assistant-message" v-model="draft" type="textarea" :rows="3" maxlength="16000" :disabled="!selected || selected.status !== 'ready' || !state.available || sending" aria-label="消息" />
            <div class="composer-actions"><span>{{ sending ? '正在生成回答' : !state.available ? assistantReason(state.reason) : '' }}</span>
              <el-button v-if="sending" :icon="Close" @click="controller?.abort()">停止生成</el-button>
              <el-button v-else type="primary" :icon="Position" native-type="submit" :disabled="!canSend">发送</el-button>
            </div>
          </form>
        </template>
        <template v-else>
          <div class="files-heading"><h2>我的知识文件</h2><el-button type="primary" :icon="Upload" :loading="uploading" :disabled="!state.available || sending" @click="fileInput?.click()">上传文件</el-button></div>
          <input ref="fileInput" class="file-input" type="file" multiple accept=".txt,.md,.pdf,.docx,.csv,.json,.c,.h" aria-label="上传知识文件" @change="upload" />
          <p v-if="!documents.length" class="empty-label">暂无知识文件</p>
          <ul class="file-list"><li v-for="file in documents" :key="file.id"><div><strong>{{ file.filename }}</strong><small>{{ (file.size_bytes / 1024).toFixed(1) }} KB</small></div><span :class="{ 'file-ready': file.status === 'ready' }">{{ fileStatus(file.status) }}</span><el-button v-if="['paused', 'error'].includes(file.status)" :icon="Refresh" circle title="重新处理" aria-label="重新处理" :disabled="!state.available" @click="retry(file.id)" /></li></ul>
        </template>
      </section>
    </div>
    <el-dialog v-model="newDialog" title="新建会话" width="min(440px, calc(100vw - 32px))" :close-on-click-modal="!creating">
      <form @submit.prevent="create"><label for="assistant-title">会话名称</label><el-input id="assistant-title" v-model="title" maxlength="120" :disabled="creating" /><el-checkbox v-model="rag" :disabled="creating">启用知识检索增强</el-checkbox><div class="dialog-actions"><el-button :disabled="creating" @click="newDialog = false">取消</el-button><el-button type="primary" native-type="submit" :loading="creating" :disabled="!title.trim()">创建</el-button></div></form>
    </el-dialog>
  </section>
</template>

<style scoped>
.assistant-page { min-width: 0; }
.availability { max-width: 100%; padding: 7px 10px; background: #fff4d6; color: #775913; font-size: 13px; border-radius: 4px; overflow-wrap: anywhere; }
.availability.available { background: #e6f3ec; color: #256747; }
.assistant-layout { display: grid; grid-template-columns: 210px minmax(0, 1fr); margin-top: 18px; border-top: 1px solid var(--border); background: #fff; min-height: 620px; }
.conversation-list { border-right: 1px solid var(--border); padding: 16px 12px; min-width: 0; max-height: 760px; overflow: auto; }
.list-heading, .assistant-toolbar, .files-heading, .chat-heading, .message header { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
h2 { margin: 0; font-size: 15px; overflow-wrap: anywhere; }
.conversation { display: block; text-align: left; width: 100%; border: 0; border-left: 3px solid transparent; background: transparent; padding: 12px 8px; margin-top: 6px; cursor: pointer; color: inherit; }
.conversation:hover { background: #f4f7fa; }.conversation.selected { background: #edf5fa; border-left-color: var(--primary); }
.conversation strong { display: block; overflow-wrap: anywhere; font-size: 14px; }.conversation span { display: block; color: #657589; font-size: 12px; margin-top: 5px; }
.assistant-main { min-width: 0; padding: 16px 20px; display: flex; flex-direction: column; }
.assistant-toolbar { min-height: 40px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.chat-heading { padding: 14px 0; }.chat-heading span { flex-shrink: 0; font-size: 12px; color: #607188; }
.messages { height: 420px; overflow-y: auto; overscroll-behavior: contain; padding-right: 8px; }
.chat-empty { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; color: #6b7e8e; }.chat-empty .el-icon { font-size: 40px; }
.message { padding: 12px 0 18px; border-bottom: 1px solid #eef1f3; }.message.user { background: #f7f9fb; padding: 12px; }.message header { font-size: 13px; color: #50677c; }
.message-content { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.8; }
.interrupted { color: #96631b; font-size: 13px; }.sources { margin-top: 12px; font-size: 12px; }.sources summary { cursor: pointer; color: #346382; }.sources div { padding: 10px 0; }.sources p { white-space: pre-wrap; overflow-wrap: anywhere; }
.composer { border-top: 1px solid var(--border); padding-top: 12px; margin-top: auto; }.composer label, form label { display: block; font-size: 13px; margin-bottom: 8px; }
.composer-actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 10px; }.composer-actions span { font-size: 12px; color: #7a651f; overflow-wrap: anywhere; }
.files-heading { margin: 20px 0; }.file-input { display: none; }.file-list { list-style: none; padding: 0; margin: 0; }.file-list li { display: flex; align-items: center; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--border); }.file-list li>div { flex: 1; min-width: 0; }.file-list strong { display: block; font-size: 14px; overflow-wrap: anywhere; }.file-list small { display: block; color: #657589; margin-top: 6px; }.file-list li>span { font-size: 12px; max-width: 100px; }.file-ready { color: #256747; }
.empty-label { color: #657589; font-size: 13px; }.dialog-actions { display: flex; justify-content: flex-end; margin-top: 16px; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (max-width: 1100px) { .assistant-layout { grid-template-columns: 170px minmax(0, 1fr); }.assistant-main { padding: 12px; } }
@media (max-width: 650px) { .assistant-layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }.conversation-list { border-right: 0; border-bottom: 1px solid var(--border); max-height: 190px; }.conversation { margin-top: 2px; padding: 8px; }.messages { height: 360px; }.chat-heading { flex-wrap: wrap; }.assistant-main { padding: 12px; }.composer-actions span { max-width: 65%; } }
</style>
