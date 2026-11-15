<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { skillsApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { SkillInfo, SkillVersionInfo } from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

interface SkillDetail extends SkillInfo {
  instructions?: string
}

const auth = useAuthStore()

const skills = ref<SkillInfo[]>([])
const loading = ref(false)
const error = ref('')
const syncing = ref(false)
const togglingId = ref('')

const keyword = ref('')
const enabledFilter = ref('all')

const detailVisible = ref(false)
const detailSkill = ref<SkillInfo | null>(null)
const detailInstructions = ref('')
const versions = ref<SkillVersionInfo[]>([])
const versionsLoading = ref(false)
const versionsError = ref('')
const selectedVersionId = ref('')

const canWrite = computed(() => auth.has('skills:write'))

const filteredSkills = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  return skills.value.filter((skill) => {
    if (enabledFilter.value === 'enabled' && !skill.enabled) return false
    if (enabledFilter.value === 'disabled' && skill.enabled) return false
    if (!text) return true
    return (
      skill.name.toLowerCase().includes(text) ||
      skill.description.toLowerCase().includes(text)
    )
  })
})

const selectedVersion = computed(
  () => versions.value.find((item) => item.id === selectedVersionId.value) || null,
)

const enabledCount = computed(() => skills.value.filter((skill) => skill.enabled).length)

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    skills.value = await skillsApi.list()
  } catch (err) {
    skills.value = []
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

async function syncSkills() {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有 Skill 同步权限')
    return
  }
  syncing.value = true
  try {
    const synced = await skillsApi.sync()
    ElMessage.success(`同步完成，共 ${synced.length} 个 Skill`)
    await load()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    syncing.value = false
  }
}

async function toggleEnabled(skill: SkillInfo) {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有 Skill 启停权限')
    return
  }
  togglingId.value = skill.id
  try {
    const updated = skill.enabled
      ? await skillsApi.disable(skill.id)
      : await skillsApi.enable(skill.id)
    const index = skills.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) skills.value[index] = updated
    ElMessage.success(updated.enabled ? 'Skill 已启用' : 'Skill 已停用')
  } catch {
    // interceptor surfaces the backend message
  } finally {
    togglingId.value = ''
  }
}

async function openDetail(skill: SkillInfo) {
  detailSkill.value = skill
  detailVisible.value = true
  detailInstructions.value = ''
  versions.value = []
  versionsError.value = ''
  selectedVersionId.value = ''
  versionsLoading.value = true
  const [detailResult, versionResult] = await Promise.allSettled([
    skillsApi.get(skill.id).then((data) => data as SkillDetail),
    skillsApi.versions(skill.id),
  ])
  if (detailResult.status === 'fulfilled') {
    detailInstructions.value = detailResult.value.instructions || ''
  }
  if (versionResult.status === 'fulfilled') {
    versions.value = versionResult.value
    selectedVersionId.value =
      [...versionResult.value].sort((a, b) => b.version - a.version)[0]?.id || ''
  } else {
    versionsError.value = describeError(versionResult.reason)
  }
  versionsLoading.value = false
}

onMounted(load)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="Skill 管理" subtitle="从代码/插件目录同步 Skill，治理依赖与启停">
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button v-if="canWrite" type="primary" :loading="syncing" @click="syncSkills">
          同步 Skill
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <div class="card">
      <div class="toolbar filter-bar">
        <div class="filter-group">
          <el-input
            v-model="keyword"
            size="small"
            :prefix-icon="Search"
            placeholder="搜索名称或描述"
            clearable
            class="keyword-input"
          />
          <el-select v-model="enabledFilter" size="small" class="filter-select">
            <el-option label="全部状态" value="all" />
            <el-option label="已启用" value="enabled" />
            <el-option label="已停用" value="disabled" />
          </el-select>
        </div>
        <span class="muted">共 {{ filteredSkills.length }} 个 Skill · 启用 {{ enabledCount }}</span>
      </div>

      <el-table v-loading="loading" :data="filteredSkills" row-key="id" size="small">
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span class="muted mono">{{ shortId(row.id, 12) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="描述" min-width="220">
          <template #default="{ row }">
            <span class="muted desc">{{ row.description || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }">
            <span class="mono">v{{ row.current_version }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="94">
          <template #default="{ row }">
            <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
          </template>
        </el-table-column>
        <el-table-column label="依赖工具" min-width="180">
          <template #default="{ row }">
            <div v-if="row.required_tools.length" class="tag-row">
              <el-tag
                v-for="tool in row.required_tools"
                :key="tool"
                size="small"
                effect="plain"
                class="mono"
              >
                {{ tool }}
              </el-tag>
            </div>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="所需权限" min-width="180">
          <template #default="{ row }">
            <div v-if="row.required_permissions.length" class="tag-row">
              <el-tag
                v-for="permission in row.required_permissions"
                :key="permission"
                size="small"
                effect="plain"
                class="mono"
              >
                {{ permission }}
              </el-tag>
            </div>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="166">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="158" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" @click="openDetail(row)">详情</el-button>
              <el-button
                v-if="canWrite"
                link
                :loading="togglingId === row.id"
                @click="toggleEnabled(row)"
              >
                {{ row.enabled ? '停用' : '启用' }}
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无 Skill，点击同步从代码目录导入" :image-size="80" />
        </template>
      </el-table>
    </div>

    <el-drawer
      v-model="detailVisible"
      size="640px"
      :title="detailSkill ? detailSkill.name : 'Skill 详情'"
    >
      <template v-if="detailSkill">
        <div class="detail-head">
          <div>
            <div class="detail-sub muted">{{ detailSkill.description || '暂无描述' }}</div>
            <div class="detail-meta">
              <span class="mono">v{{ detailSkill.current_version }}</span>
              <StatusTag :status="detailSkill.enabled ? 'enabled' : 'disabled'" />
              <span v-if="detailSkill.source_path" class="muted mono path">
                {{ detailSkill.source_path }}
              </span>
            </div>
          </div>
        </div>

        <div class="block">
          <div class="block-label">依赖工具</div>
          <div v-if="detailSkill.required_tools.length" class="tag-row">
            <el-tag
              v-for="tool in detailSkill.required_tools"
              :key="tool"
              size="small"
              effect="plain"
              class="mono"
            >
              {{ tool }}
            </el-tag>
          </div>
          <span v-else class="muted">无</span>
        </div>

        <div class="block">
          <div class="block-label">所需权限</div>
          <div v-if="detailSkill.required_permissions.length" class="tag-row">
            <el-tag
              v-for="permission in detailSkill.required_permissions"
              :key="permission"
              size="small"
              type="warning"
              effect="plain"
              class="mono"
            >
              {{ permission }}
            </el-tag>
          </div>
          <span v-else class="muted">无</span>
        </div>

        <div v-if="detailSkill.inputs.length || detailSkill.outputs.length" class="block">
          <div class="block-label">输入 / 输出</div>
          <div class="io-grid">
            <div>
              <span class="muted">输入</span>
              <div class="tag-row">
                <el-tag v-for="item in detailSkill.inputs" :key="item" size="small" effect="plain">
                  {{ item }}
                </el-tag>
                <span v-if="!detailSkill.inputs.length" class="muted">-</span>
              </div>
            </div>
            <div>
              <span class="muted">输出</span>
              <div class="tag-row">
                <el-tag v-for="item in detailSkill.outputs" :key="item" size="small" effect="plain">
                  {{ item }}
                </el-tag>
                <span v-if="!detailSkill.outputs.length" class="muted">-</span>
              </div>
            </div>
          </div>
        </div>

        <el-alert
          v-if="versionsError"
          type="error"
          :closable="false"
          show-icon
          :title="versionsError"
          class="gap"
        />

        <div v-if="!versionsLoading" class="block">
          <div class="block-label">指令</div>
          <pre v-if="detailInstructions" class="instructions">{{ detailInstructions }}</pre>
          <span v-else class="muted">暂无指令内容</span>
        </div>

        <el-skeleton v-if="versionsLoading" :rows="6" animated />

        <template v-else-if="versions.length">
          <div class="block-label">版本</div>
          <div class="version-list">
            <button
              v-for="version in versions"
              :key="version.id"
              type="button"
              class="version-item"
              :class="{ active: version.id === selectedVersionId }"
              @click="selectedVersionId = version.id"
            >
              <span class="version-no">v{{ version.version }}</span>
              <span class="muted">{{ formatDate(version.created_at) }}</span>
            </button>
          </div>

          <template v-if="selectedVersion">
            <div class="block-label">版本内容</div>
            <pre class="instructions">{{ selectedVersion.content || '（空）' }}</pre>
            <div class="block-label">快照</div>
            <JsonBlock :value="selectedVersion.snapshot" max-height="240px" />
          </template>
        </template>

        <el-empty
          v-else-if="!versionsError"
          description="暂无版本，点击同步生成"
          :image-size="70"
        />
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.filter-bar {
  margin-bottom: 14px;
}

.filter-group {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.keyword-input {
  width: 260px;
}

.filter-select {
  width: 140px;
}

.name-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.35;
}

.name {
  font-weight: 560;
}

.desc {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 12px;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.row-actions {
  display: flex;
  gap: 2px;
}

.detail-head {
  padding-bottom: 12px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 14px;
}

.detail-sub {
  font-size: 12px;
  line-height: 1.6;
}

.detail-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
  font-size: 11.5px;
}

.path {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.block {
  margin-bottom: 14px;
}

.block-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--app-muted);
  margin-bottom: 6px;
}

.io-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  font-size: 11.5px;
}

.version-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}

.version-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  border-radius: 9px;
  border: 1px solid var(--app-border);
  background: #fff;
  font-size: 12px;
  cursor: pointer;
  color: var(--app-text);
  font-family: inherit;
}

.version-item.active {
  border-color: var(--el-color-primary);
  background: rgba(47, 107, 255, 0.06);
}

.version-no {
  font-weight: 650;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
}

.instructions {
  margin: 0 0 14px;
  padding: 12px 14px;
  border-radius: 10px;
  background: #0f172a;
  color: #d7e2f5;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}

.gap {
  margin-top: 12px;
}
</style>
