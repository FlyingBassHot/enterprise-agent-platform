<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import { accessPoliciesApi, usersApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { AccessPolicyInfo, PermissionInfo, RoleInfo, UserInfo } from '@/types/api'
import { formatDate } from '@/utils/format'

const auth = useAuthStore()

const activeTab = ref<'users' | 'roles' | 'policies'>('users')
const users = ref<UserInfo[]>([])
const roles = ref<RoleInfo[]>([])
const permissions = ref<PermissionInfo[]>([])
const loading = ref(false)
const error = ref('')
const rolesError = ref('')

const userVisible = ref(false)
const editingUser = ref<UserInfo | null>(null)
const savingUser = ref(false)
const userForm = ref({
  username: '',
  password: '',
  display_name: '',
  email: '',
  role_names: [] as string[],
  is_active: true,
})

const roleVisible = ref(false)
const editingRole = ref<RoleInfo | null>(null)
const savingRole = ref(false)
const roleForm = ref({
  name: '',
  description: '',
  permission_codes: [] as string[],
})

const policies = ref<AccessPolicyInfo[]>([])
const policiesLoading = ref(false)
const policiesError = ref('')
const policyVisible = ref(false)
const editingPolicy = ref<AccessPolicyInfo | null>(null)
const savingPolicy = ref(false)
const policyForm = ref({
  name: '',
  description: '',
  effect: 'allow',
  resource_type: '',
  action: '*',
  subject_roles: '',
  subject_departments: '',
  tenant_ids: '',
  resource_ids: '',
  priority: 0,
  enabled: true,
})

const canManageUsers = computed(() => auth.has('users:manage'))
const canManageRoles = computed(() => auth.has('roles:manage'))
const isAdminRole = computed(() => editingRole.value?.name === 'admin')

const resourceLabels: Record<string, string> = {
  agent: 'Agent',
  prompt: 'Prompt',
  model: '模型',
  tool: '工具',
  execution: '执行',
  approval: '审批',
  user: '用户',
  role: '角色',
  platform: '平台',
  audit: '审计',
  knowledge: '知识库',
  skill: 'Skill',
  workflow: '工作流',
}

const effectOptions = [
  { value: 'allow', label: '允许（allow）' },
  { value: 'deny', label: '拒绝（deny）' },
]

const actionOptions = ['*', 'read', 'write', 'publish', 'execute', 'manage']

const resourceTypeOptions = computed(() => [
  '*',
  ...Object.keys(resourceLabels),
])

const groupedPermissions = computed(() => {
  const groups = new Map<string, PermissionInfo[]>()
  for (const permission of permissions.value) {
    const list = groups.get(permission.resource_type) || []
    list.push(permission)
    groups.set(permission.resource_type, list)
  }
  return Array.from(groups, ([type, items]) => ({
    type,
    label: resourceLabels[type] || type,
    items,
  }))
})

const roleOptions = computed(() => {
  const names = new Set(roles.value.map((role) => role.name))
  for (const user of users.value) {
    for (const role of user.roles) names.add(role)
  }
  return Array.from(names)
})

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

async function loadUsers() {
  error.value = ''
  const [userResult, roleResult, permissionResult] = await Promise.allSettled([
    usersApi.list(),
    canManageRoles.value ? usersApi.roles() : Promise.resolve([] as RoleInfo[]),
    canManageRoles.value ? usersApi.permissions() : Promise.resolve([] as PermissionInfo[]),
  ])

  if (userResult.status === 'fulfilled') {
    users.value = userResult.value
  } else {
    users.value = []
    error.value = describeError(userResult.reason)
  }

  if (roleResult.status === 'fulfilled') {
    roles.value = roleResult.value
    rolesError.value = ''
  } else if (canManageRoles.value) {
    roles.value = []
    rolesError.value = describeError(roleResult.reason)
  }

  if (permissionResult.status === 'fulfilled') {
    permissions.value = permissionResult.value
  } else if (canManageRoles.value && !rolesError.value) {
    rolesError.value = describeError(permissionResult.reason)
  }
}

async function load() {
  loading.value = true
  error.value = ''
  rolesError.value = ''
  await loadUsers()
  loading.value = false
}

function openUserDialog(user?: UserInfo) {
  editingUser.value = user || null
  userForm.value = user
    ? {
        username: user.username,
        password: '',
        display_name: user.display_name,
        email: user.email || '',
        role_names: [...user.roles],
        is_active: user.is_active,
      }
    : {
        username: '',
        password: '',
        display_name: '',
        email: '',
        role_names: roleOptions.value.includes('viewer') ? ['viewer'] : [],
        is_active: true,
      }
  userVisible.value = true
}

async function saveUser() {
  const username = userForm.value.username.trim()
  const password = userForm.value.password
  if (!editingUser.value) {
    if (username.length < 3) {
      ElMessage.warning('用户名至少需要 3 个字符')
      return
    }
    if (password.length < 8) {
      ElMessage.warning('密码至少需要 8 个字符')
      return
    }
  } else if (password && password.length < 8) {
    ElMessage.warning('新密码至少需要 8 个字符')
    return
  }
  savingUser.value = true
  try {
    if (editingUser.value) {
      const payload: Record<string, unknown> = {
        display_name: userForm.value.display_name,
        email: userForm.value.email.trim() || null,
        role_names: userForm.value.role_names,
        is_active: userForm.value.is_active,
      }
      if (password) payload.password = password
      await usersApi.update(editingUser.value.id, payload)
      ElMessage.success('用户已更新')
    } else {
      await usersApi.create({
        username,
        password,
        display_name: userForm.value.display_name,
        email: userForm.value.email.trim() || null,
        role_names: userForm.value.role_names,
      })
      ElMessage.success('用户已创建')
    }
    userVisible.value = false
    await loadUsers()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingUser.value = false
  }
}

function openRoleDialog(role?: RoleInfo) {
  editingRole.value = role || null
  roleForm.value = role
    ? {
        name: role.name,
        description: role.description,
        permission_codes: [...role.permissions],
      }
    : { name: '', description: '', permission_codes: [] }
  roleVisible.value = true
}

async function saveRole() {
  const name = roleForm.value.name.trim()
  if (!editingRole.value && name.length < 2) {
    ElMessage.warning('角色名称至少需要 2 个字符')
    return
  }
  if (!canManageRoles.value) {
    ElMessage.warning('当前账号没有角色管理权限')
    return
  }
  savingRole.value = true
  try {
    if (editingRole.value) {
      const payload: Record<string, unknown> = { description: roleForm.value.description }
      if (editingRole.value.name !== 'admin') {
        payload.permission_codes = roleForm.value.permission_codes
      }
      await usersApi.updateRole(editingRole.value.id, payload)
      ElMessage.success('角色已更新')
    } else {
      await usersApi.createRole({
        name,
        description: roleForm.value.description,
        permission_codes: roleForm.value.permission_codes,
      })
      ElMessage.success('角色已创建')
    }
    roleVisible.value = false
    await loadUsers()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingRole.value = false
  }
}

function effectTagType(effect: string): 'success' | 'danger' | 'info' {
  if (effect === 'allow') return 'success'
  if (effect === 'deny') return 'danger'
  return 'info'
}

function parseStringList(text: string, label: string): string[] | null {
  const value = text.trim()
  if (!value) return []
  if (value.startsWith('[')) {
    let parsed: unknown
    try {
      parsed = JSON.parse(value)
    } catch {
      ElMessage.error(`${label} 不是合法的 JSON 数组`)
      return null
    }
    if (!Array.isArray(parsed)) {
      ElMessage.error(`${label} 必须是 JSON 数组`)
      return null
    }
    const items: string[] = []
    for (const item of parsed) {
      if (typeof item !== 'string') {
        ElMessage.error(`${label} 中的每一项都必须是字符串`)
        return null
      }
      const trimmed = item.trim()
      if (trimmed) items.push(trimmed)
    }
    return items
  }
  return value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)
}

async function loadPolicies() {
  if (!canManageRoles.value) return
  policiesLoading.value = true
  policiesError.value = ''
  try {
    policies.value = await accessPoliciesApi.list()
  } catch (err) {
    policies.value = []
    policiesError.value = describeError(err)
  } finally {
    policiesLoading.value = false
  }
}

function refreshPage() {
  void load()
  if (canManageRoles.value) void loadPolicies()
}

function openPolicyDialog(policy?: AccessPolicyInfo) {
  editingPolicy.value = policy || null
  policyForm.value = policy
    ? {
        name: policy.name,
        description: policy.description,
        effect: policy.effect === 'deny' ? 'deny' : 'allow',
        resource_type: policy.resource_type,
        action: policy.action,
        subject_roles: policy.subject_roles.join(', '),
        subject_departments: policy.subject_departments.join(', '),
        tenant_ids: policy.tenant_ids.join(', '),
        resource_ids: policy.resource_ids.join(', '),
        priority: policy.priority,
        enabled: policy.enabled,
      }
    : {
        name: '',
        description: '',
        effect: 'allow',
        resource_type: '',
        action: '*',
        subject_roles: '',
        subject_departments: '',
        tenant_ids: '',
        resource_ids: '',
        priority: 0,
        enabled: true,
      }
  policyVisible.value = true
}

async function savePolicy() {
  if (!canManageRoles.value) {
    ElMessage.warning('当前账号没有访问策略管理权限')
    return
  }
  const name = policyForm.value.name.trim()
  if (!editingPolicy.value && name.length < 2) {
    ElMessage.warning('策略名称至少需要 2 个字符')
    return
  }
  const resourceType = policyForm.value.resource_type.trim()
  if (!resourceType) {
    ElMessage.warning('请填写资源类型')
    return
  }
  const subjectRoles = parseStringList(policyForm.value.subject_roles, 'subject_roles')
  if (!subjectRoles) return
  const departments = parseStringList(
    policyForm.value.subject_departments,
    'subject_departments',
  )
  if (!departments) return
  const tenants = parseStringList(policyForm.value.tenant_ids, 'tenant_ids')
  if (!tenants) return
  const resourceIds = parseStringList(policyForm.value.resource_ids, 'resource_ids')
  if (!resourceIds) return

  const payload: Record<string, unknown> = {
    description: policyForm.value.description,
    effect: policyForm.value.effect,
    resource_type: resourceType,
    action: policyForm.value.action.trim() || '*',
    subject_roles: subjectRoles,
    subject_departments: departments,
    tenant_ids: tenants,
    resource_ids: resourceIds,
    priority: Number(policyForm.value.priority) || 0,
    enabled: policyForm.value.enabled,
  }

  savingPolicy.value = true
  try {
    if (editingPolicy.value) {
      await accessPoliciesApi.update(editingPolicy.value.id, payload)
      ElMessage.success('访问策略已更新')
    } else {
      await accessPoliciesApi.create({ ...payload, name })
      ElMessage.success('访问策略已创建')
    }
    policyVisible.value = false
    await loadPolicies()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingPolicy.value = false
  }
}

async function deletePolicy(policy: AccessPolicyInfo) {
  if (!canManageRoles.value) return
  try {
    await ElMessageBox.confirm(`确定删除访问策略「${policy.name}」吗？`, '删除确认', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await accessPoliciesApi.remove(policy.id)
    ElMessage.success('访问策略已删除')
    await loadPolicies()
  } catch {
    // interceptor surfaces the backend message
  }
}

onMounted(() => {
  void load()
  if (canManageRoles.value) void loadPolicies()
})
</script>

<template>
  <div class="page-stack">
    <PageHeader title="用户与角色" subtitle="账号、角色、权限与 ABAC 访问策略；权限变更由服务端强制执行">
      <template #actions>
        <el-button :loading="loading || policiesLoading" @click="refreshPage">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button
          v-if="activeTab === 'users' && canManageUsers"
          type="primary"
          :icon="Plus"
          @click="openUserDialog()"
        >
          新建用户
        </el-button>
        <el-button
          v-if="activeTab === 'roles' && canManageRoles"
          type="primary"
          :icon="Plus"
          @click="openRoleDialog()"
        >
          新建角色
        </el-button>
        <el-button
          v-if="activeTab === 'policies' && canManageRoles"
          type="primary"
          :icon="Plus"
          @click="openPolicyDialog()"
        >
          新建策略
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <div class="card">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="用户" name="users">
          <el-table v-loading="loading" :data="users" row-key="id" size="small">
            <el-table-column label="用户名" min-width="150">
              <template #default="{ row }">
                <div class="name-cell">
                  <span class="name">{{ row.username }}</span>
                  <span class="muted">{{ row.display_name || '-' }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="邮箱" min-width="180">
              <template #default="{ row }">
                <span class="muted">{{ row.email || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="角色" min-width="190">
              <template #default="{ row }">
                <div class="tag-row">
                  <el-tag
                    v-for="roleName in row.roles"
                    :key="roleName"
                    size="small"
                    effect="plain"
                  >
                    {{ roleName }}
                  </el-tag>
                  <span v-if="!row.roles.length" class="muted">未分配</span>
                  <el-tag v-if="row.is_superuser" size="small" type="danger" effect="plain">
                    超级管理员
                  </el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <StatusTag :status="row.is_active ? 'enabled' : 'disabled'" />
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="168">
              <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button v-if="canManageUsers" link type="primary" @click="openUserDialog(row)">
                  编辑
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无用户" :image-size="80" />
            </template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="角色" name="roles">
          <el-alert
            v-if="!canManageRoles"
            type="warning"
            :closable="false"
            show-icon
            title="当前账号没有角色管理权限（roles:manage），无法查看或编辑角色。"
            class="gap"
          />
          <template v-else>
            <el-alert
              v-if="rolesError"
              type="error"
              :closable="false"
              show-icon
              :title="rolesError"
              class="gap"
            />
            <el-table v-loading="loading" :data="roles" row-key="id" size="small">
              <el-table-column label="角色名称" min-width="140">
                <template #default="{ row }">
                  <div class="name-cell">
                    <span class="name">
                      {{ row.name }}
                      <el-tag v-if="row.is_system" size="small" type="info" effect="plain">
                        系统
                      </el-tag>
                    </span>
                    <span class="muted mono">{{ row.id }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="描述" min-width="220">
                <template #default="{ row }">
                  <span class="muted">{{ row.description || '-' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="权限数" width="90">
                <template #default="{ row }">
                  <span class="count">{{ row.permissions.length }}</span>
                </template>
              </el-table-column>
              <el-table-column label="创建时间" width="168">
                <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="90" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" @click="openRoleDialog(row)">编辑</el-button>
                </template>
              </el-table-column>
              <template #empty>
                <el-empty description="暂无角色" :image-size="80" />
              </template>
            </el-table>
          </template>
        </el-tab-pane>

        <el-tab-pane v-if="canManageRoles" label="访问策略" name="policies">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="ABAC 策略按 priority 从高到低匹配，同优先级 deny 优先；命中的 deny 会覆盖 owner 与资源授权，超级管理员（is_superuser）绕过策略。未命中时沿用 RBAC 与资源授权。"
            class="gap"
          />
          <el-alert
            v-if="policiesError"
            type="error"
            :closable="false"
            show-icon
            :title="policiesError"
            class="gap"
          >
            <el-button link type="primary" @click="loadPolicies">重试</el-button>
          </el-alert>

          <el-table v-loading="policiesLoading" :data="policies" row-key="id" size="small">
            <el-table-column label="名称" min-width="170">
              <template #default="{ row }">
                <div class="name-cell">
                  <span class="name">{{ row.name }}</span>
                  <span class="muted">{{ row.description || '-' }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="效果" width="86">
              <template #default="{ row }">
                <el-tag size="small" effect="light" :type="effectTagType(row.effect)">
                  {{ row.effect }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="资源类型" width="116">
              <template #default="{ row }"><span class="mono">{{ row.resource_type }}</span></template>
            </el-table-column>
            <el-table-column label="动作" width="96">
              <template #default="{ row }"><span class="mono">{{ row.action }}</span></template>
            </el-table-column>
            <el-table-column label="角色" min-width="150">
              <template #default="{ row }">
                <div v-if="row.subject_roles.length" class="tag-row">
                  <el-tag
                    v-for="item in row.subject_roles.slice(0, 3)"
                    :key="item"
                    size="small"
                    effect="plain"
                  >
                    {{ item }}
                  </el-tag>
                  <el-tag v-if="row.subject_roles.length > 3" size="small" type="info" effect="plain">
                    +{{ row.subject_roles.length - 3 }}
                  </el-tag>
                </div>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="部门" min-width="140">
              <template #default="{ row }">
                <div v-if="row.subject_departments.length" class="tag-row">
                  <el-tag
                    v-for="item in row.subject_departments.slice(0, 3)"
                    :key="item"
                    size="small"
                    effect="plain"
                  >
                    {{ item }}
                  </el-tag>
                  <el-tag
                    v-if="row.subject_departments.length > 3"
                    size="small"
                    type="info"
                    effect="plain"
                  >
                    +{{ row.subject_departments.length - 3 }}
                  </el-tag>
                </div>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="租户" min-width="130">
              <template #default="{ row }">
                <div v-if="row.tenant_ids.length" class="tag-row">
                  <el-tag
                    v-for="item in row.tenant_ids.slice(0, 3)"
                    :key="item"
                    size="small"
                    effect="plain"
                  >
                    {{ item }}
                  </el-tag>
                  <el-tag v-if="row.tenant_ids.length > 3" size="small" type="info" effect="plain">
                    +{{ row.tenant_ids.length - 3 }}
                  </el-tag>
                </div>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="资源 ID" min-width="160">
              <template #default="{ row }">
                <div v-if="row.resource_ids.length" class="tag-row">
                  <el-tag
                    v-for="item in row.resource_ids.slice(0, 3)"
                    :key="item"
                    size="small"
                    effect="plain"
                    class="mono"
                  >
                    {{ item }}
                  </el-tag>
                  <el-tag v-if="row.resource_ids.length > 3" size="small" type="info" effect="plain">
                    +{{ row.resource_ids.length - 3 }}
                  </el-tag>
                </div>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="优先级" width="86">
              <template #default="{ row }"><span class="mono">{{ row.priority }}</span></template>
            </el-table-column>
            <el-table-column label="启用" width="86">
              <template #default="{ row }">
                <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="130" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openPolicyDialog(row)">编辑</el-button>
                <el-button link type="danger" @click="deletePolicy(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无访问策略" :image-size="80" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog
      v-model="userVisible"
      :title="editingUser ? '编辑用户' : '新建用户'"
      width="560px"
    >
      <el-form label-position="top" @submit.prevent="saveUser">
        <div class="grid-2">
          <el-form-item label="用户名" :required="!editingUser">
            <el-input
              v-model="userForm.username"
              :disabled="Boolean(editingUser)"
              maxlength="80"
              placeholder="登录账号"
            />
          </el-form-item>
          <el-form-item :label="editingUser ? '重置密码（可选）' : '密码'" :required="!editingUser">
            <el-input
              v-model="userForm.password"
              type="password"
              show-password
              placeholder="至少 8 个字符"
            />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="显示名称">
            <el-input v-model="userForm.display_name" maxlength="80" />
          </el-form-item>
          <el-form-item label="邮箱">
            <el-input v-model="userForm.email" placeholder="可选" />
          </el-form-item>
        </div>
        <el-form-item label="角色">
          <el-select
            v-model="userForm.role_names"
            multiple
            filterable
            allow-create
            default-first-option
            class="full"
            placeholder="选择或输入角色名称"
          >
            <el-option v-for="roleName in roleOptions" :key="roleName" :label="roleName" :value="roleName" />
          </el-select>
          <div v-if="!roles.length" class="inline-hint muted">
            角色清单不可用，可直接输入角色名称；服务端会校验角色是否存在。
          </div>
        </el-form-item>
        <el-form-item v-if="editingUser" label="账号状态">
          <el-switch
            v-model="userForm.is_active"
            active-text="启用"
            inactive-text="停用"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingUser" @click="saveUser">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="roleVisible"
      :title="editingRole ? '编辑角色' : '新建角色'"
      width="720px"
    >
      <el-form label-position="top" @submit.prevent="saveRole">
        <div class="grid-2">
          <el-form-item label="角色名称" :required="!editingRole">
            <el-input
              v-model="roleForm.name"
              :disabled="Boolean(editingRole)"
              maxlength="80"
              placeholder="如：auditor"
            />
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="roleForm.description" maxlength="200" />
          </el-form-item>
        </div>

        <el-form-item label="权限">
          <el-alert
            v-if="isAdminRole"
            type="info"
            :closable="false"
            show-icon
            title="admin 角色的权限不可修改，仅可编辑描述。"
            class="gap"
          />
          <el-checkbox-group
            v-model="roleForm.permission_codes"
            :disabled="isAdminRole"
            class="permission-groups"
          >
            <div v-for="group in groupedPermissions" :key="group.type" class="permission-group">
              <div class="group-title">{{ group.label }}</div>
              <div class="group-items">
                <el-checkbox
                  v-for="permission in group.items"
                  :key="permission.code"
                  :value="permission.code"
                >
                  <span>{{ permission.name }}</span>
                  <span class="mono muted code">{{ permission.code }}</span>
                </el-checkbox>
              </div>
            </div>
          </el-checkbox-group>
          <el-empty
            v-if="!groupedPermissions.length"
            description="权限清单不可用"
            :image-size="50"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingRole" @click="saveRole">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="policyVisible"
      :title="editingPolicy ? '编辑访问策略' : '新建访问策略'"
      width="740px"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="deny 优先于 owner 与资源授权，超级管理员（is_superuser）绕过策略；数组字段留空表示不限制该维度。"
        class="gap"
      />
      <el-form label-position="top" @submit.prevent="savePolicy">
        <div class="grid-2">
          <el-form-item label="名称" required>
            <el-input
              v-model="policyForm.name"
              :disabled="Boolean(editingPolicy)"
              maxlength="120"
              placeholder="如 deny-external-agent"
            />
            <div v-if="editingPolicy" class="inline-hint muted">策略名称创建后不可修改。</div>
          </el-form-item>
          <el-form-item label="效果">
            <el-select v-model="policyForm.effect" class="full">
              <el-option
                v-for="option in effectOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="描述">
          <el-input v-model="policyForm.description" type="textarea" :rows="2" maxlength="500" />
        </el-form-item>
        <div class="grid-2">
          <el-form-item label="资源类型" required>
            <el-select
              v-model="policyForm.resource_type"
              filterable
              allow-create
              default-first-option
              class="full"
              placeholder="如 agent；* 表示全部资源"
            >
              <el-option
                v-for="type in resourceTypeOptions"
                :key="type"
                :label="type === '*' ? '全部资源 (*)' : `${resourceLabels[type] || type} (${type})`"
                :value="type"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="动作">
            <el-select
              v-model="policyForm.action"
              filterable
              allow-create
              default-first-option
              class="full"
              placeholder="* / read / write / publish"
            >
              <el-option v-for="action in actionOptions" :key="action" :label="action" :value="action" />
            </el-select>
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="subject_roles">
            <el-input
              v-model="policyForm.subject_roles"
              placeholder="admin, developer 或 [&quot;admin&quot;]"
            />
          </el-form-item>
          <el-form-item label="subject_departments">
            <el-input
              v-model="policyForm.subject_departments"
              placeholder="dept-a, dept-b 或 JSON 数组"
            />
          </el-form-item>
          <el-form-item label="tenant_ids">
            <el-input v-model="policyForm.tenant_ids" placeholder="default 或 JSON 数组" />
          </el-form-item>
          <el-form-item label="resource_ids">
            <el-input
              v-model="policyForm.resource_ids"
              placeholder="资源 ID，逗号分隔或 JSON 数组"
            />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="优先级">
            <el-input-number
              v-model="policyForm.priority"
              :min="-1000"
              :max="1000"
              class="full"
            />
            <div class="inline-hint muted">数值越大越先匹配；同优先级 deny 优先。</div>
          </el-form-item>
          <el-form-item label="启用">
            <el-switch v-model="policyForm.enabled" active-text="启用" inactive-text="停用" />
          </el-form-item>
        </div>
        <div class="inline-hint muted">
          数组字段支持逗号分隔（中英文逗号）或 JSON 数组，例如
          <span class="mono">["admin","developer"]</span>；留空表示不限制。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="policyVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingPolicy" @click="savePolicy">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 10px 20px 18px;
}

.name-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.35;
}

.name {
  font-weight: 560;
  display: flex;
  align-items: center;
  gap: 6px;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.count {
  font-weight: 650;
}

.full {
  width: 100%;
}

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0 16px;
}

.inline-hint {
  font-size: 11.5px;
  line-height: 1.5;
  margin-top: 4px;
}

.gap {
  margin-bottom: 10px;
}

.permission-groups {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 360px;
  overflow-y: auto;
  padding-right: 4px;
}

.permission-group {
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 10px 14px;
  background: #fbfcfe;
}

.group-title {
  font-size: 12px;
  font-weight: 650;
  margin-bottom: 6px;
  color: var(--app-text);
}

.group-items {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
}

.code {
  margin-left: 6px;
  font-size: 10.5px;
}
</style>
