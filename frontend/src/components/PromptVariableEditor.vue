<script setup lang="ts">
import { Delete, Plus } from '@element-plus/icons-vue'
import type { PromptVariable } from '@/types/api'

const variables = defineModel<PromptVariable[]>({ required: true })

function addVariable() {
  variables.value = [...variables.value, { name: '', required: false, default: '' }]
}

function removeVariable(index: number) {
  variables.value = variables.value.filter((_, i) => i !== index)
}

function updateName(index: number, name: string) {
  variables.value = variables.value.map((item, i) =>
    i === index ? { ...item, name } : item,
  )
}

function updateRequired(index: number, required: boolean) {
  variables.value = variables.value.map((item, i) =>
    i === index ? { ...item, required } : item,
  )
}

function updateDefault(index: number, value: string) {
  variables.value = variables.value.map((item, i) =>
    i === index ? { ...item, default: value } : item,
  )
}
</script>

<template>
  <div class="variable-editor">
    <el-table v-if="variables.length" :data="variables" size="small" border>
      <el-table-column label="变量名" min-width="160">
        <template #default="{ row, $index }">
          <el-input
            :model-value="row.name"
            placeholder="如 customer_name"
            @update:model-value="(value: string) => updateName($index, value)"
          />
        </template>
      </el-table-column>
      <el-table-column label="必填" width="76" align="center">
        <template #default="{ row, $index }">
          <el-switch
            :model-value="row.required"
            size="small"
            @change="(value: string | number | boolean) => updateRequired($index, value === true)"
          />
        </template>
      </el-table-column>
      <el-table-column label="默认值" min-width="180">
        <template #default="{ row, $index }">
          <el-input
            :model-value="row.default"
            placeholder="可选"
            @update:model-value="(value: string) => updateDefault($index, value)"
          />
        </template>
      </el-table-column>
      <el-table-column width="64" align="center">
        <template #default="{ $index }">
          <el-button link type="danger" :icon="Delete" @click="removeVariable($index)" />
        </template>
      </el-table-column>
    </el-table>
    <div v-else class="muted empty-tip">尚未定义变量，可在模板中使用 &#123;&#123; variable_name &#125;&#125; 占位。</div>
    <el-button class="add-btn" size="small" :icon="Plus" @click="addVariable">添加变量</el-button>
  </div>
</template>

<style scoped>
.variable-editor {
  width: 100%;
}

.empty-tip {
  padding: 10px 0;
  font-size: 12px;
}

.add-btn {
  margin-top: 10px;
}
</style>
