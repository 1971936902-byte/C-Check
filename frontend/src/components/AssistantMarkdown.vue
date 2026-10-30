<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { Check, CopyDocument } from '@element-plus/icons-vue'
import { renderAssistantMarkdown } from './assistant-markdown'

const props = defineProps<{ content: string }>()
const html = computed(() => renderAssistantMarkdown(props.content))
const emit = defineEmits<{ error: [message: string] }>()
const rendered = ref<HTMLElement>()
const slots = shallowRef<HTMLElement[]>([]), copied = shallowRef<HTMLElement>()
let timer: ReturnType<typeof setTimeout> | undefined
function collect() { slots.value = Array.from(rendered.value?.querySelectorAll<HTMLElement>('.code-copy-slot') || []) }
async function copyCode(slot: HTMLElement) {
  const code = slot.closest('.code-block')?.querySelector('pre code')?.textContent
  if (code === undefined || code === null) return
  try {
    await navigator.clipboard.writeText(code)
    copied.value = slot; clearTimeout(timer)
    timer = setTimeout(() => { copied.value = undefined }, 2000)
  } catch { emit('error', '无法访问剪贴板，请手动选择代码') }
}
watch(html, collect, { flush: 'post' })
onMounted(collect)
onUnmounted(() => clearTimeout(timer))
</script>

<template>
  <div class="markdown-body">
    <div ref="rendered" class="rendered-markdown" v-html="html"></div>
    <Teleport v-for="(slot, index) in slots" :key="index" :to="slot">
      <el-button :icon="copied === slot ? Check : CopyDocument" text :title="copied === slot ? '已复制代码' : '复制代码'" :aria-label="copied === slot ? '已复制代码' : '复制代码'" @click="copyCode(slot)" />
    </Teleport>
  </div>
</template>

<style scoped>
.markdown-body { min-width: 0; font-size: 14px; line-height: 1.85; overflow-wrap: anywhere; }
.markdown-body :deep(.rendered-markdown > :first-child) { margin-top: 0; }
.markdown-body :deep(.rendered-markdown > :last-child) { margin-bottom: 0; }
.markdown-body :deep(p) { margin: 0 0 12px; }
.markdown-body :deep(h1), .markdown-body :deep(h2) { font-size: 19px; line-height: 1.5; margin: 22px 0 10px; }
.markdown-body :deep(h3), .markdown-body :deep(h4), .markdown-body :deep(h5), .markdown-body :deep(h6) { font-size: 15px; line-height: 1.6; margin: 18px 0 8px; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 24px; margin: 10px 0; }
.markdown-body :deep(li + li) { margin-top: 5px; }
.markdown-body :deep(blockquote) { margin: 14px 0; padding: 8px 14px; border-left: 3px solid #6a9e91; background: #f2f8f5; color: #44645b; }
.markdown-body :deep(blockquote p:last-child) { margin-bottom: 0; }
.markdown-body :deep(code) { padding: 2px 5px; border-radius: 4px; background: #edf1f4; color: #31596c; font-family: Consolas, monospace; font-size: 13px; }
.markdown-body :deep(pre) { max-width: 100%; margin: 14px 0; padding: 16px; overflow: auto; border: 1px solid #dfe5ea; border-radius: 6px; background: #f5f7f9; line-height: 1.7; white-space: pre; overflow-wrap: normal; }
.markdown-body :deep(pre code) { padding: 0; background: none; color: #293e4e; }
.markdown-body :deep(.code-block) { min-width: 0; margin: 14px 0; border: 1px solid #dfe5ea; border-radius: 6px; background: #f5f7f9; }
.markdown-body :deep(.code-toolbar) { display: flex; align-items: center; justify-content: space-between; min-height: 36px; padding: 2px 8px 2px 14px; border-bottom: 1px solid #dfe5ea; color: #627484; font-size: 12px; }
.markdown-body :deep(.code-copy-slot) { display: flex; min-width: 32px; height: 32px; }
.markdown-body :deep(.code-copy-slot .el-button) { width: 32px; height: 32px; padding: 6px; }
.markdown-body :deep(.code-block pre) { margin: 0; border: 0; border-radius: 0 0 6px 6px; }
.markdown-body :deep(.hljs-keyword), .markdown-body :deep(.hljs-type), .markdown-body :deep(.hljs-literal) { color: #8b3386; }
.markdown-body :deep(.hljs-string), .markdown-body :deep(.hljs-regexp) { color: #21694b; }
.markdown-body :deep(.hljs-number), .markdown-body :deep(.hljs-meta) { color: #97510e; }
.markdown-body :deep(.hljs-title), .markdown-body :deep(.hljs-built_in) { color: #175f94; }
.markdown-body :deep(.hljs-comment) { color: #6d7884; font-style: italic; }
.markdown-body :deep(table) { display: block; width: max-content; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: 14px 0; }
.markdown-body :deep(th), .markdown-body :deep(td) { min-width: 90px; padding: 9px 12px; border: 1px solid #dfe5ea; text-align: left; }
.markdown-body :deep(th) { background: #f0f5f6; font-weight: 600; }
.markdown-body :deep(a) { color: #236c92; text-decoration: underline; text-underline-offset: 3px; }
.markdown-body :deep(a:focus-visible) { outline: 2px solid var(--primary); outline-offset: 3px; }
.markdown-body :deep(hr) { border: 0; border-top: 1px solid var(--border); margin: 20px 0; }
</style>
