<script setup lang="ts">
import { nextTick, reactive, ref, watch } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { formatDate } from '@/utils/format'

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

interface TimelineEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
  created_at?: string
}

const props = withDefaults(
  defineProps<{
    events: TimelineEvent[]
    maxHeight?: string
    autoScroll?: boolean
  }>(),
  { maxHeight: '560px', autoScroll: false },
)

const expanded = reactive(new Set<number>())
const container = ref<HTMLElement | null>(null)

const BADGES: Record<string, { label: string; type: TagType }> = {
  execution_started: { label: '开始', type: 'primary' },
  message_delta: { label: '文本增量', type: 'info' },
  message_completed: { label: '消息完成', type: 'success' },
  tool_call_started: { label: '工具开始', type: 'warning' },
  tool_call_completed: { label: '工具完成', type: 'success' },
  approval_required: { label: '待审批', type: 'warning' },
  approval_resolved: { label: '审批结果', type: 'info' },
  execution_completed: { label: '执行完成', type: 'success' },
  execution_failed: { label: '执行失败', type: 'danger' },
  execution_cancelled: { label: '已取消', type: 'info' },
}

function badge(type: string): { label: string; type: TagType } {
  return BADGES[type] || { label: type, type: 'info' }
}

function clip(value: unknown, limit = 80): string {
  if (value === null || value === undefined || value === '') return ''
  const raw = typeof value === 'string' ? value : JSON.stringify(value)
  if (!raw) return ''
  return raw.length > limit ? `${raw.slice(0, limit)}…` : raw
}

function summary(event: TimelineEvent): string {
  const payload = event.payload || {}
  switch (event.type) {
    case 'execution_started':
      return `Agent ${clip(payload.agent_id, 12)} · v${payload.agent_version ?? '-'}`
    case 'message_delta':
      return clip(payload.delta ?? payload.content)
    case 'message_completed': {
      const content = clip(payload.content, 60)
      return `${content || '（无文本）'}${payload.finish_reason ? ` · ${payload.finish_reason}` : ''}`
    }
    case 'tool_call_started':
      return `${payload.tool_name || '工具'} · ${clip(payload.arguments, 60)}`
    case 'tool_call_completed': {
      const duration = payload.duration_ms
      return [
        payload.tool_name || '工具',
        payload.status || (payload.ok ? 'success' : 'failed'),
        duration !== undefined ? `${duration} ms` : '',
      ]
        .filter(Boolean)
        .join(' · ')
    }
    case 'approval_required':
      return `${payload.tool_name || '工具'} · 风险 ${payload.risk_level || '-'}`
    case 'approval_resolved':
      return `${payload.tool_name || '工具'} · ${payload.approved ? '已批准' : '已拒绝'}`
    case 'execution_completed':
      return clip(payload.output, 100) || '执行完成'
    case 'execution_failed':
      return clip(payload.error, 120)
    case 'execution_cancelled':
      return clip(payload.reason ?? payload.error, 100) || '执行已取消'
    default:
      return clip(payload, 80)
  }
}

function toggle(seq: number) {
  if (expanded.has(seq)) expanded.delete(seq)
  else expanded.add(seq)
}

watch(
  () => props.events.length,
  async () => {
    if (!props.autoScroll) return
    await nextTick()
    const element = container.value
    if (element) element.scrollTop = element.scrollHeight
  },
)
</script>

<template>
  <div ref="container" class="timeline" :style="{ maxHeight }">
    <div v-if="!events.length" class="empty">暂无事件</div>
    <div v-for="event in events" :key="event.seq" class="timeline-item">
      <div class="rail">
        <span class="dot" :class="badge(event.type).type" />
      </div>
      <div class="body">
        <button type="button" class="head" @click="toggle(event.seq)">
          <span class="seq mono">#{{ event.seq }}</span>
          <el-tag :type="badge(event.type).type" size="small" effect="light">
            {{ badge(event.type).label }}
          </el-tag>
          <span class="summary">{{ summary(event) }}</span>
          <span v-if="event.created_at" class="time">{{ formatDate(event.created_at) }}</span>
          <el-icon class="arrow" :class="{ open: expanded.has(event.seq) }">
            <ArrowRight />
          </el-icon>
        </button>
        <div v-if="expanded.has(event.seq)" class="payload">
          <JsonBlock :value="event.payload" max-height="260px" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  overflow-y: auto;
  padding-right: 4px;
}

.empty {
  color: var(--app-muted);
  font-size: 12.5px;
  padding: 18px 0;
}

.timeline-item {
  display: flex;
  gap: 10px;
  align-items: stretch;
}

.rail {
  width: 12px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 6px;
}

.rail::after {
  content: '';
  flex: 1;
  width: 1px;
  background: var(--app-border);
}

.timeline-item:last-child .rail::after {
  display: none;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
  background: #94a3b8;
}

.dot.primary {
  background: var(--el-color-primary);
}

.dot.success {
  background: var(--el-color-success);
}

.dot.warning {
  background: var(--el-color-warning);
}

.dot.danger {
  background: var(--el-color-danger);
}

.dot.info {
  background: #94a3b8;
}

.body {
  flex: 1;
  min-width: 0;
  padding-bottom: 8px;
}

.head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  padding: 4px 6px;
  border-radius: 8px;
  text-align: left;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.head:hover {
  background: rgba(15, 23, 42, 0.04);
}

.seq {
  color: var(--app-muted);
  font-size: 11px;
  flex: 0 0 auto;
}

.summary {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.time {
  font-size: 10.5px;
  color: var(--app-muted);
  flex: 0 0 auto;
}

.arrow {
  font-size: 12px;
  color: var(--app-muted);
  transition: transform 0.15s ease;
  flex: 0 0 auto;
}

.arrow.open {
  transform: rotate(90deg);
}

.payload {
  padding: 4px 6px 8px;
}
</style>
