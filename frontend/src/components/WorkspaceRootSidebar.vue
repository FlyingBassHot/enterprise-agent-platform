<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Document, Folder, FolderOpened, Plus, Refresh } from '@element-plus/icons-vue'
import { workspaceApi } from '@/api/platform'
import type { ConversationInfo, FileRootAccess, FileRootEntry } from '@/types/api'
import { formatBytes, formatDate } from '@/utils/format'

const props = defineProps<{
  agentId: string
  roots: FileRootAccess[]
  sessions: ConversationInfo[]
  activeRootId: string | null
  activeSessionId: string
  loadingSessions: boolean
}>()

const emit = defineEmits<{
  'select-root': [rootId: string | null]
  'new-session': [rootId: string | null]
  'select-session': [session: ConversationInfo]
  'select-file': [payload: { rootId: string; rootName: string; entry: FileRootEntry }]
  refresh: []
}>()

interface RootOption {
  id: string | null
  name: string
  count: number
}

interface PathSegment {
  name: string
  path: string
}

const activeTab = ref<'sessions' | 'files'>('sessions')
const filePath = ref('')
const entries = ref<FileRootEntry[]>([])
const entriesLoading = ref(false)
const entriesError = ref('')

let requestToken = 0

const unarchivedSessionCount = computed(
  () => props.sessions.filter((session) => session.root_id === null).length,
)

const rootOptions = computed<RootOption[]>(() => {
  const counts = new Map<string, number>()
  for (const session of props.sessions) {
    if (session.root_id !== null) {
      counts.set(session.root_id, (counts.get(session.root_id) ?? 0) + 1)
    }
  }

  const options: RootOption[] = props.roots.map((root) => ({
    id: root.id,
    name: root.name,
    count: counts.get(root.id) ?? 0,
  }))

  const known = new Set(props.roots.map((root) => root.id))
  for (const [rootId, count] of counts) {
    if (!known.has(rootId)) {
      options.push({ id: rootId, name: '其他会话', count })
    }
  }

  if (props.roots.length === 0 || unarchivedSessionCount.value > 0) {
    options.push({ id: null, name: '未归档', count: unarchivedSessionCount.value })
  }

  return options
})

const currentRoot = computed<FileRootAccess | null>(
  () => props.roots.find((root) => root.id === props.activeRootId) ?? null,
)

const canBrowseFiles = computed(() => currentRoot.value !== null)

const currentRootName = computed(() => currentRoot.value?.name ?? '文件根目录')

const visibleSessions = computed(() =>
  props.sessions.filter((session) => session.root_id === props.activeRootId),
)

const pathSegments = computed<PathSegment[]>(() => {
  let accumulated = ''
  return filePath.value
    .split('/')
    .filter(Boolean)
    .map((name) => {
      accumulated = accumulated ? `${accumulated}/${name}` : name
      return { name, path: accumulated }
    })
})

function describeError(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const payload = error as {
      response?: { data?: { message?: unknown; detail?: unknown } }
    }
    const message = payload.response?.data?.message
    if (typeof message === 'string' && message) return message
    const detail = payload.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
  }
  if (error instanceof Error && error.message) return error.message
  return '请求失败'
}

function resetFiles() {
  requestToken += 1
  filePath.value = ''
  entries.value = []
  entriesError.value = ''
  entriesLoading.value = false
}

async function loadEntries() {
  const rootId = props.activeRootId

  if (!rootId || !currentRoot.value) {
    requestToken += 1
    entries.value = []
    entriesError.value = ''
    entriesLoading.value = false
    return
  }

  const token = ++requestToken
  entriesLoading.value = true
  entriesError.value = ''

  try {
    const data = await workspaceApi.entries(props.agentId, rootId, filePath.value)
    if (token === requestToken) {
      entries.value = data
    }
  } catch (error) {
    if (token === requestToken) {
      entries.value = []
      entriesError.value = describeError(error)
    }
  } finally {
    if (token === requestToken) {
      entriesLoading.value = false
    }
  }
}

watch(
  [() => props.agentId, () => props.activeRootId],
  () => {
    resetFiles()
    if (activeTab.value === 'files') {
      void loadEntries()
    }
  },
)

watch(activeTab, (tab) => {
  if (tab === 'files') {
    void loadEntries()
  }
})

function selectRoot(rootId: string | null) {
  emit('select-root', rootId)
}

function createSession() {
  emit('new-session', props.activeRootId)
}

function selectSession(session: ConversationInfo) {
  emit('select-session', session)
}

function refresh() {
  emit('refresh')
}

function goToPath(path: string) {
  if (!canBrowseFiles.value || filePath.value === path) return
  filePath.value = path
  void loadEntries()
}

function handleEntry(entry: FileRootEntry) {
  if (entry.type === 'directory') {
    goToPath(entry.path)
    return
  }

  if (!props.activeRootId || !currentRoot.value) return
  emit('select-file', {
    rootId: props.activeRootId,
    rootName: currentRootName.value,
    entry,
  })
}
</script>

<template>
  <div class="workspace-root-sidebar">
    <header class="sidebar-header">
      <span class="sidebar-title">工作区</span>
      <el-button
        class="refresh-button"
        text
        circle
        size="small"
        :icon="Refresh"
        aria-label="刷新工作区"
        title="刷新"
        @click="refresh"
      />
    </header>

    <nav class="root-list" aria-label="文件根目录">
      <button
        v-for="option in rootOptions"
        :key="option.id ?? '__unarchived__'"
        type="button"
        class="root-item"
        :class="{ active: option.id === activeRootId }"
        :title="option.name"
        @click="selectRoot(option.id)"
      >
        <el-icon class="root-icon"><FolderOpened /></el-icon>
        <span class="root-name">{{ option.name }}</span>
        <span class="root-count">{{ option.count }}</span>
      </button>
    </nav>

    <div class="tab-bar" role="tablist" aria-label="工作区视图">
      <button
        type="button"
        role="tab"
        class="tab-button"
        :class="{ active: activeTab === 'sessions' }"
        :aria-selected="activeTab === 'sessions'"
        @click="activeTab = 'sessions'"
      >
        会话
      </button>
      <button
        type="button"
        role="tab"
        class="tab-button"
        :class="{ active: activeTab === 'files' }"
        :aria-selected="activeTab === 'files'"
        :disabled="!canBrowseFiles"
        @click="canBrowseFiles && (activeTab = 'files')"
      >
        文件
      </button>
    </div>

    <section v-if="activeTab === 'sessions'" class="panel sessions-panel" role="tabpanel">
      <div v-loading="loadingSessions" class="sessions-body">
        <el-button
          v-if="activeRootId === null || canBrowseFiles"
          class="new-session"
          type="primary"
          size="small"
          :icon="Plus"
          @click="createSession"
        >
          新建会话
        </el-button>

        <ul v-if="visibleSessions.length > 0" class="session-list">
          <li v-for="session in visibleSessions" :key="session.id">
            <button
              type="button"
              class="session-item"
              :class="{ active: session.id === activeSessionId }"
              :title="session.title || '未命名会话'"
              @click="selectSession(session)"
            >
              <span class="session-title">{{ session.title || '未命名会话' }}</span>
              <span class="session-time">{{ formatDate(session.updated_at) }}</span>
            </button>
          </li>
        </ul>

        <p v-if="!loadingSessions && visibleSessions.length === 0" class="empty-hint">暂无会话</p>
      </div>
    </section>

    <section v-else class="panel files-panel" role="tabpanel">
      <p v-if="!canBrowseFiles" class="empty-hint">
        当前工作区没有可用的文件根目录
      </p>

      <template v-else>
        <nav class="breadcrumbs" aria-label="目录路径">
          <button
            type="button"
            class="crumb"
            :class="{ current: filePath === '' }"
            :title="currentRootName"
            @click="goToPath('')"
          >
            {{ currentRootName }}
          </button>
          <template v-for="segment in pathSegments" :key="segment.path">
            <span class="crumb-sep">/</span>
            <button
              type="button"
              class="crumb"
              :class="{ current: segment.path === filePath }"
              :title="segment.name"
              @click="goToPath(segment.path)"
            >
              {{ segment.name }}
            </button>
          </template>
        </nav>

        <div v-loading="entriesLoading" class="entries-body">
          <button
            v-for="entry in entries"
            :key="entry.path"
            type="button"
            class="entry-item"
            :title="entry.name"
            :class="{ directory: entry.type === 'directory' }"
            @click="handleEntry(entry)"
          >
            <el-icon class="entry-icon">
              <Folder v-if="entry.type === 'directory'" />
              <Document v-else />
            </el-icon>
            <span class="entry-main">
              <span class="entry-name">{{ entry.name }}</span>
              <span v-if="entry.type === 'file'" class="entry-meta">
                {{ formatBytes(entry.size) }} · {{ formatDate(entry.modified_at) }}
              </span>
            </span>
          </button>

          <p v-if="entriesError" class="error-hint">{{ entriesError }}</p>
          <p v-else-if="!entriesLoading && entries.length === 0" class="empty-hint">
            当前目录为空
          </p>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.workspace-root-sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--app-surface);
  color: var(--app-text);
  overflow: hidden;
}

.sidebar-header {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--app-border);
}

.sidebar-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.refresh-button {
  flex: none;
  color: var(--app-muted);
}

.root-list {
  display: flex;
  flex: none;
  flex-direction: column;
  gap: 2px;
  max-height: 34%;
  padding: 8px;
  overflow-y: auto;
  border-bottom: 1px solid var(--app-border);
}

.root-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
  padding: 7px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--app-text);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.root-item:hover {
  background: var(--app-bg);
}

.root-item.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}

.root-icon {
  flex: none;
  color: var(--app-muted);
}

.root-item.active .root-icon {
  color: var(--el-color-primary);
}

.root-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.root-count {
  flex: none;
  min-width: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--app-bg);
  color: var(--app-muted);
  font-size: 11px;
  line-height: 16px;
  text-align: center;
}

.root-item.active .root-count {
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
}

.tab-bar {
  display: flex;
  flex: none;
  padding: 0 8px;
  border-bottom: 1px solid var(--app-border);
}

.tab-button {
  position: relative;
  flex: 1 1 0;
  min-width: 0;
  padding: 8px 4px;
  border: 0;
  background: transparent;
  color: var(--app-muted);
  font-size: 13px;
  cursor: pointer;
}

.tab-button:hover {
  color: var(--app-text);
}

.tab-button.active {
  color: var(--el-color-primary);
  font-weight: 600;
}

.tab-button.active::after {
  position: absolute;
  right: 20%;
  bottom: -1px;
  left: 20%;
  height: 2px;
  border-radius: 1px;
  background: var(--el-color-primary);
  content: '';
}

.panel {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
}

.sessions-panel,
.sessions-body {
  min-height: 0;
}

.sessions-body {
  position: relative;
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  padding: 8px;
  overflow: hidden;
}

.new-session {
  flex: none;
  width: 100%;
}

.session-list {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  overflow-y: auto;
}

.session-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  min-width: 0;
  padding: 7px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--app-text);
  text-align: left;
  cursor: pointer;
}

.session-item:hover {
  background: var(--app-bg);
}

.session-item.active {
  background: var(--el-color-primary-light-9);
}

.session-title {
  min-width: 0;
  overflow: hidden;
  font-size: 13px;
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item.active .session-title {
  color: var(--el-color-primary);
  font-weight: 600;
}

.session-time {
  color: var(--app-muted);
  font-size: 11px;
  line-height: 15px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.files-panel {
  padding: 8px;
}

.breadcrumbs {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  align-items: center;
  gap: 0;
  max-height: 58px;
  padding-bottom: 6px;
  overflow-y: auto;
  border-bottom: 1px solid var(--app-border);
}

.crumb {
  max-width: 100%;
  padding: 1px 2px;
  overflow: hidden;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--el-color-primary);
  font-size: 12px;
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.crumb:hover {
  background: var(--app-bg);
}

.crumb.current {
  color: var(--app-text);
  font-weight: 600;
  cursor: default;
}

.crumb-sep {
  color: var(--app-muted);
  font-size: 11px;
}

.entries-body {
  position: relative;
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
  margin-top: 6px;
  overflow-y: auto;
}

.entry-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  min-width: 0;
  padding: 6px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--app-text);
  text-align: left;
  cursor: pointer;
}

.entry-item:hover {
  background: var(--app-bg);
}

.entry-icon {
  flex: none;
  margin-top: 2px;
  color: var(--app-muted);
}

.entry-item.directory .entry-icon {
  color: var(--el-color-primary);
}

.entry-main {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.entry-name {
  min-width: 0;
  overflow: hidden;
  font-size: 13px;
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.entry-meta {
  color: var(--app-muted);
  font-size: 11px;
  line-height: 15px;
  overflow-wrap: anywhere;
}

.empty-hint {
  margin: 16px 8px;
  color: var(--app-muted);
  font-size: 12px;
  line-height: 18px;
  text-align: center;
}

.error-hint {
  margin: 12px 8px;
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 18px;
  overflow-wrap: anywhere;
}
</style>
