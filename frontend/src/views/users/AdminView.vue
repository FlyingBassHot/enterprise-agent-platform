<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import { usersApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { PermissionInfo, RoleInfo, UserInfo } from '@/types/api'
import { formatDate } from '@/utils/format'

const auth = useAuthStore()

const activeTab = ref<'users' | 'roles'>('users')
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
}

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

onMounted(load)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="用户与角色" subtitle="账号、角色与权限分配；权限变更由服务端强制执行">
      <template #actions>
        <el-button :loading="loading" @click="load">
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
