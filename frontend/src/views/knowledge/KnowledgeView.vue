<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile, UploadInstance } from 'element-plus'
import { Plus, Refresh, Search, Upload } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { knowledgeApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type {
  KnowledgeBaseInfo,
  KnowledgeDocumentInfo,
  RetrievalResult,
} from '@/types/api'
import { formatBytes, formatDate } from '@/utils/format'

const auth = useAuthStore()

const bases = ref<KnowledgeBaseInfo[]>([])
const basesLoading = ref(false)
const basesError = ref('')

const selectedBase = ref<KnowledgeBaseInfo | null>(null)
const activeTab = ref<'documents' | 'search'>('documents')

const documents = ref<KnowledgeDocumentInfo[]>([])
const documentsLoading = ref(false)
const documentsError = ref('')
const uploading = ref(false)
const busyDocumentId = ref('')

const uploadRef = ref<UploadInstance>()

const searchQuery = ref('')
const searchTopK = ref(5)
const metadataFilterText = ref('')
const searchResults = ref<RetrievalResult[]>([])
const searching = ref(false)
const searchError = ref('')
const searchPerformed = ref(false)

const baseDialogVisible = ref(false)
const editingBase = ref<KnowledgeBaseInfo | null>(null)
const savingBase = ref(false)
const baseForm = ref({
  name: '',
  description: '',
  access_scope: 'private',
})

const accessScopes = [
  { value: 'public', label: '公开' },
  { value: 'department', label: '部门' },
  { value: 'private', label: '私有' },
]

const allowedExtensions = ['xlsx', 'xls', 'csv', 'pdf', 'docx', 'txt', 'md']
const acceptTypes = '.xlsx,.xls,.csv,.pdf,.docx,.txt,.md'

const canWrite = computed(() => auth.has('knowledge:write'))

const documentStats = computed(() => {
  const ready = documents.value.filter((item) => item.status === 'ready').length
  const processing = documents.value.filter(
    (item) => item.status === 'pending' || item.status === 'processing',
  ).length
  return { ready, processing }
})

let pollTimer: number | undefined
let pollInFlight = false

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function scopeLabel(scope: string): string {
  return accessScopes.find((item) => item.value === scope)?.label || scope
}

function scopeTagType(scope: string): 'success' | 'warning' | 'info' {
  if (scope === 'public') return 'success'
  if (scope === 'department') return 'warning'
  return 'info'
}

function documentStatus(status: string): { label: string; type: 'success' | 'warning' | 'danger' | 'info' | 'primary' } {
  const map: Record<string, { label: string; type: 'success' | 'warning' | 'danger' | 'info' | 'primary' }> = {
    pending: { label: '等待处理', type: 'warning' },
    processing: { label: '处理中', type: 'primary' },
    ready: { label: '就绪', type: 'success' },
    failed: { label: '失败', type: 'danger' },
    deleted: { label: '已删除', type: 'info' },
  }
  return map[status] || { label: status, type: 'info' }
}

function isProcessing(status: string): boolean {
  return status === 'pending' || status === 'processing'
}

async function loadBases() {
  basesLoading.value = true
  basesError.value = ''
  try {
    bases.value = await knowledgeApi.listBases()
    const currentId = selectedBase.value?.id
    const matched = currentId ? bases.value.find((item) => item.id === currentId) : null
    if (matched) {
      selectedBase.value = matched
    } else if (bases.value.length) {
      await selectBase(bases.value[0])
    } else {
      selectedBase.value = null
      documents.value = []
      stopPolling()
    }
  } catch (err) {
    bases.value = []
    basesError.value = describeError(err)
  } finally {
    basesLoading.value = false
  }
}

async function selectBase(base: KnowledgeBaseInfo) {
  selectedBase.value = base
  activeTab.value = 'documents'
  stopPolling()
  searchResults.value = []
  searchError.value = ''
  searchPerformed.value = false
  await loadDocuments()
}

async function loadDocuments() {
  const base = selectedBase.value
  if (!base) {
    documents.value = []
    return
  }
  documentsLoading.value = true
  documentsError.value = ''
  try {
    const list = await knowledgeApi.documents(base.id)
    if (selectedBase.value?.id !== base.id) return
    documents.value = list
    selectedBase.value.document_count = list.length
    ensurePolling()
  } catch (err) {
    if (selectedBase.value?.id === base.id) {
      documents.value = []
      documentsError.value = describeError(err)
    }
  } finally {
    documentsLoading.value = false
  }
}

function ensurePolling() {
  const hasActive = documents.value.some((item) => isProcessing(item.status))
  if (!hasActive) {
    stopPolling()
    return
  }
  if (pollTimer === undefined) {
    pollTimer = window.setInterval(() => {
      void pollDocuments()
    }, 2500)
  }
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

function mergeDocument(updated: KnowledgeDocumentInfo) {
  const index = documents.value.findIndex((item) => item.id === updated.id)
  if (index >= 0) documents.value[index] = updated
}

async function pollDocuments() {
  if (pollInFlight) return
  const base = selectedBase.value
  if (!base) {
    stopPolling()
    return
  }
  const pending = documents.value.filter((item) => isProcessing(item.status))
  if (!pending.length) {
    stopPolling()
    return
  }
  pollInFlight = true
  try {
    const results = await Promise.allSettled(
      pending.map((item) => knowledgeApi.getDocument(item.id)),
    )
    if (selectedBase.value?.id !== base.id) return
    for (const result of results) {
      if (result.status === 'fulfilled') mergeDocument(result.value)
    }
    ensurePolling()
  } finally {
    pollInFlight = false
  }
}

async function handleFileChange(file: UploadFile) {
  uploadRef.value?.clearFiles()
  const raw = file.raw
  const base = selectedBase.value
  if (!raw || !base) return
  const extension = raw.name.split('.').pop()?.toLowerCase() || ''
  if (!allowedExtensions.includes(extension)) {
    ElMessage.warning(`仅支持 ${allowedExtensions.join(' / ')} 格式`)
    return
  }
  uploading.value = true
  activeTab.value = 'documents'
  const targetBaseId = base.id
  try {
    await knowledgeApi.uploadDocument(targetBaseId, raw)
    ElMessage.success('文档已上传，正在等待处理')
    if (selectedBase.value?.id === targetBaseId) await loadDocuments()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    uploading.value = false
  }
}

async function deleteDocument(document: KnowledgeDocumentInfo) {
  try {
    await ElMessageBox.confirm(`确定删除文档「${document.filename}」吗？`, '删除确认', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  busyDocumentId.value = document.id
  try {
    await knowledgeApi.removeDocument(document.id)
    ElMessage.success('文档已删除')
    await loadDocuments()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    busyDocumentId.value = ''
  }
}

async function reprocessDocument(document: KnowledgeDocumentInfo) {
  busyDocumentId.value = document.id
  try {
    const updated = await knowledgeApi.reprocessDocument(document.id)
    mergeDocument(updated)
    ensurePolling()
    ElMessage.success('已提交重新处理')
  } catch {
    // interceptor surfaces the backend message
  } finally {
    busyDocumentId.value = ''
  }
}

function openBaseDialog(base?: KnowledgeBaseInfo) {
  editingBase.value = base || null
  baseForm.value = base
    ? {
        name: base.name,
        description: base.description,
        access_scope: base.access_scope,
      }
    : { name: '', description: '', access_scope: 'private' }
  baseDialogVisible.value = true
}

async function saveBase() {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有知识库管理权限')
    return
  }
  const name = baseForm.value.name.trim()
  if (name.length < 2) {
    ElMessage.warning('知识库名称至少需要 2 个字符')
    return
  }
  savingBase.value = true
  try {
    if (editingBase.value) {
      const updated = await knowledgeApi.updateBase(editingBase.value.id, {
        name,
        description: baseForm.value.description,
        access_scope: baseForm.value.access_scope,
      })
      ElMessage.success('知识库已更新')
      if (selectedBase.value?.id === updated.id) selectedBase.value = updated
    } else {
      const created = await knowledgeApi.createBase({
        name,
        description: baseForm.value.description,
        access_scope: baseForm.value.access_scope,
      })
      ElMessage.success('知识库已创建')
      selectedBase.value = created
    }
    baseDialogVisible.value = false
    await loadBases()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingBase.value = false
  }
}

async function deleteBase(base: KnowledgeBaseInfo) {
  try {
    await ElMessageBox.confirm(
      `确定删除知识库「${base.name}」及其全部文档吗？该操作不可撤销。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await knowledgeApi.removeBase(base.id)
    ElMessage.success('知识库已删除')
    if (selectedBase.value?.id === base.id) {
      selectedBase.value = null
      documents.value = []
      stopPolling()
    }
    await loadBases()
  } catch {
    // interceptor surfaces the backend message
  }
}

function parseMetadataFilter(): Record<string, unknown> | null | 'invalid' {
  const text = metadataFilterText.value.trim()
  if (!text) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    ElMessage.error('metadata_filter 不是合法 JSON')
    return 'invalid'
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    ElMessage.error('metadata_filter 必须是 JSON 对象')
    return 'invalid'
  }
  return parsed as Record<string, unknown>
}

async function runSearch() {
  const base = selectedBase.value
  if (!base) return
  const query = searchQuery.value.trim()
  if (!query) {
    ElMessage.warning('请输入检索内容')
    return
  }
  const filter = parseMetadataFilter()
  if (filter === 'invalid') return
  searching.value = true
  searchError.value = ''
  searchResults.value = []
  searchPerformed.value = true
  try {
    searchResults.value = await knowledgeApi.search(base.id, {
      query,
      top_k: searchTopK.value,
      ...(filter ? { metadata_filter: filter } : {}),
    })
  } catch (err) {
    searchError.value = describeError(err)
  } finally {
    searching.value = false
  }
}

onMounted(loadBases)
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="知识库" subtitle="文档入库、切片向量化与检索测试">
      <template #actions>
        <el-button :loading="basesLoading" @click="loadBases">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openBaseDialog()">
          新建知识库
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="basesError" type="error" :closable="false" show-icon :title="basesError">
      <el-button link type="primary" @click="loadBases">重试</el-button>
    </el-alert>

    <div class="knowledge-layout">
      <div class="card kb-panel">
        <div class="panel-head">
          <h3>知识库</h3>
          <span class="muted">{{ bases.length }} 个</span>
        </div>

        <el-skeleton v-if="basesLoading && !bases.length" :rows="4" animated />

        <div v-else-if="bases.length" class="kb-list">
          <div
            v-for="base in bases"
            :key="base.id"
            class="kb-item"
            :class="{ active: selectedBase?.id === base.id }"
            role="button"
            tabindex="0"
            @click="selectBase(base)"
            @keyup.enter="selectBase(base)"
          >
            <div class="kb-main">
              <div class="kb-name">{{ base.name }}</div>
              <div class="muted kb-desc">{{ base.description || '暂无描述' }}</div>
              <div class="kb-meta">
                <el-tag size="small" effect="plain" :type="scopeTagType(base.access_scope)">
                  {{ scopeLabel(base.access_scope) }}
                </el-tag>
                <el-tag v-if="base.status !== 'active'" size="small" type="info" effect="plain">
                  {{ base.status === 'disabled' ? '已停用' : base.status }}
                </el-tag>
                <span class="muted">文档 {{ base.document_count ?? 0 }}</span>
              </div>
            </div>
            <div v-if="canWrite" class="kb-actions">
              <el-button link type="primary" @click.stop="openBaseDialog(base)">编辑</el-button>
              <el-button link type="danger" @click.stop="deleteBase(base)">删除</el-button>
            </div>
          </div>
        </div>

        <el-empty v-else description="暂无知识库" :image-size="70" />
      </div>

      <div class="card detail-panel">
        <template v-if="selectedBase">
          <div class="detail-head">
            <div>
              <div class="detail-title">{{ selectedBase.name }}</div>
              <div class="muted detail-sub">
                {{ scopeLabel(selectedBase.access_scope) }} · 切片大小
                {{ selectedBase.chunk_size }} · 重叠 {{ selectedBase.chunk_overlap }}
              </div>
            </div>
            <el-tag size="small" effect="plain">{{ selectedBase.embedding_model || '默认向量模型' }}</el-tag>
          </div>

          <el-tabs v-model="activeTab">
            <el-tab-pane label="文档" name="documents">
              <div class="doc-toolbar">
                <div class="muted">
                  {{ documents.length }} 个文档 · 就绪 {{ documentStats.ready }}
                  <template v-if="documentStats.processing"> · 处理中 {{ documentStats.processing }}</template>
                </div>
                <div class="doc-actions">
                  <el-button size="small" :loading="documentsLoading" @click="loadDocuments">
                    刷新
                  </el-button>
                  <el-upload
                    v-if="canWrite"
                    ref="uploadRef"
                    :auto-upload="false"
                    :show-file-list="false"
                    :accept="acceptTypes"
                    :on-change="handleFileChange"
                  >
                    <el-button type="primary" size="small" :icon="Upload" :loading="uploading">
                      上传文档
                    </el-button>
                  </el-upload>
                </div>
              </div>

              <div class="muted upload-hint">
                支持 {{ allowedExtensions.join(' / ') }}；上传后自动解析、切片并生成向量。
              </div>

              <el-alert
                v-if="documentsError"
                type="error"
                :closable="false"
                show-icon
                :title="documentsError"
                class="gap"
              />

              <el-table v-loading="documentsLoading" :data="documents" row-key="id" size="small">
                <el-table-column label="文件名" min-width="220">
                  <template #default="{ row }">
                    <div class="doc-name">
                      <span class="name">{{ row.filename }}</span>
                      <span v-if="row.error" class="doc-error">{{ row.error }}</span>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="112">
                  <template #default="{ row }">
                    <el-tag size="small" effect="light" :type="documentStatus(row.status).type">
                      {{ documentStatus(row.status).label }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="版本" width="72">
                  <template #default="{ row }"><span class="mono">v{{ row.version }}</span></template>
                </el-table-column>
                <el-table-column label="切片数" width="80">
                  <template #default="{ row }">{{ row.chunk_count ?? '-' }}</template>
                </el-table-column>
                <el-table-column label="大小" width="92">
                  <template #default="{ row }">{{ formatBytes(row.size) }}</template>
                </el-table-column>
                <el-table-column label="更新时间" width="166">
                  <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
                </el-table-column>
                <el-table-column v-if="canWrite" label="操作" width="150" fixed="right">
                  <template #default="{ row }">
                    <el-button
                      v-if="!isProcessing(row.status)"
                      link
                      type="primary"
                      :loading="busyDocumentId === row.id"
                      @click="reprocessDocument(row)"
                    >
                      重处理
                    </el-button>
                    <el-button
                      link
                      type="danger"
                      :loading="busyDocumentId === row.id"
                      @click="deleteDocument(row)"
                    >
                      删除
                    </el-button>
                  </template>
                </el-table-column>
                <template #empty>
                  <el-empty description="暂无文档，上传后开始解析" :image-size="70" />
                </template>
              </el-table>
            </el-tab-pane>

            <el-tab-pane label="检索测试" name="search">
              <el-form label-position="top" @submit.prevent="runSearch">
                <div class="search-grid">
                  <el-form-item label="查询内容" required>
                    <el-input
                      v-model="searchQuery"
                      type="textarea"
                      :rows="2"
                      placeholder="输入自然语言问题或关键词"
                    />
                  </el-form-item>
                  <el-form-item label="Top K">
                    <el-input-number v-model="searchTopK" :min="1" :max="20" class="full" />
                  </el-form-item>
                </div>
                <el-form-item label="metadata_filter（JSON，可选）">
                  <el-input
                    v-model="metadataFilterText"
                    type="textarea"
                    :rows="3"
                    class="mono-input"
                    placeholder='{"source": "policy"}'
                  />
                </el-form-item>
                <el-button type="primary" :icon="Search" :loading="searching" @click="runSearch">
                  执行检索
                </el-button>
              </el-form>

              <el-alert
                v-if="searchError"
                type="error"
                :closable="false"
                show-icon
                :title="searchError"
                class="gap"
              />

              <el-skeleton v-if="searching" :rows="4" animated class="gap" />

              <div v-else-if="searchResults.length" class="results">
                <div v-for="result in searchResults" :key="result.chunk_id" class="result-card">
                  <div class="result-head">
                    <span class="result-title">{{ result.document_title }}</span>
                    <el-tag size="small" type="success" effect="plain">
                      score {{ result.score.toFixed(4) }}
                    </el-tag>
                  </div>
                  <div class="result-content">{{ result.content }}</div>
                  <div v-if="Object.keys(result.metadata).length" class="result-meta">
                    <JsonBlock :value="result.metadata" max-height="120px" />
                  </div>
                </div>
              </div>

              <el-empty
                v-else-if="searchPerformed && !searchError"
                description="未命中任何切片"
                :image-size="70"
              />
              <el-empty
                v-else-if="!searchError"
                description="输入查询内容后执行检索测试"
                :image-size="70"
              />
            </el-tab-pane>
          </el-tabs>
        </template>

        <el-empty v-else description="选择左侧知识库查看文档与检索" :image-size="90" />
      </div>
    </div>

    <el-dialog
      v-model="baseDialogVisible"
      :title="editingBase ? '编辑知识库' : '新建知识库'"
      width="520px"
    >
      <el-form label-position="top" @submit.prevent="saveBase">
        <el-form-item label="名称" required>
          <el-input v-model="baseForm.name" maxlength="120" show-word-limit />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="baseForm.description"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="访问范围">
          <el-select v-model="baseForm.access_scope" class="full">
            <el-option
              v-for="scope in accessScopes"
              :key="scope.value"
              :label="scope.label"
              :value="scope.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="baseDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingBase" @click="saveBase">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.knowledge-layout {
  display: grid;
  grid-template-columns: 330px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

@media (max-width: 1080px) {
  .knowledge-layout {
    grid-template-columns: 1fr;
  }
}

.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
}

.panel-head h3 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 650;
}

.kb-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 640px;
  overflow-y: auto;
}

.kb-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: 11px;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
  outline: none;
}

.kb-item:hover {
  border-color: #b9cdfb;
}

.kb-item.active {
  border-color: var(--el-color-primary);
  background: rgba(47, 107, 255, 0.05);
}

.kb-main {
  min-width: 0;
}

.kb-name {
  font-weight: 600;
  font-size: 13px;
}

.kb-desc {
  font-size: 11.5px;
  margin-top: 3px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.kb-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 7px;
  font-size: 11px;
}

.kb-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  flex-shrink: 0;
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 8px;
}

.detail-title {
  font-size: 17px;
  font-weight: 660;
}

.detail-sub {
  font-size: 11.5px;
  margin-top: 3px;
}

.doc-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.doc-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.upload-hint {
  font-size: 11px;
  margin-bottom: 12px;
}

.doc-name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.4;
}

.name {
  font-weight: 550;
  word-break: break-all;
}

.doc-error {
  font-size: 11px;
  color: var(--el-color-danger);
  word-break: break-word;
}

.search-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 130px;
  gap: 0 16px;
}

.full {
  width: 100%;
}

.mono-input :deep(textarea) {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}

.gap {
  margin-top: 14px;
}

.results {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
}

.result-card {
  border: 1px solid var(--app-border);
  border-radius: 11px;
  padding: 12px 14px;
  background: #fbfcfe;
}

.result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.result-title {
  font-weight: 600;
  font-size: 13px;
}

.result-content {
  font-size: 12.5px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}

.result-meta {
  margin-top: 10px;
}
</style>
