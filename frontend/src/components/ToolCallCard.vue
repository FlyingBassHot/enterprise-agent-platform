<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { formatDuration } from '@/utils/format'

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

const props = withDefaults(
  defineProps<{
    name: string
    status?: string | null
    ok?: boolean | null
    durationMs?: number | null
    arguments?: Record<string, unknown> | null
    error?: string | null
  }>(),
  { status: null, ok: null, durationMs: null, arguments: null, error: null },
)

const expanded = ref(false)

const statusInfo = computed<{ label: string; type: TagType }>(() => {
  switch (props.status) {
    case 'running':
      return { label: '运行中', type: 'primary' }
    case 'success':
      return { label: '成功', type: 'success' }
    case 'failed':
      return { label: '失败', type: 'danger' }
    case 'rejected':
      return { label: '已拒绝', type: 'danger' }
    case 'denied':
      return { label: '已阻止', type: 'warning' }
    case 'timeout':
      return { label: '超时', type: 'warning' }
    case 'pending':
      return { label: '等待中', type: 'info' }
    default:
      return {
        label: props.status || (props.ok === true ? '成功' : props.ok === false ? '失败' : '等待中'),
        type: props.ok === true ? 'success' : props.ok === false ? 'danger' : 'info',
      }
  }
})

const hasArguments = computed(
  () => !!props.arguments && Object.keys(props.arguments).length > 0,
)
</script>

<template>
  <div class="tool-card" :class="{ expanded }">
    <div class="tool-head">
      <div class="tool-identity">
        <span class="tool-icon">TR</span>
        <div>
          <div class="tool-name">{{ name }}</div>
          <div class="tool-meta">
            <el-tag :type="statusInfo.type" size="small" effect="light" round>
              {{ statusInfo.label }}
            </el-tag>
            <span v-if="durationMs !== null && durationMs !== undefined" class="duration">
              {{ formatDuration(durationMs) }}
            </span>
          </div>
        </div>
      </div>
      <el-button v-if="hasArguments" link class="toggle" @click="expanded = !expanded">
        参数
        <el-icon class="arrow" :class="{ open: expanded }"><ArrowRight /></el-icon>
      </el-button>
    </div>
    <div v-if="error" class="tool-error">{{ error }}</div>
    <el-collapse-transition>
      <div v-if="expanded && hasArguments" class="tool-args">
        <JsonBlock :value="arguments" max-height="200px" />
      </div>
    </el-collapse-transition>
  </div>
</template>

<style scoped>
.tool-card {
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 10px 12px;
  background: #fbfcfe;
}

.tool-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.tool-identity {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}

.tool-icon {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border-radius: 8px;
  display: grid;
  place-items: center;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.4px;
  color: #fff;
  background: linear-gradient(135deg, #4b5563, #1f2937);
}

.tool-name {
  font-size: 13px;
  font-weight: 600;
  word-break: break-all;
}

.tool-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 5px;
}

.duration {
  font-size: 11px;
  color: var(--app-muted);
}

.toggle {
  font-size: 12px;
  flex: 0 0 auto;
}

.arrow {
  margin-left: 2px;
  transition: transform 0.15s ease;
}

.arrow.open {
  transform: rotate(90deg);
}

.tool-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-color-danger);
  word-break: break-word;
}

.tool-args {
  margin-top: 10px;
}
</style>
