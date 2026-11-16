<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status?: string | null }>()

const statusMap: Record<string, { label: string; type: string }> = {
  draft: { label: '草稿', type: 'info' },
  validating: { label: '校验中', type: 'warning' },
  published: { label: '已发布', type: 'success' },
  disabled: { label: '已停用', type: 'info' },
  archived: { label: '已归档', type: 'info' },
  pending: { label: '等待中', type: 'warning' },
  running: { label: '执行中', type: 'primary' },
  waiting_approval: { label: '待审批', type: 'warning' },
  completed: { label: '已完成', type: 'success' },
  incomplete: { label: '未完成（可继续）', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' },
  approved: { label: '已批准', type: 'success' },
  rejected: { label: '已拒绝', type: 'danger' },
  success: { label: '成功', type: 'success' },
  timeout: { label: '超时', type: 'warning' },
  denied: { label: '被拒绝', type: 'danger' },
  enabled: { label: '启用', type: 'success' },
  low: { label: '低风险', type: 'success' },
  medium: { label: '中风险', type: 'warning' },
  high: { label: '高风险', type: 'danger' },
  critical: { label: '极高风险', type: 'danger' },
}

const info = computed(
  () => statusMap[props.status || ''] || { label: props.status || '未知', type: 'info' },
)
</script>

<template>
  <el-tag :type="info.type as any" size="small" effect="light" round>
    {{ info.label }}
  </el-tag>
</template>
