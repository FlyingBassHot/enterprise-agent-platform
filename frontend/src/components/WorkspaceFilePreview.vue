<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Close, Document, Download } from '@element-plus/icons-vue'
import { workspaceApi } from '@/api/platform'
import type { FileRootEntry } from '@/types/api'
import { formatBytes } from '@/utils/format'

const props = defineProps<{
  agentId: string
  rootId: string
  rootName: string
  file: FileRootEntry
}>()

const emit = defineEmits<{
  close: []
}>()

type PreviewKind = 'text' | 'image' | 'pdf' | 'binary'

const TEXT_EXTENSIONS = new Set([
  'txt',
  'md',
  'json',
  'yaml',
  'yml',
  'csv',
  'ts',
  'js',
  'vue',
  'py',
  'html',
  'css',
  'xml',
  'log',
  'ini',
  'conf',
  'sql',
  'sh',
  'tsv',
])

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'])

const TEXT_PREVIEW_LIMIT = 1024 * 1024

const loading = ref(false)
const downloading = ref(false)
const error = ref('')
const kind = ref<PreviewKind>('binary')
const textContent = ref('')
const objectUrl = ref('')
const blob = ref<Blob | null>(null)

let requestToken = 0
let loadedKey = ''

interface ErrorPayload {
  response?: {
    data?: {
      message?: unknown
      detail?: unknown
    } | null
  }
}

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const payload = value as ErrorPayload
    const message = payload.response?.data?.message
    if (typeof message === 'string' && message) return message
    const detail = payload.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
  }
  if (value instanceof Error && value.message) return value.message
  return '请求失败'
}

function fileExtension(name: string): string {
  const index = name.lastIndexOf('.')
  return index > 0 ? name.slice(index + 1).toLowerCase() : ''
}

function extensionKind(name: string): PreviewKind {
  const extension = fileExtension(name)
  if (TEXT_EXTENSIONS.has(extension)) return 'text'
  if (IMAGE_EXTENSIONS.has(extension)) return 'image'
  if (extension === 'pdf') return 'pdf'
  return 'binary'
}

function sourceKey(): string {
  return `${props.agentId}\u0000${props.rootId}\u0000${props.file.path}`
}

function revokeObjectUrl() {
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = ''
  }
}

async function loadPreview() {
  const token = ++requestToken
  revokeObjectUrl()
  blob.value = null
  textContent.value = ''
  error.value = ''
  kind.value = 'binary'
  loadedKey = ''

  const agentId = props.agentId
  const rootId = props.rootId
  const path = props.file.path
  const key = sourceKey()

  if (!agentId || !rootId || !path) {
    loading.value = false
    error.value = '无法加载文件预览'
    return
  }

  loading.value = true

  try {
    const data = await workspaceApi.downloadFile(agentId, rootId, path)
    if (token !== requestToken) return

    blob.value = data
    loadedKey = key

    const detected = extensionKind(props.file.name)
    if (detected === 'text') {
      if (data.size > TEXT_PREVIEW_LIMIT) {
        kind.value = 'binary'
        return
      }
      const text = await data.text()
      if (token !== requestToken) return
      kind.value = 'text'
      textContent.value = text
      return
    }

    if (detected === 'image' || detected === 'pdf') {
      const url = URL.createObjectURL(data)
      if (token !== requestToken) {
        URL.revokeObjectURL(url)
        return
      }
      kind.value = detected
      objectUrl.value = url
      return
    }

    kind.value = 'binary'
  } catch (err) {
    if (token === requestToken) {
      error.value = describeError(err)
    }
  } finally {
    if (token === requestToken) loading.value = false
  }
}

async function downloadFile() {
  if (downloading.value) return

  const agentId = props.agentId
  const rootId = props.rootId
  const path = props.file.path
  const name = props.file.name
  const key = sourceKey()

  downloading.value = true
  try {
    const data =
      blob.value && loadedKey === key
        ? blob.value
        : await workspaceApi.downloadFile(agentId, rootId, path)
    const url = URL.createObjectURL(data)
    try {
      const link = document.createElement('a')
      link.href = url
      link.download = name
      link.rel = 'noopener'
      document.body.appendChild(link)
      link.click()
      link.remove()
    } finally {
      URL.revokeObjectURL(url)
    }
  } catch (err) {
    ElMessage.error(`下载失败：${describeError(err)}`)
  } finally {
    downloading.value = false
  }
}

watch(
  () => [props.agentId, props.rootId, props.file.path] as const,
  () => {
    void loadPreview()
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  requestToken += 1
  revokeObjectUrl()
})
</script>

<template>
  <section class="file-preview">
    <header class="preview-header">
      <div class="title-block">
        <div class="file-name" :title="file.name">{{ file.name }}</div>
        <div class="file-location" :title="`${rootName} / ${file.path}`">
          <span class="location-root">{{ rootName }}</span>
          <span class="location-sep">/</span>
          <span class="location-path">{{ file.path }}</span>
        </div>
      </div>

      <div class="header-actions">
        <el-button
          text
          circle
          size="small"
          :icon="Download"
          :loading="downloading"
          title="下载文件"
          aria-label="下载文件"
          @click="downloadFile"
        />
        <el-button
          text
          circle
          size="small"
          :icon="Close"
          title="关闭预览"
          aria-label="关闭预览"
          @click="emit('close')"
        />
      </div>
    </header>

    <div v-if="loading" class="preview-body loading-body">
      <el-skeleton :rows="8" animated />
    </div>

    <div v-else-if="error" class="preview-body state-box">
      <el-alert :title="error" type="error" show-icon :closable="false" />
      <el-button size="small" type="primary" plain @click="loadPreview">重试</el-button>
    </div>

    <div v-else-if="kind === 'text'" class="preview-body">
      <pre class="text-content">{{ textContent }}</pre>
    </div>

    <div v-else-if="kind === 'image'" class="preview-body image-body">
      <img class="image-content" :src="objectUrl" :alt="file.name" />
    </div>

    <div v-else-if="kind === 'pdf'" class="preview-body pdf-body">
      <iframe class="pdf-frame" :src="objectUrl" :title="`${file.name} PDF 预览`" />
    </div>

    <div v-else class="preview-body state-box">
      <el-icon class="binary-icon"><Document /></el-icon>
      <div class="binary-name">{{ file.name }}</div>
      <div class="binary-meta">{{ formatBytes(blob?.size ?? file.size) }}</div>
      <p class="binary-hint">该文件不支持预览，请下载查看</p>
      <el-button
        size="small"
        type="primary"
        plain
        :icon="Download"
        :loading="downloading"
        @click="downloadFile"
      >
        下载文件
      </el-button>
    </div>
  </section>
</template>

<style scoped>
.file-preview {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: var(--app-surface);
  color: var(--app-text);
}

.preview-header {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex: none;
  padding: 10px 10px 10px 12px;
  border-bottom: 1px solid var(--app-border);
  min-width: 0;
}

.title-block {
  flex: 1 1 auto;
  min-width: 0;
}

.file-name {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-location {
  display: flex;
  align-items: baseline;
  gap: 3px;
  margin-top: 2px;
  min-width: 0;
  color: var(--app-muted);
  font-size: 11px;
  line-height: 1.4;
}

.location-root,
.location-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.location-root {
  flex: none;
  max-width: 50%;
}

.location-path {
  flex: 0 1 auto;
  min-width: 0;
}

.location-sep {
  flex: none;
  color: var(--app-border);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: none;
}

.header-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.preview-body {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.loading-body {
  padding: 16px 12px;
  overflow: auto;
}

.state-box {
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 20px 16px;
  text-align: center;
  overflow: auto;
}

.state-box :deep(.el-alert) {
  width: 100%;
  text-align: left;
}

.text-content {
  flex: 1 1 auto;
  min-height: 0;
  margin: 0;
  padding: 12px;
  overflow: auto;
  background: var(--app-bg);
  color: var(--app-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  tab-size: 2;
}

.image-body {
  align-items: center;
  justify-content: center;
  padding: 12px;
  background: var(--app-bg);
}

.image-content {
  display: block;
  max-width: 100%;
  max-height: 100%;
  width: auto;
  height: auto;
  min-width: 0;
  min-height: 0;
  object-fit: contain;
}

.pdf-body {
  background: var(--app-bg);
}

.pdf-frame {
  flex: 1 1 auto;
  width: 100%;
  height: 100%;
  min-height: 0;
  border: none;
  background: var(--app-surface);
}

.binary-icon {
  font-size: 40px;
  color: var(--app-muted);
}

.binary-name {
  max-width: 100%;
  font-size: 12.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.binary-meta {
  color: var(--app-muted);
  font-size: 11.5px;
}

.binary-hint {
  margin: 0;
  max-width: 220px;
  color: var(--app-muted);
  font-size: 12px;
  line-height: 1.5;
}
</style>
